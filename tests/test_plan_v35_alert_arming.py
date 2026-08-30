"""Immutable v38 no-capture recovery and fixture-rehearsal contract."""

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


V37_RELATIVE = "docs/plans/premarket-perp-capture-planonly-20260822-v37.json"
V37_PATH = ROOT / V37_RELATIVE
V37_ID = "premarket_perp_capture_20260822_v37"
V37_PLAN_HASH = "9671b54040a3eabb21a4a5a3bf455ed5837fe13d82f51b5ca22dc5bbc6f6ffcf"
V37_FILE_SHA = "b8519c77cf352033c683908d011e470f94b38437dd2dfe9b8ddea297b5fd2463"
V38_PATH = ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v38.json"


class V38ImmutableLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(V38_PATH.is_file(), "v38 PlanOnly must be published as a new file")
        self.plan = json.loads(V38_PATH.read_text(encoding="utf-8"))

    def test_v37_remains_byte_identical_and_is_the_exact_predecessor(self) -> None:
        self.assertEqual(hashlib.sha256(V37_PATH.read_bytes()).hexdigest(), V37_FILE_SHA)
        self.assertEqual(json.loads(V37_PATH.read_text(encoding="utf-8"))["plan_hash"], V37_PLAN_HASH)
        self.assertEqual(getattr(config, "V37_PLAN_PATH", None), V37_PATH)
        self.assertEqual(config.V38_PLAN_PATH, V38_PATH)
        self.assertEqual(self.plan["supersedes_plan_id"], V37_ID)
        self.assertEqual(self.plan["supersedes_plan_hash"], V37_PLAN_HASH)
        self.assertEqual(self.plan["supersedes_plan_path"], V37_RELATIVE)

    def test_v38_has_a_new_identity_and_no_capture_status(self) -> None:
        self.assertEqual(self.plan["schema"], "premarket_perp_capture_planonly_v38")
        self.assertEqual(self.plan["plan_id"], "premarket_perp_capture_20260822_v38")
        self.assertEqual(
            self.plan["status"], risk_gate.OFFICIAL_T0_ARMING_READY_PLAN_STATUS
        )
        self.assertIs(self.plan["activation_gate"]["capture_authorized"], False)
        self.assertNotIn(
            risk_gate.CAPTURE_ACTION, self.plan["authorized_after_gate_green"]
        )
        self.assertNotIn(
            "run one deterministic temporary no-authority fixture rehearsal",
            self.plan["authorized_after_gate_green"],
        )
        authorization = risk_gate.PLAN_WRITE_AUTHORIZATION[self.plan["status"]]
        self.assertNotIn("market_data_capture", authorization["write_classes"])

    def test_rehearsal_recovery_and_arming_runtime_are_sha_bound(self) -> None:
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
        self.assertEqual(bound["fixture_rehearsal"], "src/fixture_rehearsal.py")
        self.assertEqual(
            bound["fixture_rehearsal_launcher"],
            "tools/start_premarket_fixture_rehearsal.ps1",
        )

    def test_plan_preregisters_alert_arming_and_v39_proposal_contracts(self) -> None:
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
        self.assertEqual(
            arming["stale_lock_recovery"],
            "LOSSLESS_ARCHIVE_AND_SINGLE_REACQUIRE_ONLY_FOR_CONCLUSIVELY_DEAD_SAME_HOST_PID",
        )
        self.assertEqual(
            arming["release_ownership"], "EXACT_LOCK_BYTES_REQUIRED_BEFORE_UNLINK"
        )
        self.assertEqual(
            arming["post_archive_crash_resume"],
            "EXISTING_ARCHIVE_ACCEPTED_ONLY_IF_SAME_NON_SYMLINK_INODE",
        )

        proposal = self.plan["event_bound_plan_proposal"]
        self.assertEqual(proposal["proposed_plan_id"], "premarket_perp_capture_20260822_v39")
        self.assertEqual(proposal["mode"], "CREATE_ONLY_PROPOSAL_NO_TRUST_ROOT_REBIND")
        self.assertIs(proposal["capture_authorized"], False)
        self.assertIs(proposal["requires_explicit_user_capture_approval"], True)
        self.assertEqual(proposal["event_anchor"], "FROZEN_V38_ARMING_RECEIPT")
        self.assertEqual(proposal["current_lifecycle_snapshot"], "REQUIRED_UNDER_V39_BEFORE_CAPTURE")
        self.assertEqual(
            proposal["current_arming_head"],
            "SUPPLIED_RECEIPT_MUST_REMAIN_DURABLE_HEAD_UNDER_ARMING_WRITER_LOCK",
        )
        self.assertEqual(
            proposal["clock"], "FINAL_UTC_SECONDS_SAMPLED_AFTER_COMMIT_PREFLIGHT"
        )
        self.assertEqual(
            proposal["publication"],
            "FSYNC_NONAUTHORITATIVE_STAGE_THEN_ATOMIC_NO_REPLACE_LINK",
        )
        self.assertEqual(
            proposal["interrupted_stage_recovery"],
            "LOSSLESS_ARCHIVE_THEN_RETRY_UNDER_CURRENT_ARMING_HEAD_LOCK",
        )
        self.assertEqual(
            proposal["valid_existing_final"],
            "STRICT_IDEMPOTENT_READBACK_NO_REWRITE",
        )
        self.assertEqual(
            proposal["post_archive_crash_resume"],
            "EXISTING_ARCHIVE_ACCEPTED_ONLY_IF_SAME_NON_SYMLINK_INODE",
        )

        rehearsal = self.plan["fixture_rehearsal"]
        self.assertEqual(rehearsal["runtime"], "src/fixture_rehearsal.py")
        self.assertEqual(rehearsal["network"], False)
        self.assertEqual(rehearsal["production_writes"], False)
        self.assertEqual(rehearsal["toast"], False)
        self.assertEqual(rehearsal["capture_authorized"], False)
        self.assertEqual(
            rehearsal["preflight"],
            "LAUNCHER_AND_RUNTIME_ACTIVE_PLAN_SHA_AND_CAPABILITY_SCAN_BEFORE_TEMPORARY_WRITE",
        )

    def test_control_paths_roll_to_v42_but_alert_and_arming_namespaces_are_stable(self) -> None:
        for path in (
            config.ANNOUNCEMENT_STATE_PATH,
            config.ANNOUNCEMENT_ATTEMPTS_PATH,
            config.ANNOUNCEMENT_WATCH_CLAIM_PATH,
            config.ANNOUNCEMENT_WATCH_CLAIM_ARCHIVE,
        ):
            self.assertIn("v42", path.name)
        self.assertEqual(
            config.CANDIDATE_ALERT_LEDGER_PATH.name,
            "official-listing-candidate-alerts-v1.jsonl",
        )
        self.assertEqual(config.OFFICIAL_T0_ARMING_ROOT.name, "official-t0-v1")

    def test_v38_is_retired_and_active_trust_root_is_v42(self) -> None:
        self.assertEqual(trust_root.PLAN_ID, "premarket_perp_capture_20260822_v42")
        retired = {item["path"]: item for item in trust_root.RETIRED_PLANS}
        self.assertEqual(retired[V37_RELATIVE]["plan_hash"], V37_PLAN_HASH)
        self.assertEqual(retired[V37_RELATIVE]["plan_file_sha256"], V37_FILE_SHA)
        v38_relative = "docs/plans/premarket-perp-capture-planonly-20260822-v38.json"
        self.assertEqual(retired[v38_relative]["plan_hash"], self.plan["plan_hash"])


class V38RiskClassTests(unittest.TestCase):
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
