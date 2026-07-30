import unittest
from datetime import datetime, timedelta, timezone

from monitoring.rules import RuleEngine, validate_rule_spec


def rule(**overrides):
    base = {
        "id": 1,
        "code": "600000",
        "name": "价格上沿",
        "direction": "sell",
        "metric": "price",
        "operator": "gte",
        "threshold": 100,
        "confirm_count": 2,
        "cooldown_seconds": 3600,
        "hysteresis": 1,
    }
    return {**base, **overrides}


def state():
    return {
        "rule_id": 1,
        "consecutive_hits": 0,
        "armed": 1,
        "last_value": None,
        "last_evaluated_at": None,
        "last_triggered_at": None,
    }


class RuleEngineTests(unittest.TestCase):
    def test_confirmation_rearm_and_hysteresis_prevent_duplicate_alerts(self):
        engine = RuleEngine()
        now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
        current = state()

        first = engine.evaluate(rule(), current, 101, now)
        self.assertFalse(first.triggered)
        second = engine.evaluate(rule(), first.state, 102, now + timedelta(minutes=1))
        self.assertTrue(second.triggered)
        duplicate = engine.evaluate(
            rule(), second.state, 103, now + timedelta(minutes=2)
        )
        self.assertFalse(duplicate.triggered)
        self.assertEqual(duplicate.state["armed"], 0)

        not_far_enough = engine.evaluate(
            rule(), duplicate.state, 99.5, now + timedelta(minutes=3)
        )
        self.assertEqual(not_far_enough.state["armed"], 0)
        rearmed = engine.evaluate(
            rule(), not_far_enough.state, 99, now + timedelta(minutes=4)
        )
        self.assertEqual(rearmed.state["armed"], 1)

    def test_crossing_rule_fires_only_on_boundary_cross(self):
        engine = RuleEngine()
        now = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)
        crossing = rule(
            operator="crosses_above", confirm_count=1, hysteresis=0
        )
        below = engine.evaluate(crossing, state(), 99, now)
        above = engine.evaluate(crossing, below.state, 101, now + timedelta(minutes=1))
        self.assertTrue(above.triggered)

    def test_crossing_rule_rejects_multiple_confirmation_samples(self):
        with self.assertRaisesRegex(ValueError, "必须为 1"):
            validate_rule_spec(rule(operator="crosses_below", confirm_count=2))


if __name__ == "__main__":
    unittest.main()
