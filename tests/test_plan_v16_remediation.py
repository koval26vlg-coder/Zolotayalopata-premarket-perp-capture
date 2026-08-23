"""Immutable v17 contract for the registry, replay, and quarantine remediation."""

from __future__ import annotations

import copy
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
import event_registry  # noqa: E402
import plan_builder  # noqa: E402
import project_config as config  # noqa: E402
import registry_quarantine  # noqa: E402
import replay  # noqa: E402
import risk_gate  # noqa: E402


V15_PATH = ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v15.json"
V15_PLAN_HASH = "41accb18028f6ccbee59264c8564d36ff9efd3d2c28aea9119e3a2d2741a062c"
V15_FILE_SHA256 = "47a0f340b9352b3bb9897a44e1d278f8d96e2a4d33ce5eb3339821f7f3d3bdd6"
V16_PATH = ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v16.json"
V16_PLAN_HASH = "98cbca5522753bd511ca348f2fba60134bfb8a45ed3cb96607b0b5aadb42cd4a"
V16_FILE_SHA256 = "5efd17a44bf307e9e90d6a581c515d07761c6e62644ce4e1ceae6b09d5246e48"


def _retired(path):
    """Find a published plan in the lineage by name.

    Positional lookup said 'the one before last', which was true only while v17 was
    current; every reissue shifted it and the assertion silently began describing a
    different plan."""
    for item in trust_root.RETIRED_PLANS:
        if str(item["path"]).replace(chr(92), "/").endswith(path.name):
            return item
    raise AssertionError(f"{path.name} is not in the lineage")


class ImmutableV17IdentityTests(unittest.TestCase):
    def test_v15_is_preserved_before_v16(self) -> None:
        self.assertEqual(hashlib.sha256(V15_PATH.read_bytes()).hexdigest(), V15_FILE_SHA256)
        self.assertEqual(json.loads(V15_PATH.read_text(encoding="utf-8"))["plan_hash"], V15_PLAN_HASH)
        previous = _retired(V15_PATH)
        self.assertEqual(
            previous["plan_id"],
            json.loads(V15_PATH.read_text(encoding="utf-8"))["plan_id"],
        )
        self.assertEqual(previous["plan_hash"], V15_PLAN_HASH)
        self.assertEqual(previous["plan_file_sha256"], V15_FILE_SHA256)

    def test_v16_is_preserved_as_the_immediate_predecessor(self) -> None:
        self.assertEqual(hashlib.sha256(V16_PATH.read_bytes()).hexdigest(), V16_FILE_SHA256)
        self.assertEqual(json.loads(V16_PATH.read_text(encoding="utf-8"))["plan_hash"], V16_PLAN_HASH)
        previous = _retired(V16_PATH)
        self.assertEqual(
            previous["plan_id"],
            json.loads(V16_PATH.read_text(encoding="utf-8"))["plan_id"],
        )
        self.assertEqual(previous["plan_hash"], V16_PLAN_HASH)
        self.assertEqual(previous["plan_file_sha256"], V16_FILE_SHA256)

    def test_builder_issues_the_active_identity_without_capture_authority(self) -> None:
        plan = plan_builder.build_plan("2026-08-23T18:00:00.000Z")
        # The identity is whatever the trust root pins today. What must hold at
        # every reissue is that the builder agrees with it and grants no capture.
        self.assertEqual(plan["schema"], trust_root.ACTIVE_PLAN["schema"])
        self.assertEqual(plan["plan_id"], trust_root.PLAN_ID)
        # Whatever the lineage retired last, not a fixed version: this assertion
        # was about v17 superseding v16 and had to be true of every reissue after it.
        self.assertEqual(
            plan["supersedes_plan_hash"], trust_root.RETIRED_PLANS[-1]["plan_hash"]
        )
        self.assertEqual(plan["status"], risk_gate.REGISTRY_QUARANTINE_PLAN_STATUS)
        self.assertFalse(plan["activation_gate"]["capture_authorized"])
        self.assertNotIn(risk_gate.CAPTURE_ACTION, plan["authorized_after_gate_green"])


