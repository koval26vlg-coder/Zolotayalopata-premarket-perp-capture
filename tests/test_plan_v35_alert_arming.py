"""Immutable v36 patch contract for alerting and official-t0 arming."""

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

import frozen_plan_bindings as trust_root  # noqa: E402
import plan_builder  # noqa: E402
import project_config as config  # noqa: E402
import risk_gate  # noqa: E402


V35_RELATIVE = "docs/plans/premarket-perp-capture-planonly-20260822-v35.json"
V35_PATH = ROOT / V35_RELATIVE
V35_ID = "premarket_perp_capture_20260822_v35"
V35_PLAN_HASH = "51956bf5e041f4df2424f1647c52bde438232b3f2e9303de3456e7fa98dd2950"
V35_FILE_SHA = "a82b8e52415747b4b6601af1e02efff6dd27da3f0fc4060cf7547ff15694b9e4"
V36_PATH = ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v36.json"


class V36ImmutableLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(V36_PATH.is_file(), "v36 PlanOnly must be published as a new file")
        self.plan = json.loads(V36_PATH.read_text(encoding="utf-8"))

    def test_v35_remains_byte_identical_and_is_the_exact_predecessor(self) -> None:
        self.assertEqual(hashlib.sha256(V35_PATH.read_bytes()).hexdigest(), V35_FILE_SHA)
        self.assertEqual(json.loads(V35_PATH.read_text(encoding="utf-8"))["plan_hash"], V35_PLAN_HASH)
        self.assertEqual(getattr(config, "V35_PLAN_PATH", None), V35_PATH)
        self.assertEqual(config.PLAN_PATH, V36_PATH)
        self.assertEqual(self.plan["supersedes_plan_id"], V35_ID)
        self.assertEqual(self.plan["supersedes_plan_hash"], V35_PLAN_HASH)
        self.assertEqual(self.plan["supersedes_plan_path"], V35_RELATIVE)

    def test_v36_has_a_new_identity_and_no_capture_status(self) -> None:
        self.assertEqual(plan_builder.SCHEMA, "premarket_perp_capture_planonly_v36")
        self.assertEqual(plan_builder.PLAN_ID, "premarket_perp_capture_20260822_v36")
        self.assertEqual(self.plan["schema"], plan_builder.SCHEMA)
        self.assertEqual(self.plan["plan_id"], plan_builder.PLAN_ID)
        self.assertEqual(
            self.plan["status"], risk_gate.OFFICIAL_T0_ARMING_READY_PLAN_STATUS
        )
        self.assertIs(self.plan["activation_gate"]["capture_authorized"], False)
        self.assertNotIn(
            risk_gate.CAPTURE_ACTION, self.plan["authorized_after_gate_green"]
        )
        authorization = risk_gate.PLAN_WRITE_AUTHORIZATION[self.plan["status"]]
        self.assertNotIn("market_data_capture", authorization["write_classes"])

    def test_candidate_alert_and_arming_runtime_are_sha_bound(self) -> None:
        bound = dict(config.BOUND_RUNTIME_FILES)
        self.assertEqual(bound["candidate_alert"], "src/candidate_alert.py")
        self.assertEqual(
            bound["candidate_alert_sidecar"],
            "tools/show_premarket_candidate_alert.ps1",
        )
        self.assertEqual(bound["official_t0_arming"], "src/official_t0_arming.py")
        self.assertEqual(
            bound["official_t0_arming_launcher"],
            "tools/start_premarket_official_t0_arming_visible.ps1",
        )
        self.assertEqual(
            bound["event_bound_plan_proposal"],
            "src/event_bound_plan_proposal.py",
        )

    def test_plan_preregisters_alert_arming_and_v37_proposal_contracts(self) -> None:
        alert = self.plan["candidate_alert"]
        self.assertEqual(alert["scheduler"], "EXISTING_HIDDEN_INTERACTIVE_TASK")
        self.assertEqual(alert["automatic_show_max_per_candidate"], 1)
        self.assertEqual(alert["duplicate_or_revision"], "NO_NEW_TOAST")
        self.assertEqual(
            alert["operator_review_status"],
            "READ_ONLY_VERIFIED_QUEUE_WITH_EPISODE_AND_LIFECYCLE_GENERATION",
        )
        self.assertIs(alert["capture_authorized"], False)

        arming = self.plan["official_t0_arming"]
        self.assertEqual(arming["source_class"], "OFFICIAL_ANNOUNCEMENT")
        self.assertEqual(arming["asset_class"], "CRYPTO_TOKEN")
        self.assertEqual(arming["required_precision_sec"], 1)
        self.assertEqual(arming["minimum_lead_sec"], config.CAPTURE_WINDOW_BEFORE_SEC)
        self.assertEqual(
            arming["revision_compare_and_swap"],
            "EXACT_CURRENT_ARMING_RECEIPT_HASH_REQUIRED_WHEN_ANCHOR_CHANGES",
        )
        self.assertEqual(
            arming["clock"], "FINAL_UTC_SECONDS_SAMPLED_AFTER_COMMIT_PREFLIGHT"
        )
        self.assertEqual(
            arming["registry_head_guard"],
            "RESELECT_AFTER_COMMIT_PREFLIGHT_AND_HOLD_PRODUCTION_REGISTRY_LOCK_"
            "THROUGH_RECEIPT_FSYNC",
        )
        self.assertIs(arming["capture_authorized"], False)
        self.assertIs(arming["capture_token_issued"], False)

        proposal = self.plan["event_bound_plan_proposal"]
        self.assertEqual(proposal["proposed_plan_id"], "premarket_perp_capture_20260822_v37")
        self.assertEqual(proposal["mode"], "CREATE_ONLY_PROPOSAL_NO_TRUST_ROOT_REBIND")
        self.assertIs(proposal["capture_authorized"], False)
        self.assertIs(proposal["requires_explicit_user_capture_approval"], True)
        self.assertEqual(proposal["event_anchor"], "FROZEN_V36_ARMING_RECEIPT")
        self.assertEqual(proposal["current_lifecycle_snapshot"], "REQUIRED_UNDER_V37_BEFORE_CAPTURE")
        self.assertEqual(
            proposal["current_arming_head"],
            "SUPPLIED_RECEIPT_MUST_REMAIN_DURABLE_HEAD_UNDER_ARMING_WRITER_LOCK",
        )
        self.assertEqual(
            proposal["clock"], "FINAL_UTC_SECONDS_SAMPLED_AFTER_COMMIT_PREFLIGHT"
        )

    def test_control_paths_roll_to_v36_but_alert_and_arming_namespaces_are_stable(self) -> None:
        for path in (
            config.ANNOUNCEMENT_STATE_PATH,
            config.ANNOUNCEMENT_ATTEMPTS_PATH,
            config.ANNOUNCEMENT_WATCH_CLAIM_PATH,
            config.ANNOUNCEMENT_WATCH_CLAIM_ARCHIVE,
        ):
            self.assertIn("v36", path.name)
        self.assertEqual(
            config.CANDIDATE_ALERT_LEDGER_PATH.name,
            "official-listing-candidate-alerts-v1.jsonl",
        )
        self.assertEqual(config.OFFICIAL_T0_ARMING_ROOT.name, "official-t0-v1")

    def test_active_trust_root_is_v36_and_v35_is_retired(self) -> None:
        self.assertEqual(trust_root.PLAN_ID, self.plan["plan_id"])
        self.assertEqual(trust_root.PLAN_HASH, self.plan["plan_hash"])
        retired = {item["path"]: item for item in trust_root.RETIRED_PLANS}
        self.assertEqual(retired[V35_RELATIVE]["plan_hash"], V35_PLAN_HASH)
        self.assertEqual(retired[V35_RELATIVE]["plan_file_sha256"], V35_FILE_SHA)


class V36RiskClassTests(unittest.TestCase):
    def test_new_write_classes_are_local_or_evidence_only_and_never_mint_tokens(self) -> None:
        self.assertIn("candidate_alert", config.WRITE_CLASSES)
        self.assertIn("official_t0_arming", config.WRITE_CLASSES)
        alert = config.WRITE_CLASSES["candidate_alert"]
        self.assertIs(alert["exclusive_writer_claim"], False)
        self.assertIs(alert["capture_token"], False)
        self.assertIs(alert["shared_gate_required"], False)
        self.assertIs(alert["endpoint_allow_list"], False)

        arming = config.WRITE_CLASSES["official_t0_arming"]
        self.assertIs(arming["exclusive_writer_claim"], False)
        self.assertIs(arming["capture_token"], False)
        self.assertIs(arming["shared_gate_required"], True)
        self.assertIs(arming["endpoint_allow_list"], True)


if __name__ == "__main__":
    unittest.main()
