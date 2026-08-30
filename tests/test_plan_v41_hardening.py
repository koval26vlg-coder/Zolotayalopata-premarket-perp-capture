"""Immutable v41 lineage and active trust-bound PlanOnly v42 contract."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import event_bound_plan_proposal
import frozen_plan_bindings
import project_config
import risk_gate


V40 = ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v40.json"
V41 = ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v41.json"
V42 = ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v42.json"
V40_PLAN_HASH = "fbc4456333a2d7886fac3f887d7cca1258dec5091d9671af17ccdddf42eb6c2f"
V40_FILE_SHA = "e60cc27bcaaaff01576026e8649b3be8aca38a5e9827286001d79dbd5ec9498e"
V41_PLAN_HASH = "137e4c7da1236727cadbba8b22b209a31465b9a7353b06cd916ab7f207a109b2"
V41_FILE_SHA = "ab568c1656342f33ff6a9ab415129fbf4e0386e9e24112d90861536adbd376d8"
V42_PLAN_HASH = "72acbc1426ddfc5ccb168dd1d75d6414e5af0d30507b80f32fa8d85020691926"
V42_FILE_SHA = "696f6368f1f2a72fdcaa598148766324ea0d24bdc2e28308f8e15470a5e081b5"
V42_STATUS = "HISTORICAL_ACQUISITION_REPLAY_TRUST_BOUND_NO_CAPTURE"


class V42ImmutableTrustBindingTests(unittest.TestCase):
    def test_v40_remains_byte_exact_and_is_retired(self) -> None:
        raw = V40.read_bytes()
        payload = json.loads(raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), V40_FILE_SHA)
        self.assertEqual(payload["plan_hash"], V40_PLAN_HASH)
        retired = {
            row["plan_id"]: row for row in frozen_plan_bindings.RETIRED_PLANS
        }
        self.assertEqual(
            retired["premarket_perp_capture_20260822_v40"]["plan_file_sha256"],
            V40_FILE_SHA,
        )

    def test_v41_remains_byte_exact_and_is_retired(self) -> None:
        raw = V41.read_bytes()
        payload = json.loads(raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), V41_FILE_SHA)
        self.assertEqual(payload["plan_hash"], V41_PLAN_HASH)
        retired = {
            row["plan_id"]: row for row in frozen_plan_bindings.RETIRED_PLANS
        }
        self.assertEqual(
            retired["premarket_perp_capture_20260822_v41"],
            {
                "schema": "premarket_perp_capture_planonly_v41",
                "plan_id": "premarket_perp_capture_20260822_v41",
                "plan_hash": V41_PLAN_HASH,
                "plan_file_sha256": V41_FILE_SHA,
                "path": "docs/plans/premarket-perp-capture-planonly-20260822-v41.json",
            },
        )

    def test_v42_is_active_and_supersedes_v41(self) -> None:
        self.assertTrue(V42.is_file(), "v42 trust-bound PlanOnly must be issued")
        raw = V42.read_bytes()
        plan = json.loads(raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), V42_FILE_SHA)
        self.assertEqual(plan["plan_hash"], V42_PLAN_HASH)
        self.assertEqual(plan["schema"], "premarket_perp_capture_planonly_v42")
        self.assertEqual(plan["plan_id"], "premarket_perp_capture_20260822_v42")
        self.assertEqual(plan["status"], V42_STATUS)
        self.assertEqual(plan["supersedes_plan_id"], "premarket_perp_capture_20260822_v41")
        self.assertEqual(plan["supersedes_plan_hash"], V41_PLAN_HASH)
        self.assertEqual(Path(project_config.PLAN_PATH), V42)
        self.assertEqual(frozen_plan_bindings.PLAN_ID, plan["plan_id"])
        self.assertEqual(frozen_plan_bindings.PLAN_HASH, plan["plan_hash"])
        self.assertEqual(frozen_plan_bindings.PLAN_FILE_SHA256, V42_FILE_SHA)

    def test_future_event_bound_identity_moves_to_v43(self) -> None:
        self.assertEqual(
            event_bound_plan_proposal.PROPOSED_PLAN_ID,
            "premarket_perp_capture_20260822_v43",
        )
        self.assertTrue(str(project_config.NEXT_EVENT_BOUND_PLAN_PATH).endswith("v43.json"))

    def test_v42_uses_separate_control_and_historical_roots(self) -> None:
        self.assertIn("v42", project_config.ANNOUNCEMENT_STATE_PATH.name)
        self.assertEqual(
            Path(project_config.HISTORICAL_SEED_PATH),
            ROOT / "docs/historical/historical-event-seeds-v1.json",
        )
        self.assertTrue(str(project_config.HISTORICAL_EVIDENCE_ROOT).endswith("historical\\v42"))
        self.assertNotEqual(
            Path(project_config.HISTORICAL_EVIDENCE_ROOT),
            ROOT / "docs/historical/v41",
        )

    def test_v42_status_has_exact_no_capture_authority(self) -> None:
        authorization = risk_gate.PLAN_WRITE_AUTHORIZATION[V42_STATUS]
        self.assertIn("historical_market_data_acquisition", authorization["write_classes"])
        self.assertNotIn("market_data_capture", authorization["write_classes"])
        self.assertNotIn(risk_gate.CAPTURE_ACTION, authorization["authorized_actions"])

    def test_production_sealed_execution_is_not_run_without_trusted_loader(self) -> None:
        plan = json.loads(V42.read_text(encoding="utf-8"))
        replay = plan["historical_causal_replay"]
        self.assertEqual(
            replay["production_sealed_request_result"],
            "NOT_RUN_TRUSTED_EVIDENCE_LOADER_REQUIRED",
        )
        self.assertEqual(
            replay["trusted_evidence_loader"],
            "NOT_IMPLEMENTED_FAIL_CLOSED_UNTIL_EVENT_BOUND_V43",
        )


if __name__ == "__main__":
    unittest.main()