class RegistryV17PlanContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = plan_builder.build_plan("2026-08-23T18:00:00.000Z")
        cls.registry = cls.plan["event_registry"]

    def test_registry_v3_preserves_the_exact_v2_projection(self) -> None:
        self.assertEqual(self.registry["schema"], "premarket_perp_event_registry_v3")
        self.assertEqual(self.registry["path"], "docs/registry/listing-events-v3.jsonl")
        legacy = self.registry["immutable_v2_projection"]
        self.assertEqual(legacy["path"], "docs/registry/listing-events-v2.jsonl")
        self.assertEqual(
            legacy["registry_sha256"],
            "fd3b864bc4b1b311b49a904246edd8980008ab5f1830df9042087df9619bc9a4",
        )
        self.assertEqual(
            legacy["registry_head_record_hash"],
            "7d3459943cf2122c0eb39a27452a4ab28eb41a08ceb61e19011730e14d6e8696",
        )
        self.assertEqual(
            legacy["mutation_receipt_hash"],
            "d86913b6af93d3487ef3cdbe09a3b47f3519188eea65ebf2f18aaa8fd5976282",
        )
        self.assertEqual(
            legacy["summary_sha256"], event_registry.LEGACY_V2_SUMMARY_SHA256
        )
        self.assertEqual(
            legacy["mutation_receipt_file_sha256"],
            event_registry.LEGACY_V2_MUTATION_RECEIPT_FILE_SHA256,
        )

    def test_asset_class_and_capture_eligibility_are_explicit(self) -> None:
        identity = self.registry["asset_identity"]
        self.assertEqual(
            identity["classes"],
            [
                "CRYPTO_TOKEN",
                "EQUITY_ISSUER",
                "TOKENIZED_EQUITY",
                "TRADFI_OTHER",
                "UNCLASSIFIED",
            ],
        )
        self.assertEqual(identity["capture_eligible_asset_class"], "CRYPTO_TOKEN")
        self.assertEqual(identity["all_other_classes"], "DESCRIPTIVE_ONLY")
        self.assertEqual(identity["ambiguous_identity"], "FAIL_CLOSED_UNCLASSIFIED")
        self.assertEqual(
            self.registry["venue_metadata_semantics"]["bybit"]["asset_classification"],
            (
                "symbolType=stock => EQUITY_ISSUER; symbolType in "
                "{commodity,forex,etf} => TRADFI_OTHER; "
                "symbolType=innovation => CRYPTO_TOKEN; "
                "missing/empty/unknown => UNCLASSIFIED"
            ),
        )
        self.assertEqual(
            self.registry["venue_metadata_semantics"]["gate"]["prelaunch_predicate"],
            (
                "is_pre_market=true AND in_delisting!=true AND "
                "status in {prelaunch,trading}"
            ),
        )
        self.assertEqual(
            self.registry["venue_metadata_semantics"]["gate"]["capture_identity_policy"],
            "MISSING_OR_UNVERIFIED_CONTRACT_TYPE_IS_UNCLASSIFIED_DESCRIPTIVE_ONLY",
        )

    def test_all_five_discovery_surfaces_and_cross_surface_transition_are_bound(self) -> None:
        surfaces = self.registry["discovery_surfaces"]
        self.assertEqual(
            [surface["surface_id"] for surface in surfaces],
            [surface.surface_id for surface in event_registry.SURFACES],
        )
        self.assertEqual(
            [surface["query_params"] for surface in surfaces],
            [surface.params for surface in event_registry.SURFACES],
        )
        self.assertEqual(
            [surface["endpoint_url"] for surface in surfaces],
            [surface.url for surface in event_registry.SURFACES],
        )
        self.assertEqual(
            [surface["rows_path"] for surface in surfaces],
            [list(surface.rows_path) for surface in event_registry.SURFACES],
        )
        self.assertEqual(
            [surface["native_id_field"] for surface in surfaces],
            [surface.native_id_field for surface in event_registry.SURFACES],
        )
        self.assertEqual(
            [surface["expected_instrument_type"] for surface in surfaces],
            [surface.expected_instrument_type for surface in event_registry.SURFACES],
        )
        authority = self.registry["relevant_identity_authority"]
        self.assertEqual(authority["retention_scope"], "ALL_RELEVANT_IDENTITIES_PER_SURFACE")
        self.assertEqual(authority["missing_tracked_identity"], "ACQUISITION_FAILURE_NO_MUTATION")
        self.assertEqual(authority["bybit_cross_surface_trading"], "TERMINAL_TRANSITION")
        self.assertEqual(authority["okx_cross_surface_xperp"], "TERMINAL_TRANSITION")
        self.assertEqual(authority["okx_cross_surface_normal"], "TERMINAL_TRANSITION")
        self.assertEqual(authority["untracked_terminal_row"], "NO_GENERATION_ALLOCATION")
        self.assertEqual(
            authority["bybit_terminal_status_surface"],
            "NOT_PREREGISTERED_IN_V17",
        )
        self.assertEqual(
            authority["bybit_unobserved_cancelled_or_closed"],
            "MISSING_TRACKED_IDENTITY_ACQUISITION_FAILURE_NO_INFERENCE",
        )
        okx = self.registry["venue_metadata_semantics"]["okx"]
        self.assertEqual(
            okx["prelaunch_predicate"],
            "ruleType=pre_market AND state in {preopen,live}",
        )
        self.assertEqual(okx["xperp"], "CROSS_SURFACE_TERMINAL_TRANSITION")
        self.assertEqual(okx["normal"], "CROSS_SURFACE_TERMINAL_TRANSITION")
        self.assertEqual(
            okx["terminal_state"],
            {"expired": "DELISTED", "suspend": "DELISTED"},
        )
        self.assertEqual(
            self.registry["venue_metadata_semantics"]["gate"]["terminal_predicates"],
            {
                "status": {
                    "delisting": "DELISTING",
                    "delisted": "DELISTED",
                    "cancelled": "CANCELLED",
                    "canceled": "CANCELLED",
                },
                "in_delisting_true": "DELISTING",
                "transitioned_standard": (
                    "status=trading AND was_tracked=true AND is_pre_market=false"
                ),
            },
        )
        self.assertEqual(
            self.registry["lifecycle_states"],
            [
                event_registry.LIFECYCLE_SCHEDULED,
                event_registry.LIFECYCLE_ACTIVE_PREMARKET,
                event_registry.LIFECYCLE_TRANSITION_SCHEDULED,
                event_registry.LIFECYCLE_TRANSITIONED_STANDARD,
                event_registry.LIFECYCLE_CANCELLED,
                event_registry.LIFECYCLE_DELISTING,
                event_registry.LIFECYCLE_DELISTED,
                event_registry.LIFECYCLE_UNKNOWN,
            ],
        )
        refresh = self.registry["production_refresh_contract"]
        self.assertEqual(
            refresh["required_nonempty_full_universe_surfaces"],
            list(config.FULL_UNIVERSE_SURFACE_IDS),
        )
        self.assertEqual(
            refresh["legitimately_empty_surfaces"],
            list(config.LEGITIMATELY_EMPTY_SURFACE_IDS),
        )
        self.assertEqual(
            refresh["retention_scope"],
            "PER_REQUIRED_FULL_UNIVERSE_SURFACE",
        )

    def test_event_clocks_are_never_collapsed(self) -> None:
        self.assertEqual(
            self.registry["timestamp_kinds"],
            [
                "premarket_contract_launch_ts",
                "official_spot_t0",
                "first_trade_ts",
                "transition_ts",
                "contract_created_ts",
            ],
        )
        self.assertEqual(
            self.registry["timestamp_producers"]["first_trade_ts"],
            "RESERVED_NO_REGISTRY_PRODUCER_CAPTURE_TRADES_SEPARATE",
        )
        self.assertIn(
            "DETECTION_TIME_PROXY",
            self.registry["timestamp_producers"]["transition_ts"],
        )
        self.assertEqual(self.registry["acceptance_anchor"]["timestamp_kind"], "official_spot_t0")
        self.assertEqual(self.registry["proxy_policy"]["evidence_use"], "DESCRIPTIVE_ONLY")
        self.assertFalse(self.registry["proxy_policy"]["capture_eligible"])
        self.assertFalse(
            self.registry["seconds_grade_readiness"][
                "current_official_producer_capable"
            ]
        )
        self.assertEqual(
            self.registry["seconds_grade_readiness"]["current_result"],
            "DESCRIPTIVE_ONLY_PRECISION_GT_ONE_SECOND",
        )
        self.assertEqual(
            self.registry["venue_metadata_semantics"]["gate"]["launch_time"],
            "CONTRACT_EXPIRY_NOT_TRADING_LAUNCH",
        )
        self.assertEqual(
            self.registry["venue_metadata_semantics"]["gate"][
                "terminal_predicates"
            ],
            {
                "status": {
                    "delisting": "DELISTING",
                    "delisted": "DELISTED",
                    "cancelled": "CANCELLED",
                    "canceled": "CANCELLED",
                },
                "in_delisting_true": "DELISTING",
                "transitioned_standard": (
                    "status=trading AND was_tracked=true AND is_pre_market=false"
                ),
            },
        )
        self.assertEqual(
            self.registry["terminal_lifecycle_evidence"],
            {
                "storage": "APPEND_ONLY_TIMESTAMP_OBSERVATION",
                "summary_terminal_ids": (
                    "PREVIOUS_REFRESH_CLASSIFICATION_MEMORY_ONLY_NOT_REQUIRED_"
                    "NOT_ACTIVE_NOT_HIGH_WATER"
                ),
                "classification_memory_scope": (
                    "LAST_COMPLETE_REFRESH_READ_ON_NEXT_REFRESH_ONLY_"
                    "NOT_RELEVANT_NOT_COMPLETENESS"
                ),
                "venue_timestamp": "USE_WHEN_EXPLICIT",
                "missing_venue_timestamp": "DETECTION_TIME_PROXY_DESCRIPTIVE_ONLY",
                "terminal_phase_field": "lifecycle_phase",
                "simultaneous_surface_precedence": (
                    "EXACT_VENUE_TRANSITION_TIMESTAMP_WINS_OVER_DETECTION_PROXY"
                ),
            },
        )
        self.assertEqual(
            self.registry["cross_surface_conflict_policy"],
            {
                "terminal_vs_active": (
                    "EXPLICIT_TERMINAL_WINS_OVER_STALE_ACTIVE_ROW"
                ),
                "simultaneous_terminal_evidence": (
                    "EXACT_VENUE_TIMESTAMP_WINS_OVER_DETECTION_PROXY"
                ),
                "equal_strength_tie_break": "SURFACE_DECLARATION_ORDER",
            },
        )

    def test_capture_lineage_uses_historical_prefix_not_current_head(self) -> None:
        lineage = self.registry["capture_lineage_verification"]
        self.assertEqual(lineage["registry_bytes"], "EXACT_HISTORICAL_PREFIX")
        self.assertEqual(lineage["mutation_receipt"], "EXACT_SEQ_AND_HASH")
        self.assertEqual(lineage["official_record"], "UNSUPERSEDED_OFFICIAL_CRYPTO_ANCHOR")
        self.assertEqual(
            lineage["official_source_url_and_identity"],
            "EXACT_OFFICIAL_RECORD_MATCH",
        )
        self.assertEqual(lineage["current_head_substitution"], "FORBIDDEN")


