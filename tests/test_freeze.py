import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FreezeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.engineer = Actor("eng-1", "engineer")
        self.safety = Actor("safety-1", "safety")
        self.verifier = Actor("ver-1", "verifier")

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def _build_frozen_unit(self):
        """Create a unit with an implemented change and a verified action
        item, then shut the unit down so everything is frozen."""
        unit = self.service.create(
            self.admin, "unit", {"name": "Reactor-1", "location": "Plant-A"}
        )
        change = self.service.create(
            self.admin, "change",
            {"unit_id": unit["id"], "description": "Change alarm threshold"},
        )
        self.service.transition(
            self.admin, change["id"], "assess",
            {"risk_level": "medium", "analyst": "E-1"},
        )
        self.service.transition(
            self.admin, change["id"], "approve",
            {"approvals": ["S-1", "S-2"], "permit_id": "MOC-1"},
        )
        self.service.transition(
            self.admin, change["id"], "implement", {"procedure_version": "v2"}
        )
        item = self.service.create(
            self.admin, "action_item",
            {"change_id": change["id"], "description": "Train operators", "owner": "O-1"},
        )
        self.service.transition(
            self.admin, item["id"], "complete",
            {"completed_by": "O-1", "evidence": "training-log"},
        )
        self.service.transition(
            self.verifier, item["id"], "verify", {"verifier": "V-1"}
        )
        self.service.transition(
            self.admin, unit["id"], "shutdown", {"reason": "turnaround"}
        )
        # Re-fetch so callers observe the post-transition state.
        unit = self.service.get(unit["id"])
        change = self.service.get(change["id"])
        item = self.service.get(item["id"])
        return unit, change, item

    # -- freeze on shutdown ------------------------------------------------

    def test_shutdown_freezes_changes_and_action_items(self):
        unit, change, item = self._build_frozen_unit()
        self.assertEqual(unit["status"], "shutdown")
        self.assertTrue(self.repo.is_frozen(change["id"]))
        self.assertTrue(self.repo.is_frozen(item["id"]))

    def test_shutdown_without_children_freezes_nothing(self):
        unit = self.service.create(
            self.admin, "unit", {"name": "U-2", "location": "Plant-B"}
        )
        self.service.transition(self.admin, unit["id"], "shutdown", {"reason": "maintenance"})
        self.assertEqual(self.repo.list_frozen(unit_id=unit["id"]), [])

    # -- progress actions blocked ------------------------------------------

    def test_frozen_change_implement_returns_latest_version_and_conflicts(self):
        unit, change, item = self._build_frozen_unit()
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                self.engineer, change["id"], "implement", {"procedure_version": "v3"}
            )
        exc = ctx.exception
        self.assertIsNotNone(exc.details)
        self.assertEqual(exc.details["entity"]["id"], change["id"])
        self.assertEqual(exc.details["entity"]["version"], change["version"])
        self.assertEqual(exc.details["entity"]["status"], "implemented")
        conflicts = exc.details["conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["code"], "frozen")

    def test_frozen_change_commission_blocked(self):
        unit, change, item = self._build_frozen_unit()
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.engineer, change["id"], "commission", {"tests_passed": True}
            )

    def test_frozen_action_item_complete_blocked(self):
        unit, change, item = self._build_frozen_unit()
        # Reopen first so the item is in a state where complete would apply.
        self.service.transition(self.verifier, item["id"], "reopen", {"reason": "rework"})
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.engineer, item["id"], "complete",
                {"completed_by": "O-1", "evidence": "log"},
            )

    def test_frozen_action_item_verify_blocked(self):
        unit, change, item = self._build_frozen_unit()
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.verifier, item["id"], "verify", {"verifier": "V-2"}
            )

    # -- safety-only unfreeze ---------------------------------------------

    def test_engineer_cannot_unfreeze(self):
        unit, change, item = self._build_frozen_unit()
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.engineer, change["id"], "unfreeze", {"note": "checked"})

    def test_safety_can_unfreeze_after_inventory(self):
        unit, change, item = self._build_frozen_unit()
        result = self.service.transition(
            self.safety, change["id"], "unfreeze", {"note": "inventory checked"}
        )
        self.assertEqual(result["id"], change["id"])
        self.assertFalse(self.repo.is_frozen(change["id"]))
        record = self.repo.get_freeze(change["id"])
        self.assertEqual(record["unfrozen_by"], "safety-1")
        self.assertEqual(record["unfreeze_note"], "inventory checked")

    def test_unfreeze_non_frozen_entity_fails(self):
        unit = self.service.create(
            self.admin, "unit", {"name": "U-3", "location": "Plant-C"}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(self.safety, unit["id"], "unfreeze", {"note": "x"})

    # -- startup recalculation ---------------------------------------------

    def test_startup_blocked_while_frozen_items_remain(self):
        unit, change, item = self._build_frozen_unit()
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.admin, unit["id"], "startup", {})
        exc = ctx.exception
        self.assertIsNotNone(exc.details)
        frozen_items = exc.details["frozen_items"]
        ids = {entry["entity_id"] for entry in frozen_items}
        self.assertIn(change["id"], ids)
        self.assertIn(item["id"], ids)
        for entry in frozen_items:
            self.assertEqual(entry["drift_reasons"], [])

    def test_startup_succeeds_after_all_unfrozen(self):
        unit, change, item = self._build_frozen_unit()
        self.service.transition(self.safety, change["id"], "unfreeze", {"note": "ok"})
        self.service.transition(self.safety, item["id"], "unfreeze", {"note": "ok"})
        result = self.service.transition(self.admin, unit["id"], "startup", {})
        self.assertEqual(result["status"], "operating")

    def test_startup_detects_rolled_back_change(self):
        unit, change, item = self._build_frozen_unit()
        # Rollback is not a blocked progress action, so it can drift during
        # the shutdown freeze.
        self.service.transition(
            self.admin, change["id"], "rollback", {"reason": "drift detected"}
        )
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.admin, unit["id"], "startup", {})
        frozen_items = ctx.exception.details["frozen_items"]
        change_entry = next(e for e in frozen_items if e["entity_id"] == change["id"])
        self.assertIn("change was rolled back during shutdown", change_entry["drift_reasons"])
        self.assertTrue(self.repo.is_frozen(change["id"]))

    def test_startup_detects_reopened_action_item(self):
        unit, change, item = self._build_frozen_unit()
        self.service.transition(self.verifier, item["id"], "reopen", {"reason": "rework"})
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.admin, unit["id"], "startup", {})
        frozen_items = ctx.exception.details["frozen_items"]
        item_entry = next(e for e in frozen_items if e["entity_id"] == item["id"])
        self.assertIn("action item was reopened during shutdown", item_entry["drift_reasons"])
        self.assertTrue(self.repo.is_frozen(item["id"]))

    def test_startup_detects_owner_change(self):
        unit, change, item = self._build_frozen_unit()
        # Simulate an owner reassignment that happened during the shutdown.
        current = self.service.get(item["id"])
        new_data = dict(current["data"])
        new_data["owner"] = "NEW-OWNER"
        self.repo.update_entity(item["id"], current["version"], current["status"], new_data)
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.admin, unit["id"], "startup", {})
        frozen_items = ctx.exception.details["frozen_items"]
        item_entry = next(e for e in frozen_items if e["entity_id"] == item["id"])
        self.assertTrue(
            any("owner changed" in reason for reason in item_entry["drift_reasons"]),
            item_entry["drift_reasons"],
        )
        self.assertTrue(self.repo.is_frozen(item["id"]))

    # -- idempotent retry --------------------------------------------------

    def test_shutdown_retry_does_not_refreeze_or_rewrite_audit(self):
        unit = self.service.create(
            self.admin, "unit", {"name": "U-4", "location": "Plant-D"}
        )
        change = self.service.create(
            self.admin, "change", {"unit_id": unit["id"], "description": "test change"}
        )
        self.service.transition(
            self.admin, change["id"], "assess",
            {"risk_level": "low", "analyst": "E-1"},
        )
        self.service.transition(
            self.admin, change["id"], "approve",
            {"approvals": ["S-1"], "permit_id": "MOC-2"},
        )
        self.service.transition(
            self.admin, change["id"], "implement", {"procedure_version": "v1"}
        )
        key = "shutdown-retry-1"
        first = self.service.transition(
            self.admin, unit["id"], "shutdown", {"reason": "turnaround"},
            idempotency_key=key,
        )
        frozen_after_first = len(self.repo.list_frozen(unit_id=unit["id"]))
        audit_after_first = len(self.repo.list_audit())
        retry = self.service.transition(
            self.admin, unit["id"], "shutdown", {"reason": "turnaround"},
            idempotency_key=key,
        )
        self.assertEqual(first["id"], retry["id"])
        self.assertEqual(retry["status"], "shutdown")
        self.assertEqual(len(self.repo.list_frozen(unit_id=unit["id"])), frozen_after_first)
        self.assertEqual(len(self.repo.list_audit()), audit_after_first)

    def test_idempotency_key_reused_for_different_action_conflicts(self):
        unit, change, item = self._build_frozen_unit()
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin, unit["id"], "startup", {},
                idempotency_key="shutdown-retry-1",
            )

    # -- persistence across restart ----------------------------------------

    def test_freeze_state_persists_across_service_restart(self):
        unit, change, item = self._build_frozen_unit()
        # Simulate a service restart by building a new service on the same DB.
        restarted = DomainService(self.repo, RuleEngine())
        self.assertTrue(restarted.repository.is_frozen(change["id"]))
        self.assertTrue(restarted.repository.is_frozen(item["id"]))
        # Safety can still unfreeze after restart.
        restarted.transition(self.safety, change["id"], "unfreeze", {"note": "ok"})
        restarted.transition(self.safety, item["id"], "unfreeze", {"note": "ok"})
        result = restarted.transition(self.admin, unit["id"], "startup", {})
        self.assertEqual(result["status"], "operating")

    # -- audit trail -------------------------------------------------------

    def test_freeze_writes_audit_records(self):
        unit, change, item = self._build_frozen_unit()
        audits = self.repo.list_audit(entity_id=change["id"])
        freeze_audits = [a for a in audits if a["action"] == "freeze"]
        self.assertEqual(len(freeze_audits), 1)
        self.assertEqual(freeze_audits[0]["actor_id"], "admin")
        unfreeze_audits = [a for a in audits if a["action"] == "unfreeze"]
        self.assertEqual(len(unfreeze_audits), 0)
        self.service.transition(self.safety, change["id"], "unfreeze", {"note": "checked"})
        audits = self.repo.list_audit(entity_id=change["id"])
        unfreeze_audits = [a for a in audits if a["action"] == "unfreeze"]
        self.assertEqual(len(unfreeze_audits), 1)
        self.assertEqual(unfreeze_audits[0]["actor_id"], "safety-1")


if __name__ == "__main__":
    unittest.main()
