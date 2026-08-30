"""The additive v43 primitives must not silently activate under v42."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import frozen_plan_bindings as trust_root  # noqa: E402
import project_config as config  # noqa: E402
import risk_gate  # noqa: E402
import v43_event_binding  # noqa: E402


class V43PreparationBoundaryTests(unittest.TestCase):
    def test_active_plan_remains_v42_no_capture(self) -> None:
        plan = risk_gate.load_and_verify_plan()
        self.assertEqual(trust_root.PLAN_ID, "premarket_perp_capture_20260822_v42")
        self.assertEqual(plan["plan_id"], trust_root.PLAN_ID)
        self.assertEqual(
            plan["status"],
            "HISTORICAL_ACQUISITION_REPLAY_TRUST_BOUND_NO_CAPTURE",
        )
        with self.assertRaises(risk_gate.RiskGateError):
            risk_gate.verify_plan_write_authorization(plan, "market_data_capture")

    def test_no_placeholder_v43_plan_was_written(self) -> None:
        self.assertFalse(config.NEXT_EVENT_BOUND_PLAN_PATH.exists())
        readiness = v43_event_binding.issue_readiness(
            proposal=None,
            lifecycle_snapshot=None,
            approval_receipt=None,
        )
        self.assertEqual(readiness["status"], "NOT_ISSUED_EVENT_REQUIRED")
        self.assertFalse(readiness["capture_authorized"])

    def test_candidate_modules_are_not_part_of_v42_runtime_authority(self) -> None:
        bound_paths = {path for _role, path in config.BOUND_RUNTIME_FILES}
        for path in (
            "src/public_ws.py",
            "src/l2_book.py",
            "src/l2_evidence.py",
            "src/v43_event_binding.py",
            "src/venue_ws_v43.py",
        ):
            with self.subTest(path=path):
                self.assertNotIn(path, bound_paths)


if __name__ == "__main__":
    unittest.main()
