import unittest

from src.rules import required_approval_level
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        self.assertEqual(required_approval_level("low"), 1)
        self.assertEqual(required_approval_level("high"), 3)
        self.assertEqual(required_approval_level("unknown"), 4)
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(self.admin, {"kind": "change", "status": "assessed", "id": "c1", "data": {"required_approvals": 2}}, "approve", {"approvals": ["one"], "permit_id": "p"})


if __name__ == "__main__":
    unittest.main()
