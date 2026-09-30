import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_workflow(self):
        created = {}
        steps = [{'op': 'create', 'as': 'unit', 'kind': 'unit', 'data': {'name': 'Reactor-1', 'location': 'Plant-A'}}, {'op': 'create', 'as': 'change', 'kind': 'change', 'data': {'unit_id': '{unit}', 'description': 'Change alarm threshold'}}, {'op': 'transition', 'target': 'change', 'action': 'assess', 'data': {'risk_level': 'medium', 'analyst': 'E-1'}, 'expect': 'assessed'}, {'op': 'transition', 'target': 'change', 'action': 'approve', 'data': {'approvals': ['S-1', 'S-2'], 'permit_id': 'MOC-1'}, 'expect': 'approved'}, {'op': 'transition', 'target': 'change', 'action': 'implement', 'data': {'procedure_version': 'v2'}, 'expect': 'implemented'}, {'op': 'create', 'as': 'item', 'kind': 'action_item', 'data': {'change_id': '{change}', 'description': 'Train operators', 'owner': 'O-1'}}, {'op': 'transition', 'target': 'item', 'action': 'complete', 'data': {'completed_by': 'O-1', 'evidence': 'training-log'}, 'expect': 'completed'}, {'op': 'transition', 'target': 'item', 'action': 'verify', 'data': {'verifier': 'V-1'}, 'expect': 'verified'}, {'op': 'transition', 'target': 'change', 'action': 'commission', 'data': {'tests_passed': True}, 'expect': 'commissioned'}, {'op': 'transition', 'target': 'change', 'action': 'rollback', 'data': {'reason': 'unexpected drift'}, 'expect': 'rolled_back'}]
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    self.actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])


if __name__ == "__main__":
    unittest.main()
