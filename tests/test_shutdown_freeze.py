import json
import tempfile
import threading
import unittest
import urllib.request
from http.client import RemoteDisconnected
from pathlib import Path

from src.domain import Actor, ConflictError, FreezeBlocked, InvalidTransition, PermissionDenied
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ShutdownFreezeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.repo = SQLiteRepository(self.db_path)
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.operator = Actor("op-1", "operator")
        self.engineer = Actor("eng-1", "engineer")
        self.safety = Actor("safety-1", "safety")
        self.verifier = Actor("verifier-1", "verifier")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_unit_with_change(self):
        unit = self.service.create(
            self.admin, "unit", {"name": "Reactor-1", "location": "Plant-A"}
        )
        change = self.service.create(
            self.engineer, "change",
            {"unit_id": unit["id"], "description": "Change alarm threshold"},
        )
        self.service.transition(
            self.engineer, change["id"], "assess",
            {"risk_level": "medium", "analyst": "E-1"},
        )
        self.service.transition(
            self.safety, change["id"], "approve",
            {"approvals": ["S-1", "S-2"], "permit_id": "MOC-1"},
        )
        self.service.transition(
            self.engineer, change["id"], "implement", {"procedure_version": "v2"},
        )
        item = self.service.create(
            self.safety, "action_item",
            {"change_id": change["id"], "description": "Train operators", "owner": "O-1"},
        )
        self.service.transition(
            self.engineer, item["id"], "complete",
            {"completed_by": "O-1", "evidence": "training-log"},
        )
        return unit, change, item

    def test_shutdown_freezes_changes_and_action_items(self):
        unit, change, item = self._make_unit_with_change()
        result = self.service.transition(
            self.operator, unit["id"], "shutdown", {"reason": "maintenance"}
        )
        self.assertEqual(result["status"], "shutdown")
        ids = {entry["entity_id"] for entry in result["frozen_items"]}
        self.assertEqual(ids, {change["id"], item["id"]})

        # Engineer submissions report the latest version and conflicts.
        with self.assertRaises(FreezeBlocked) as blocked:
            self.service.transition(
                self.engineer, change["id"], "commission", {"tests_passed": True}
            )
        exc = blocked.exception
        self.assertEqual(exc.entity["id"], change["id"])
        self.assertEqual(exc.entity["version"], self.service.get(change["id"])["version"])
        self.assertGreater(exc.entity["version"], change["version"])
        conflict_ids = {entry["entity_id"] for entry in exc.conflicts}
        self.assertEqual(conflict_ids, {change["id"], item["id"]})

        with self.assertRaises(FreezeBlocked):
            self.service.transition(
                self.verifier, item["id"], "verify", {"verifier": "V-1"}
            )

    def test_only_safety_can_release_one_item_at_a_time(self):
        unit, change, item = self._make_unit_with_change()
        self.service.transition(
            self.operator, unit["id"], "shutdown", {"reason": "maintenance"}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.engineer, item["id"], "release",
                {"inventory_note": "counted on the floor"},
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.operator, change["id"], "release",
                {"inventory_note": "counted on the floor"},
            )
        # release requires the inventory note.
        with self.assertRaises(Exception):
            self.service.transition(self.safety, item["id"], "release", {})

        # Releasing just the item leaves the change frozen.
        self.service.transition(
            self.safety, item["id"], "release",
            {"inventory_note": "training log present"},
        )
        self.service.transition(
            self.verifier, item["id"], "verify", {"verifier": "V-1"}
        )
        with self.assertRaises(FreezeBlocked):
            self.service.transition(
                self.engineer, change["id"], "commission", {"tests_passed": True}
            )

        # Releasing an unfrozen entity is invalid.
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.safety, item["id"], "release",
                {"inventory_note": "again"},
            )

    def test_startup_resumes_clean_items(self):
        unit, change, item = self._make_unit_with_change()
        self.service.transition(
            self.operator, unit["id"], "shutdown", {"reason": "maintenance"}
        )
        result = self.service.transition(
            self.operator, unit["id"], "startup", {"reason": "maintenance done"}
        )
        self.assertEqual(result["status"], "operating")
        self.assertEqual(set(result["resumed"]), {change["id"], item["id"]})
        self.assertEqual(result["still_frozen"], [])
        # Work flows again.
        self.service.transition(
            self.verifier, item["id"], "verify", {"verifier": "V-1"}
        )

    def test_startup_keeps_rolled_back_change_and_reopened_item_and_owner_change_frozen(self):
        unit, change, item = self._make_unit_with_change()
        self.service.transition(
            self.operator, unit["id"], "shutdown", {"reason": "maintenance"}
        )
        # Safety clears items one by one after inventory.
        self.service.transition(
            self.safety, change["id"], "release",
            {"inventory_note": "procedure reviewed"},
        )
        self.service.transition(
            self.safety, item["id"], "release",
            {"inventory_note": "training log present"},
        )
        # During shutdown the change is rolled back and the item verified then reopened.
        self.service.transition(
            self.engineer, change["id"], "rollback", {"reason": "drift found"}
        )
        self.service.transition(
            self.verifier, item["id"], "verify", {"verifier": "V-1"}
        )
        self.service.transition(
            self.verifier, item["id"], "reopen", {"reason": "evidence missing"}
        )

        # A second change/item pair exercises the owner-change reason.
        change2 = self.service.create(
            self.engineer, "change",
            {"unit_id": unit["id"], "description": "Second change"},
        )
        item2 = self.service.create(
            self.safety, "action_item",
            {"change_id": change2["id"], "description": "Update sign", "owner": "O-2"},
        )
        self.service.transition(
            self.safety, item2["id"], "release",
            {"inventory_note": "sign located"},
        )
        self.service.transition(
            self.safety, change2["id"], "release",
            {"inventory_note": "paperwork in order"},
        )
        self.service.transition(
            self.safety, item2["id"], "assign", {"owner": "O-9"}
        )

        result = self.service.transition(
            self.operator, unit["id"], "startup", {"reason": "maintenance done"}
        )
        still = {entry["entity_id"]: entry for entry in result["still_frozen"]}
        self.assertEqual(set(result["resumed"]), set())
        self.assertIn(change["id"], still)
        self.assertEqual(still[change["id"]]["reblock_reason"],
                         "change rolled back during shutdown")
        self.assertIn(item["id"], still)
        self.assertEqual(still[item["id"]]["reblock_reason"],
                         "action item reopened during shutdown")
        self.assertIn(item2["id"], still)
        self.assertIn("owner changed during shutdown", still[item2["id"]]["reblock_reason"])
        # change2 never moved, so it resumes even though its item did not.
        self.assertNotIn(change2["id"], still)

        # Drifted items need another explicit safety release after restart.
        self.service.transition(
            self.safety, change["id"], "release",
            {"inventory_note": "rollback accepted"},
        )

    def test_item_created_during_shutdown_is_born_frozen(self):
        unit, change, item = self._make_unit_with_change()
        self.service.transition(
            self.operator, unit["id"], "shutdown", {"reason": "maintenance"}
        )
        late_item = self.service.create(
            self.safety, "action_item",
            {"change_id": change["id"], "description": "Late catch", "owner": "O-3"},
        )
        with self.assertRaises(FreezeBlocked):
            self.service.transition(
                self.engineer, late_item["id"], "complete",
                {"completed_by": "O-3", "evidence": "none"},
            )
        result = self.service.transition(
            self.operator, unit["id"], "startup", {"reason": "done"}
        )
        # Untouched born-frozen item is clean and resumes automatically.
        self.assertIn(late_item["id"], result["resumed"])

    def test_retrying_shutdown_same_key_does_not_duplicate(self):
        unit, change, item = self._make_unit_with_change()
        first = self.service.transition(
            self.operator, unit["id"], "shutdown", {"reason": "maintenance"},
            idempotency_key="shutdown-1",
        )
        audit_after_first = len(self.service.audit_log())
        # Simulated network retry with the same request and key.
        second = self.service.transition(
            self.operator, unit["id"], "shutdown", {"reason": "maintenance"},
            idempotency_key="shutdown-1",
        )
        self.assertEqual(second["version"], first["version"])
        self.assertEqual(second["status"], "shutdown")
        self.assertEqual(len(self.service.audit_log()), audit_after_first)
        self.assertEqual(
            {entry["entity_id"] for entry in second["frozen_items"]},
            {change["id"], item["id"]},
        )
        # Same key with a different payload is rejected.
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.operator, unit["id"], "startup", {"reason": "other"},
                idempotency_key="shutdown-1",
            )

    def test_retrying_normal_action_is_idempotent(self):
        unit, change, item = self._make_unit_with_change()
        fresh = self.service.create(
            self.safety, "action_item",
            {"change_id": change["id"], "description": "Another item", "owner": "O-4"},
        )
        completed = self.service.transition(
            self.engineer, fresh["id"], "complete",
            {"completed_by": "O-4", "evidence": "photo"},
            idempotency_key="complete-1",
        )
        audit_count = len(self.service.audit_log())
        retried = self.service.transition(
            self.engineer, fresh["id"], "complete",
            {"completed_by": "O-4", "evidence": "photo"},
            idempotency_key="complete-1",
        )
        self.assertEqual(retried["version"], completed["version"])
        self.assertEqual(len(self.service.audit_log()), audit_count)
        self.assertEqual(completed["status"], "completed")

    def test_service_restart_can_resume(self):
        unit, change, item = self._make_unit_with_change()
        self.service.transition(
            self.operator, unit["id"], "shutdown", {"reason": "maintenance"},
            idempotency_key="shutdown-restart",
        )
        # Restart: a brand new repository/service on the same file.
        repo2 = SQLiteRepository(self.db_path)
        service2 = DomainService(repo2, RuleEngine())
        retried = service2.transition(
            self.operator, unit["id"], "shutdown", {"reason": "maintenance"},
            idempotency_key="shutdown-restart",
        )
        self.assertEqual(retried["status"], "shutdown")
        audit = service2.audit_log()
        freeze_audits = [row for row in audit if row["action"] == "freeze"]
        self.assertEqual(len(freeze_audits), 2)
        # Startup still works after restart and freezes are durable.
        started = service2.transition(
            self.operator, unit["id"], "startup", {"reason": "done"},
            idempotency_key="startup-restart",
        )
        self.assertEqual(started["status"], "operating")
        self.assertEqual(set(started["resumed"]), {change["id"], item["id"]})
        # Restart again and replay the startup key; no new audit rows.
        repo3 = SQLiteRepository(self.db_path)
        service3 = DomainService(repo3, RuleEngine())
        audit_before = len(service3.audit_log())
        replay = service3.transition(
            self.operator, unit["id"], "startup", {"reason": "done"},
            idempotency_key="startup-restart",
        )
        self.assertEqual(replay["status"], "operating")
        self.assertEqual(len(service3.audit_log()), audit_before)

    def test_shutdown_freeze_visible_over_http_with_conflicts(self):
        unit, change, item = self._make_unit_with_change()
        server = create_server("127.0.0.1", 0, self.service, RuleEngine(),
                               str(Path(__file__).resolve().parent.parent / "static"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            def post(path, body, role, user="u", key=None):
                headers = {"Content-Type": "application/json",
                           "X-Role": role, "X-User-Id": user}
                if key:
                    headers["Idempotency-Key"] = key
                request = urllib.request.Request(
                    "http://%s:%s%s" % (host, port, path),
                    data=json.dumps(body).encode("utf-8"),
                    headers=headers, method="POST",
                )
                try:
                    with urllib.request.urlopen(request) as response:
                        return response.status, json.loads(response.read())
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read())

            status, body = post("/api/entities/%s/actions" % unit["id"],
                                {"action": "shutdown",
                                 "data": {"reason": "maintenance"},
                                 "expected_version": unit["version"]},
                                "operator", "op-1", key="http-shutdown")
            self.assertEqual(status, 200)
            self.assertEqual(len(body["frozen_items"]), 2)

            # Retry with the same Idempotency-Key returns the same result.
            audit_count = len(self.service.audit_log())
            status, body = post("/api/entities/%s/actions" % unit["id"],
                                {"action": "shutdown",
                                 "data": {"reason": "maintenance"},
                                 "expected_version": unit["version"]},
                                "operator", "op-1", key="http-shutdown")
            self.assertEqual(status, 200)
            self.assertEqual(len(self.service.audit_log()), audit_count)

            # Engineer commission is blocked with latest version and conflicts.
            status, body = post("/api/entities/%s/actions" % change["id"],
                                {"action": "commission",
                                 "data": {"tests_passed": True}},
                                "engineer", "eng-1")
            self.assertEqual(status, 409)
            self.assertEqual(body["type"], "FreezeBlocked")
            self.assertEqual(body["entity"]["id"], change["id"])
            self.assertEqual(len(body["conflicts"]), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
