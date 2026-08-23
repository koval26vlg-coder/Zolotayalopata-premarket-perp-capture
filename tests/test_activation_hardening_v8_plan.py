"""Preserve the immutable PlanOnly v8 checkpoint under the active v9 lineage.

v7 remains a published byte-identical checkpoint.  v8 gives the newly discovered
causal/evidence boundaries a structured PlanOnly representation while deliberately
retaining the no-capture status.  The field names below are the desired contract: a
future implementation must not satisfy these tests with equivalent but ambiguous
prose.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from canonical_hash import canonical_hash  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import project_config as config  # noqa: E402
import risk_gate  # noqa: E402


V7_RELATIVE_PATH = (
    "docs/plans/premarket-perp-capture-planonly-20260822-v7.json"
)
V7_PATH = ROOT / V7_RELATIVE_PATH
V7_PLAN_HASH = "0fb59db93f3f52a47614e080e04d59b77fbdbbc990da888b291b4cc832330e59"
V7_FILE_SHA256 = "6ac94a64be7a83835b764115d1805f05d2194ac060c4b4df7ddfb768bb5ab75e"
V8_RELATIVE_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v8.json"
V8_PATH = ROOT / V8_RELATIVE_PATH
NO_CAPTURE_STATUS = "CAPTURE_IMPLEMENTATION_AUDIT_GREEN_NO_CAPTURE"


def _active_plan() -> dict[str, object]:
    return json.loads(config.PLAN_PATH.read_text(encoding="utf-8"))


def _published_v8_plan() -> dict[str, object]:
    return json.loads(V8_PATH.read_text(encoding="utf-8"))


class V8ImmutableLineageTests(unittest.TestCase):
    def test_v7_is_preserved_exactly_and_retired_before_v8(self) -> None:
        self.assertTrue(V7_PATH.is_file(), "the published v7 PlanOnly must remain on disk")
        self.assertEqual(hashlib.sha256(V7_PATH.read_bytes()).hexdigest(), V7_FILE_SHA256)
        v7 = json.loads(V7_PATH.read_text(encoding="utf-8"))
        self.assertEqual(v7["plan_hash"], V7_PLAN_HASH)
        self.assertEqual(
            canonical_hash({key: value for key, value in v7.items() if key != "plan_hash"}),
            V7_PLAN_HASH,
        )

        retired = {
            str(item["path"]).replace("\\", "/"): item
            for item in trust_root.RETIRED_PLANS
        }
        self.assertIn(V7_RELATIVE_PATH, retired)
        self.assertEqual(retired[V7_RELATIVE_PATH]["schema"], "premarket_perp_capture_planonly_v7")
        self.assertEqual(retired[V7_RELATIVE_PATH]["plan_id"], "premarket_perp_capture_20260822_v7")
        self.assertEqual(retired[V7_RELATIVE_PATH]["plan_hash"], V7_PLAN_HASH)
        self.assertEqual(retired[V7_RELATIVE_PATH]["plan_file_sha256"], V7_FILE_SHA256)
        retired_paths = tuple(
            str(item["path"]).replace("\\", "/") for item in trust_root.RETIRED_PLANS
        )
        self.assertLess(retired_paths.index(V7_RELATIVE_PATH), len(retired_paths) - 1)

    def test_active_identity_is_new_v9_and_supersedes_exact_v8(self) -> None:
        plan = _active_plan()
        self.assertEqual(config.PLAN_PATH.name, "premarket-perp-capture-planonly-20260822-v9.json")
        self.assertEqual(trust_root.ACTIVE_PLAN["schema"], "premarket_perp_capture_planonly_v9")
        self.assertEqual(trust_root.ACTIVE_PLAN["plan_id"], "premarket_perp_capture_20260822_v9")
        self.assertEqual(plan["schema"], "premarket_perp_capture_planonly_v9")
        self.assertEqual(plan["plan_id"], "premarket_perp_capture_20260822_v9")
        self.assertEqual(plan["supersedes_plan_id"], "premarket_perp_capture_20260822_v8")
        self.assertEqual(
            plan["supersedes_plan_hash"],
            "fb9a44f17ca2f3ffcb8f9ef87c7e9ad42684bfd80ad03dfe5ad48d05f34d223f",
        )
        self.assertEqual(
            str(plan["supersedes_plan_path"]).replace("\\", "/"),
            "docs/plans/premarket-perp-capture-planonly-20260822-v8.json",
        )
        self.assertEqual(plan["status"], NO_CAPTURE_STATUS)
        self.assertFalse(plan["activation_gate"]["capture_authorized"])

    def test_v9_loads_through_runtime_verifier_and_remains_capture_disabled(self) -> None:
        plan = risk_gate.load_and_verify_plan()
        self.assertEqual(plan["plan_id"], "premarket_perp_capture_20260822_v9")
        self.assertEqual(plan["status"], NO_CAPTURE_STATUS)
        self.assertNotIn(risk_gate.CAPTURE_ACTION, plan["authorized_after_gate_green"])
        with self.assertRaisesRegex(risk_gate.RiskGateError, "does not authorize"):
            risk_gate.verify_plan_write_authorization(plan, "market_data_capture")


class V8StructuredPlanClausesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = _published_v8_plan()

    def _mapping(self, parent: Mapping[str, object], key: str) -> Mapping[str, object]:
        value = parent.get(key)
        self.assertIsInstance(value, Mapping, f"PlanOnly clause {key!r} must be structured")
        return value  # type: ignore[return-value]

    def test_due_window_and_both_sampling_cadences_are_exact(self) -> None:
        registry = self._mapping(self.plan, "event_registry")
        due = self._mapping(registry, "capture_due_window")
        target = "official_spot_t0 - window_before_t0_sec"
        self.assertEqual(due.get("target"), target)
        self.assertEqual(due.get("early_grace_sec"), config.CAPTURE_LAUNCH_EARLY_GRACE_SEC)
        self.assertEqual(due.get("late_grace_sec"), config.CAPTURE_LAUNCH_LATE_GRACE_SEC)
        self.assertEqual(
            due.get("eligibility_interval"),
            "target - early_grace_sec <= now_ts <= target + late_grace_sec",
        )

        evidence = self._mapping(self.plan, "capture_evidence")
        cadence = self._mapping(evidence, "sampling_cadence_sec")
        self.assertEqual(cadence.get("outside_burst"), dict(config.PROBE_CADENCE_SEC))
        self.assertEqual(cadence.get("burst"), dict(config.BURST_CADENCE_SEC))
        self.assertEqual(cadence.get("burst_half_width_sec"), config.BURST_HALF_WIDTH_SEC)

    def test_official_quote_is_stored_verbatim_without_normalization(self) -> None:
        registry = self._mapping(self.plan, "event_registry")
        attestation = self._mapping(registry, "official_attestation")
        quotation = self._mapping(attestation, "quotation_evidence")
        self.assertEqual(quotation.get("storage"), "VERBATIM_UTF8")
        self.assertEqual(quotation.get("normalization"), "FORBIDDEN")
        self.assertTrue(quotation.get("time_fragment_must_be_verbatim_substring"))
        self.assertTrue(quotation.get("symbol_fragment_must_be_verbatim_substring"))

    def test_registry_mutations_have_immutable_local_receipt_chain(self) -> None:
        registry = self._mapping(self.plan, "event_registry")
        chain = self._mapping(registry, "mutation_receipt_chain")
        self.assertEqual(chain.get("creation"), "O_EXCL")
        self.assertTrue(chain.get("immutable"))
        self.assertEqual(chain.get("link_field"), "previous_mutation_receipt_hash")
        self.assertEqual(
            chain.get("failure_recovery_boundary"),
            "LOCAL_CRASH_RECOVERY_EVIDENCE_ONLY_NOT_CRYPTOGRAPHIC_AUTHENTICITY",
        )
        registry_text = json.dumps(registry, ensure_ascii=False).lower()
        self.assertNotIn("signed summary", registry_text)

    def test_active_lifecycle_generation_is_an_explicit_capture_precondition(self) -> None:
        registry = self._mapping(self.plan, "event_registry")
        lifecycle = self._mapping(registry, "active_lifecycle_generation")
        self.assertEqual(
            lifecycle.get("identity_fields"),
            ["venue", "premarket_contract_id", "lifecycle_generation"],
        )
        self.assertTrue(lifecycle.get("required_for_official_attestation"))
        self.assertTrue(lifecycle.get("required_for_capture"))
        self.assertEqual(
            lifecycle.get("source_of_truth"),
            "latest_complete_metadata_refresh_mutation_receipt",
        )

    def test_live_entrypoint_is_capture_event_and_run_capture_is_synthetic_only(self) -> None:
        evidence = self._mapping(self.plan, "capture_evidence")
        entrypoints = self._mapping(evidence, "entrypoint_policy")
        self.assertEqual(entrypoints.get("run_capture"), "SYNTHETIC_OFFLINE_FETCH_ONLY")
        self.assertEqual(
            entrypoints.get("live_public_market_data"),
            "capture_event_ONLY_AFTER_GATE_TOKEN_AND_CLAIM",
        )

    def test_gate_orderbook_without_echo_binds_validated_request_identity(self) -> None:
        evidence = self._mapping(self.plan, "capture_evidence")
        identity = self._mapping(evidence, "gate_orderbook_request_identity")
        self.assertTrue(identity.get("required_when_response_contract_absent"))
        self.assertEqual(identity.get("source"), "validated_on_wire_request_query.contract")
        self.assertEqual(identity.get("must_equal"), "capture_job.premarket_contract_id")
        self.assertTrue(identity.get("persist_in_sample"))

    def test_manifest_is_exclusive_and_receipt_rehashes_samples(self) -> None:
        evidence = self._mapping(self.plan, "capture_evidence")
        commit = self._mapping(evidence, "artifact_commit")
        self.assertEqual(commit.get("manifest_creation"), "O_EXCL")
        self.assertTrue(commit.get("manifest_immutable"))
        self.assertTrue(commit.get("receipt_rehashes_samples"))
        self.assertEqual(
            commit.get("receipt_samples_hash_must_equal"),
            "manifest.output_sha256",
        )


if __name__ == "__main__":
    unittest.main()
