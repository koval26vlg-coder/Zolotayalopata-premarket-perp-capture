"""Immutable v9 rebind after the final due-window implementation correction."""

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


V8_RELATIVE_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v8.json"
V8_PATH = ROOT / V8_RELATIVE_PATH
V8_PLAN_HASH = "fb9a44f17ca2f3ffcb8f9ef87c7e9ad42684bfd80ad03dfe5ad48d05f34d223f"
V8_FILE_SHA256 = "045c614865cc0744025b93eb1ee5ef1de2d093d8680a1a9d3f2e64909839ced5"
NO_CAPTURE_STATUS = "CAPTURE_IMPLEMENTATION_AUDIT_GREEN_NO_CAPTURE"


class V9FinalRebindTests(unittest.TestCase):
    def active_plan(self) -> dict:
        return json.loads(config.PLAN_PATH.read_text(encoding="utf-8"))

    def test_v8_is_preserved_byte_identical_as_last_retired_plan(self) -> None:
        self.assertTrue(V8_PATH.is_file())
        self.assertEqual(hashlib.sha256(V8_PATH.read_bytes()).hexdigest(), V8_FILE_SHA256)
        plan = json.loads(V8_PATH.read_text(encoding="utf-8"))
        self.assertEqual(plan["plan_hash"], V8_PLAN_HASH)
        self.assertEqual(
            canonical_hash({key: value for key, value in plan.items() if key != "plan_hash"}),
            V8_PLAN_HASH,
        )
        retired = {
            str(item["path"]).replace("\\", "/"): item
            for item in trust_root.RETIRED_PLANS
        }
        self.assertIn(V8_RELATIVE_PATH, retired)
        self.assertEqual(retired[V8_RELATIVE_PATH]["plan_hash"], V8_PLAN_HASH)
        self.assertEqual(retired[V8_RELATIVE_PATH]["plan_file_sha256"], V8_FILE_SHA256)
        self.assertEqual(
            str(trust_root.RETIRED_PLANS[-1]["path"]).replace("\\", "/"),
            V8_RELATIVE_PATH,
        )

    def test_active_v9_supersedes_exact_v8_and_remains_no_capture(self) -> None:
        self.assertEqual(config.PLAN_PATH.name, "premarket-perp-capture-planonly-20260822-v9.json")
        plan = self.active_plan()
        self.assertEqual(plan["schema"], "premarket_perp_capture_planonly_v9")
        self.assertEqual(plan["plan_id"], "premarket_perp_capture_20260822_v9")
        self.assertEqual(plan["supersedes_plan_id"], "premarket_perp_capture_20260822_v8")
        self.assertEqual(plan["supersedes_plan_hash"], V8_PLAN_HASH)
        self.assertEqual(str(plan["supersedes_plan_path"]).replace("\\", "/"), V8_RELATIVE_PATH)
        self.assertEqual(plan["status"], NO_CAPTURE_STATUS)
        self.assertFalse(plan["activation_gate"]["capture_authorized"])

    def test_v9_preregisters_current_generation_freshness_and_production_refresh_cas(self) -> None:
        registry = self.active_plan()["event_registry"]
        lifecycle = registry["active_lifecycle_generation"]
        self.assertEqual(
            lifecycle["active_state_field"],
            "active_lifecycle_generations_by_venue",
        )
        self.assertEqual(
            lifecycle["high_water_field"],
            "lifecycle_generation_high_water_by_venue",
        )
        self.assertEqual(lifecycle["max_complete_metadata_refresh_age_sec"], 300)
        self.assertEqual(
            lifecycle["source_of_truth"],
            "latest_verified_mutation_receipt_carrying_last_complete_metadata_refresh_state",
        )
        self.assertTrue(lifecycle["required_for_official_attestation"])
        self.assertTrue(lifecycle["required_for_capture"])

        refresh = registry["production_refresh_contract"]
        self.assertEqual(refresh["timestamp_owner"], "WRITER_AFTER_ALL_HTTP_RESPONSES")
        self.assertEqual(refresh["caller_observed_at_override"], "FORBIDDEN")
        self.assertEqual(refresh["injected_payload_destination"], "EXPLICIT_NONPRODUCTION_PATH_ONLY")
        self.assertEqual(refresh["precommit_authority_recheck"], "UNDER_REGISTRY_LOCK")
        self.assertEqual(refresh["concurrency_control"], "RECEIPT_HEAD_COMPARE_AND_SWAP")
        self.assertEqual(refresh["empty_full_universe_response"], "ACQUISITION_FAILURE")
        self.assertEqual(
            registry["capture_authority_lineage_fields"],
            [
                "mutation_receipt_seq",
                "mutation_receipt_hash",
                "summary_content_sha256",
                "registry_authority_state_hash",
            ],
        )

    def test_v9_preregisters_writer_clock_attestation_and_manual_crash_recovery_boundary(self) -> None:
        registry = self.active_plan()["event_registry"]
        attestation = registry["official_attestation"]
        self.assertEqual(attestation["writer_clock_recheck"], "UNDER_REGISTRY_LOCK_BEFORE_APPEND")
        self.assertTrue(attestation["metadata_freshness_required"])
        self.assertEqual(attestation["current_generation_required"], True)
        self.assertEqual(attestation["precommit_authority_recheck"], "UNDER_REGISTRY_LOCK")
        self.assertEqual(
            attestation["quotation_evidence"]["forbidden_unicode_categories"],
            ["Cc", "Cf"],
        )
        self.assertEqual(attestation["announcement_url_policy"]["explicit_port"], "FORBIDDEN")

        mutation = registry["mutation_receipt_chain"]
        self.assertIn("registry_head_record_hash", mutation["anchors"])
        self.assertIn("summary_content_hash", mutation["anchors"])
        self.assertEqual(
            mutation["process_crash_recovery"],
            "FAIL_CLOSED_MANUAL_RECOVERY_NO_AUTOMATIC_ATOMICITY_CLAIM",
        )
        self.assertFalse(mutation["wal_implemented"])

    def test_v9_preregisters_causal_capture_and_exact_synthetic_boundary(self) -> None:
        evidence = self.active_plan()["capture_evidence"]
        entrypoints = evidence["entrypoint_policy"]
        self.assertEqual(
            entrypoints["run_capture"],
            "EXACT_STATIC_JSON_SYNTHETIC_FIXTURE_TRANSPORT_ONLY",
        )
        self.assertEqual(
            entrypoints["live_public_market_data"],
            "capture_event_ONLY_AFTER_GATE_TOKEN_AND_CLAIM",
        )
        self.assertEqual(
            evidence["fixed_exit_evidence_clock"],
            "first_valid_received_ts_at_or_after_target_within_one_cadence",
        )
        self.assertEqual(evidence["pre_target_exit_sample_policy"], "FORBIDDEN_NO_LOOKAHEAD")
        self.assertTrue(evidence["failed_rows_in_readiness_denominator"])
        self.assertEqual(evidence["sampling_report_clock"], "received_ts")
        self.assertEqual(
            evidence["per_request_boundary_recheck"],
            [
                "before_each_metadata_or_poll_request",
                "immediately_after_each_metadata_or_poll_response",
            ],
        )
        self.assertEqual(evidence["shared_gate_process_exit_code"], "MUST_BE_ZERO")

    def test_v9_preregisters_terminal_release_and_multi_trade_semantics(self) -> None:
        evidence = self.active_plan()["capture_evidence"]
        terminal = evidence["terminal_accounting"]
        self.assertTrue(terminal["terminal_run_record_before_claim_release"])
        self.assertEqual(
            terminal["receipt_failure"],
            "FAILED_EXCEPTION_TERMINAL_RECORD_THEN_CLAIM_RELEASE",
        )
        self.assertEqual(
            terminal["stop_request_cleanup"],
            "WHILE_OLD_RUN_STILL_OWNS_CLAIM_BEFORE_RELEASE",
        )
        trades = evidence["multi_trade_response_policy"]
        self.assertEqual(trades["okx"], "NONEMPTY_ALL_ROWS_EXACT_INSTRUMENT")
        self.assertEqual(trades["gate"], "NONEMPTY_ALL_ROWS_EXACT_CONTRACT")

    def test_runtime_verifier_loads_v9_but_capture_authorization_stays_blocked(self) -> None:
        plan = risk_gate.load_and_verify_plan()
        self.assertEqual(plan["plan_id"], "premarket_perp_capture_20260822_v9")
        self.assertNotIn(risk_gate.CAPTURE_ACTION, plan["authorized_after_gate_green"])
        with self.assertRaisesRegex(risk_gate.RiskGateError, "does not authorize"):
            risk_gate.verify_plan_write_authorization(plan, "market_data_capture")


if __name__ == "__main__":
    unittest.main()