class ReplayAndQuarantineV17PlanContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = plan_builder.build_plan("2026-08-23T18:00:00.000Z")

    def test_replay_is_causal_descriptive_and_non_executional(self) -> None:
        replay_contract = self.plan["replay_evidence"]
        self.assertEqual(replay_contract["selection_clock"], "received_ts")
        self.assertEqual(replay_contract["target_selection"], "FIRST_VALID_AT_OR_AFTER_WITHIN_ONE_CADENCE")
        self.assertEqual(replay_contract["pre_target_fallback"], "FORBIDDEN")
        self.assertEqual(replay_contract["interpolation_or_bracketing"], "FORBIDDEN")
        self.assertEqual(replay_contract["exchange_ts_use"], "FRESHNESS_FILTER_ONLY")
        self.assertEqual(replay_contract["output"], "DESCRIPTIVE_GROSS_BBO_MARKOUT_ONLY")
        self.assertFalse(replay_contract["acceptance_capable"])
        self.assertFalse(replay_contract["execution_or_fill_model"])
        self.assertIn(
            "CAPTURE_AUTHORIZED_SELECTED_PLAN",
            replay_contract["integrity"],
        )
        self.assertIn(
            "SELECTED_PLAN_CAPTURE_ROOT",
            replay_contract["integrity"],
        )
        self.assertEqual(replay_contract["production_evidence_class"], "CAUSAL_REPLAY_INPUT_READY")
        self.assertEqual(
            replay_contract["synthetic_mode"], replay.SYNTHETIC_EVIDENCE_MODE
        )
        self.assertEqual(replay_contract["synthetic_mode_selection"], "EXPLICIT_REQUIRED")
        self.assertEqual(
            replay_contract["fixed_exit_offsets_sec"],
            list(config.PRIMARY_EXIT_OFFSETS_SEC),
        )
        self.assertEqual(
            replay_contract["fixed_entry_lead_sec"],
            config.PRIMARY_ENTRY_LEAD_SEC,
        )
        self.assertEqual(
            self.plan["capture_evidence"]["fixed_entry_evidence_clock"],
            "first_valid_received_ts_at_or_after_target_within_one_cadence",
        )
        self.assertEqual(
            replay_contract["report_readiness"],
            "SEALED_CAPTURE_READY_AND_ENTRY_AND_ALL_FIXED_EXITS",
        )

    def test_probe_schema_limitations_are_preregistered(self) -> None:
        evidence = self.plan["capture_evidence"]
        probes = evidence["venue_probe_authority"]
        self.assertEqual(
            probes["okx_orderbook_instrument_identity"],
            "EXACT_BOUND_REQUEST_RESPONSE_DOES_NOT_ECHO_INST_ID",
        )
        self.assertEqual(
            probes["gate_ticker_exchange_timestamp"],
            "ABSENT_FROM_PAYLOAD_OPTIONAL_DESCRIPTIVE_ONLY",
        )
        self.assertEqual(
            evidence["required_probes_by_venue"]["gate"],
            ["trades", "orderbook"],
        )

    def test_quarantine_is_a_fail_closed_local_transaction(self) -> None:
        quarantine = self.plan["registry_quarantine"]
        self.assertEqual(quarantine["archive_schema"], registry_quarantine.ARCHIVE_SCHEMA)
        self.assertEqual(quarantine["state_schema"], registry_quarantine.STATE_SCHEMA)
        self.assertEqual(quarantine["result_schema"], registry_quarantine.RESULT_SCHEMA)
        self.assertEqual(
            quarantine["receipt_archive_schema"],
            registry_quarantine.RECEIPT_ARCHIVE_SCHEMA,
        )
        self.assertEqual(quarantine["network_requests"], "FORBIDDEN")
        self.assertEqual(quarantine["operator_cas"], "EXACT_SOURCE_BYTES_AND_NAMES")
        self.assertEqual(
            quarantine["publish_order"],
            [
                "PREPARED_STAGED_DURABLE",
                "COMMIT_PREFLIGHT_AND_SOURCE_CAS",
                "GLOBAL_WRITER_CLAIM_ACQUIRED_AND_SOURCE_CAS_RECHECKED",
                "ARCHIVE_PUBLISHED_DURABLE_SAME_VOLUME_MOVE",
                "ARCHIVE_EXACT_ENTRY_SET_AND_FULL_READBACK_VERIFIED",
                "ARCHIVE_DURABLE_STATE",
                "MUTATION_RECEIPTS_MOVED_TO_RETAINED_TOMBSTONE_IF_PRESENT",
                "SUMMARY_MOVED_TO_RETAINED_TOMBSTONE_IF_PRESENT",
                "REGISTRY_MOVED_TO_RETAINED_TOMBSTONE_LAST",
                "SOURCE_DEACTIVATED_STATE_BINDS_TOMBSTONES",
                "TERMINAL_BOUNDARY_VERIFIED",
                "GLOBAL_WRITER_CLAIM_RELEASED",
                "TERMINAL_BOUNDARY_REVERIFIED",
                "REGISTRY_LOCK_MOVED_TO_TERMINAL_PROOF_FINAL",
            ],
        )
        self.assertEqual(
            quarantine["publish_order"][-1],
            "REGISTRY_LOCK_MOVED_TO_TERMINAL_PROOF_FINAL",
        )
        self.assertEqual(
            quarantine["registry_deactivated"],
            "LAST_SOURCE_COMPONENT_RETAINED_AS_TOMBSTONE",
        )
        self.assertEqual(
            quarantine["lock"]["release_order"],
            [
                "GLOBAL_MARKET_WRITER_CLAIM",
                "REGISTRY_LOCK_MOVED_TO_TERMINAL_PROOF_FINAL_FALLIBLE_IO",
            ],
        )
        self.assertEqual(
            quarantine["receipt_archival"]["round_trip"],
            "EXACT_BYTES_REQUIRED",
        )
        self.assertEqual(
            quarantine["source_change_after_archive"][
                "canonical_path_absence_after_source_deactivation"
            ],
            {
                "scope": ["REGISTRY", "SUMMARY", "MUTATION_RECEIPTS"],
                "original_presence": "IRRELEVANT",
                "reappearance": "RECOVERY_REQUIRED_RETAIN_REMAINING_LOCKS",
            },
        )
        self.assertIn(
            "RETAINED_TOMBSTONE_EXACT_SET_NAMES_AND_BYTES",
            quarantine["terminal_status_verification"]["completed_requires"],
        )
        self.assertEqual(quarantine["automatic_recovery"], "FORBIDDEN_MANUAL_FAIL_CLOSED")
        self.assertNotIn("market_data_capture", risk_gate.PLAN_WRITE_AUTHORIZATION[self.plan["status"]]["write_classes"])

    def test_plan_binds_the_quarantine_path_and_nonacceptance_evidence_class(self) -> None:
        self.assertIn("registry_quarantine_root", self.plan["resolved_path_bindings"])
        policy = self.plan["acceptance_policy"]
        self.assertEqual(policy["evidence_class"], "CAUSAL_REPLAY_INPUT_READY")
        self.assertFalse(policy["acceptance_capable"])
        self.assertEqual(policy["acceptance_decision"], "NONE_CAPTURE_ONLY")


