"""Preserved PlanOnly v7 lineage and the active capture-disabled contract."""

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

from canonical_hash import canonical_hash  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import project_config as config  # noqa: E402
import risk_gate  # noqa: E402


V6_PLAN = ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v6.json"
V7_PLAN = ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v7.json"


class V7ImmutableLineageTests(unittest.TestCase):
    def test_v7_and_v6_are_preserved_as_retired(self) -> None:
        self.assertTrue(V6_PLAN.is_file())
        self.assertEqual(
            hashlib.sha256(V6_PLAN.read_bytes()).hexdigest(),
            "0be95c2a4a60e6457697bfa0bf612ada7b0e63efdd903abafb7ab9c77f1bbe6f",
        )
        retired = {str(item["path"]).replace("\\", "/"): item for item in trust_root.RETIRED_PLANS}
        self.assertIn(
            "docs/plans/premarket-perp-capture-planonly-20260822-v6.json",
            retired,
        )
        self.assertEqual(
            retired["docs/plans/premarket-perp-capture-planonly-20260822-v6.json"]
            ["plan_hash"],
            "b2e07bd3475b57b4d815bf1adca8dbd5b52f120d4b544ea10d3227186682ab2e",
        )
        self.assertTrue(V7_PLAN.is_file())
        self.assertEqual(
            hashlib.sha256(V7_PLAN.read_bytes()).hexdigest(),
            "6ac94a64be7a83835b764115d1805f05d2194ac060c4b4df7ddfb768bb5ab75e",
        )
        self.assertIn(
            "docs/plans/premarket-perp-capture-planonly-20260822-v7.json",
            retired,
        )
        # The version is whatever the trust root pins today. What must hold at every
        # reissue is that the schema, the plan_id and the filename agree about it.
        version = trust_root.ACTIVE_PLAN["plan_id"].rsplit("_", 1)[-1]
        self.assertRegex(version, r"^v\d+$")
        self.assertEqual(
            trust_root.ACTIVE_PLAN["schema"],
            f"premarket_perp_capture_planonly_{version}",
        )
        self.assertTrue(config.PLAN_PATH.name.endswith(f"-{version}.json"))

    def test_active_plan_matches_external_trust_root_and_supersedes_v8(self) -> None:
        plan = json.loads(config.PLAN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(plan["plan_id"], trust_root.PLAN_ID)
        self.assertEqual(plan["plan_hash"], trust_root.PLAN_HASH)
        self.assertEqual(
            hashlib.sha256(config.PLAN_PATH.read_bytes()).hexdigest(),
            trust_root.PLAN_FILE_SHA256,
        )
        without_hash = {key: value for key, value in plan.items() if key != "plan_hash"}
        self.assertEqual(plan["plan_hash"], canonical_hash(without_hash))
        # Whichever plan the lineage retired last, the active one must name it. A
        # literal here has to be hand-edited at every reissue and eventually is.
        previous = trust_root.RETIRED_PLANS[-1]
        self.assertEqual(plan["supersedes_plan_id"], previous["plan_id"])
        self.assertEqual(plan["supersedes_plan_hash"], previous["plan_hash"])


class V7CaptureDisabledContractTests(unittest.TestCase):
    @staticmethod
    def _plan() -> dict:
        return json.loads(config.PLAN_PATH.read_text(encoding="utf-8"))

    def test_audit_green_status_still_cannot_authorize_capture(self) -> None:
        plan = self._plan()
        self.assertEqual(plan["status"], "CAPTURE_IMPLEMENTATION_AUDIT_GREEN_NO_CAPTURE")
        self.assertFalse(plan["activation_gate"]["capture_authorized"])
        self.assertNotIn(risk_gate.CAPTURE_ACTION, plan["authorized_after_gate_green"])
        self.assertNotIn(
            "market_data_capture",
            risk_gate.PLAN_WRITE_AUTHORIZATION[plan["status"]]["write_classes"],
        )
        with self.assertRaisesRegex(risk_gate.RiskGateError, "does not authorize"):
            risk_gate.verify_plan_write_authorization(plan, "market_data_capture")

    def test_plan_preregisters_causal_and_atomic_evidence_requirements(self) -> None:
        plan = self._plan()
        evidence = plan["capture_evidence"]
        registry = plan["event_registry"]
        self.assertEqual(registry["official_t0_precision_sec"], 60)
        self.assertEqual(registry["selection_clock"], "received_at_utc")
        self.assertEqual(evidence["coverage_clock"], "received_ts")
        self.assertEqual(evidence["metadata_max_retries"], 0)
        self.assertEqual(evidence["required_probes"], ["trades", "orderbook", "ticker"])
        self.assertEqual(evidence["artifact_commit"]["manifest_creation"], "O_EXCL")
        self.assertIn("after claim", evidence["post_claim_revalidation"])
        self.assertIn("not consumed", evidence["token_mismatch_policy"])


if __name__ == "__main__":
    unittest.main()