class PlanValidatorExactnessTests(unittest.TestCase):
    @staticmethod
    def _rehash(plan: dict) -> None:
        plan["plan_hash"] = plan_builder.canonical_hash(
            {key: value for key, value in plan.items() if key != "plan_hash"}
        )

    def test_structured_runtime_contract_mutations_fail_after_rehash(self) -> None:
        mutations = {
            "surface_rows_path": lambda plan: plan["event_registry"][
                "discovery_surfaces"
            ][0].__setitem__("rows_path", ["result", "wrong"]),
            "surface_payload": lambda plan: plan["event_registry"][
                "discovery_surfaces"
            ][0]["payload_contract"].__setitem__("category", "OPTIONAL"),
            "surface_cursor": lambda plan: plan["event_registry"][
                "discovery_surfaces"
            ][0]["pagination_contract"].__setitem__(
                "cursor_contract", "STRING_OPTIONAL"
            ),
            "terminal_memory": lambda plan: plan["event_registry"][
                "terminal_lifecycle_evidence"
            ].__setitem__("summary_terminal_ids", "CURRENT_ONLY"),
            "receipt_fields": lambda plan: plan["event_registry"][
                "mutation_receipt_chain"
            ]["record_fields_before_receipt_hash"].remove("mutation_run_id"),
            "shared_writer": lambda plan: plan["enforcement"][
                "shared_single_writer"
            ].__setitem__("stale_claim", "AUTO_CLEAR"),
            "quarantine_lock": lambda plan: plan["registry_quarantine"][
                "lock"
            ].__setitem__("hold", "RELEASE_EARLY"),
            "quarantine_receipts": lambda plan: plan["registry_quarantine"][
                "receipt_archival"
            ].__setitem__("round_trip", "BEST_EFFORT"),
            "quarantine_source_change": lambda plan: plan["registry_quarantine"][
                "source_change_after_archive"
            ].__setitem__("canonical_reappearance", "DELETE"),
            "quarantine_canonical_absence_scope": lambda plan: plan[
                "registry_quarantine"
            ]["source_change_after_archive"][
                "canonical_path_absence_after_source_deactivation"
            ]["scope"].remove("SUMMARY"),
            "quarantine_terminal_status": lambda plan: plan[
                "registry_quarantine"
            ]["terminal_status_verification"].__setitem__(
                "before_global_claim_release", "SKIP"
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                plan = copy.deepcopy(
                    plan_builder.build_plan("2026-08-23T18:00:00.000Z")
                )
                mutate(plan)
                self._rehash(plan)
                with self.assertRaises(plan_builder.PlanBuildError):
                    plan_builder.validate_plan(plan)


if __name__ == "__main__":
    unittest.main()
