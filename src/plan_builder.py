"""Generates the immutable PlanOnly for this project.

The plan records what the project may reach, what it may do, and the SHA-256 of every
runtime file that implements those limits. Reissuing it is the deliberate act that
accompanies any runtime change - the risk gate refuses to run against a plan that no
longer describes the files on disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import project_config as config
from canonical_hash import canonical_hash


SCHEMA = "premarket_perp_capture_planonly_v28"
PLAN_ID = "premarket_perp_capture_20260822_v28"
SUPERSEDES_PLAN_ID = "premarket_perp_capture_20260822_v27"
SUPERSEDES_PLAN_HASH = "859bd59a406dd97ae0fb1e8239f5f34541a50cb08cbb39fbda4d189c5d7b2446"
SUPERSEDES_PLAN_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v27.json"
HASH_METHOD = "sha256_canonical_json_excluding_plan_hash"
PLAN_STATUS = "OFFICIAL_ATTESTATION_LINEAGE_HARDENED_NO_CAPTURE"


class PlanBuildError(ValueError):
    pass


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        raise PlanBuildError(f"bound file missing: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _surface_row_contract(
    *,
    native_id_field: str,
    lifecycle_fields: dict[str, str],
    queried_surface_predicate: str,
) -> dict[str, Any]:
    return {
        "row_type": "MAPPING_REQUIRED",
        "non_object_row": "ACQUISITION_FAILURE_NO_MUTATION",
        "native_id_field": native_id_field,
        "native_id_contract": "NONEMPTY_CANONICAL_STRING_NO_WHITESPACE",
        "missing_or_noncanonical_native_id": "ACQUISITION_FAILURE_NO_MUTATION",
        "duplicate_native_id": "ACQUISITION_FAILURE_NO_MUTATION",
        "lifecycle_fields": lifecycle_fields,
        "invalid_lifecycle_field": "ACQUISITION_FAILURE_NO_MUTATION",
        "queried_surface_predicate": queried_surface_predicate,
    }


def _discovery_surface_contracts() -> list[dict[str, Any]]:
    bybit_payload = {
        "root_type": "MAPPING_REQUIRED",
        "success_code": "EXACT_INT_0_OR_STRING_0_BOOL_AND_FLOAT_FORBIDDEN",
        "rows_type": "ARRAY_REQUIRED",
        "category": "EXACT_STRING_LINEAR_REQUIRED",
        "failure": "ACQUISITION_FAILURE_NO_MUTATION",
    }
    bybit_pagination = {
        "cursor_path": ["result", "nextPageCursor"],
        "cursor_param": "cursor",
        "cursor_contract": "CANONICAL_STRING_REQUIRED_EMPTY_ALLOWED",
        "empty_cursor": "COMPLETE",
        "nonempty_cursor": "FOLLOW_UNTIL_EMPTY",
        "repeated_cursor": "ACQUISITION_FAILURE_NO_MUTATION",
        "max_pages": 20,
        "page_cap": "TRUNCATED_INCOMPLETE_NO_MUTATION",
    }
    okx_payload = {
        "root_type": "MAPPING_REQUIRED",
        "success_code": "STRING_COERCION_EXACT_ZERO",
        "rows_type": "ARRAY_REQUIRED",
        "failure": "ACQUISITION_FAILURE_NO_MUTATION",
    }
    gate_payload = {
        "root_type": "ARRAY_REQUIRED",
        "failure": "ACQUISITION_FAILURE_NO_MUTATION",
    }
    return [
        {
            "surface_id": "bybit_linear_prelaunch",
            "venue": "bybit",
            "endpoint_url": "https://api.bybit.com/v5/market/instruments-info",
            "query_params": {
                "category": "linear",
                "status": "PreLaunch",
                "limit": "1000",
            },
            "rows_path": ["result", "list"],
            "native_id_field": "symbol",
            "instrument_type_field": "contractType",
            "expected_instrument_type": "LinearPerpetual",
            "unexpected_instrument_type_policy": "REJECT",
            "payload_contract": dict(bybit_payload),
            "row_contract": _surface_row_contract(
                native_id_field="symbol",
                lifecycle_fields={
                    "status": "EXACT_STRING_PRELAUNCH_REQUIRED",
                    "isPreListing": "EXACT_BOOLEAN_TRUE_REQUIRED",
                    "launchTime": "OPTIONAL_POSITIVE_NUMERIC_EPOCH_MILLISECONDS",
                },
                queried_surface_predicate=(
                    "status=PreLaunch AND isPreListing=true"
                ),
            ),
            "pagination_contract": dict(bybit_pagination),
        },
        {
            "surface_id": "bybit_linear_trading",
            "venue": "bybit",
            "endpoint_url": "https://api.bybit.com/v5/market/instruments-info",
            "query_params": {
                "category": "linear",
                "status": "Trading",
                "limit": "1000",
            },
            "rows_path": ["result", "list"],
            "native_id_field": "symbol",
            "instrument_type_field": "contractType",
            "expected_instrument_type": "LinearPerpetual",
            "unexpected_instrument_type_policy": "FILTER",
            "payload_contract": dict(bybit_payload),
            "row_contract": _surface_row_contract(
                native_id_field="symbol",
                lifecycle_fields={
                    "status": "EXACT_STRING_TRADING_REQUIRED",
                    "isPreListing": "EXACT_BOOLEAN_FALSE_REQUIRED",
                    "launchTime": "OPTIONAL_POSITIVE_NUMERIC_EPOCH_MILLISECONDS",
                },
                queried_surface_predicate=(
                    "status=Trading AND isPreListing=false"
                ),
            ),
            "pagination_contract": dict(bybit_pagination),
        },
        {
            "surface_id": "okx_swap",
            "venue": "okx",
            "endpoint_url": "https://www.okx.com/api/v5/public/instruments",
            "query_params": {"instType": "SWAP"},
            "rows_path": ["data"],
            "native_id_field": "instId",
            "instrument_type_field": "instType",
            "expected_instrument_type": "SWAP",
            "unexpected_instrument_type_policy": "REJECT",
            "payload_contract": dict(okx_payload),
            "row_contract": _surface_row_contract(
                native_id_field="instId",
                lifecycle_fields={
                    "state": "NONEMPTY_CANONICAL_STRING_REQUIRED",
                    "ruleType": "NONEMPTY_CANONICAL_STRING_REQUIRED",
                    "listTime": "OPTIONAL_POSITIVE_NUMERIC_EPOCH_MILLISECONDS",
                    "preMktSwTime": "OPTIONAL_POSITIVE_NUMERIC_EPOCH_MILLISECONDS",
                },
                queried_surface_predicate="instType=SWAP",
            ),
            "pagination_contract": "NONE_SINGLE_RESPONSE",
        },
        {
            "surface_id": "okx_futures",
            "venue": "okx",
            "endpoint_url": "https://www.okx.com/api/v5/public/instruments",
            "query_params": {"instType": "FUTURES"},
            "rows_path": ["data"],
            "native_id_field": "instId",
            "instrument_type_field": "instType",
            "expected_instrument_type": "FUTURES",
            "unexpected_instrument_type_policy": "REJECT",
            "payload_contract": dict(okx_payload),
            "row_contract": _surface_row_contract(
                native_id_field="instId",
                lifecycle_fields={
                    "state": "NONEMPTY_CANONICAL_STRING_REQUIRED",
                    "ruleType": "NONEMPTY_CANONICAL_STRING_REQUIRED",
                    "listTime": "OPTIONAL_POSITIVE_NUMERIC_EPOCH_MILLISECONDS",
                    "preMktSwTime": "OPTIONAL_POSITIVE_NUMERIC_EPOCH_MILLISECONDS",
                },
                queried_surface_predicate="instType=FUTURES",
            ),
            "pagination_contract": "NONE_SINGLE_RESPONSE",
        },
        {
            "surface_id": "gate_usdt_contracts",
            "venue": "gate",
            "endpoint_url": "https://api.gateio.ws/api/v4/futures/usdt/contracts",
            "query_params": {},
            "rows_path": [],
            "native_id_field": "name",
            "instrument_type_field": None,
            "expected_instrument_type": None,
            "unexpected_instrument_type_policy": "REJECT",
            "payload_contract": dict(gate_payload),
            "row_contract": _surface_row_contract(
                native_id_field="name",
                lifecycle_fields={
                    "status": "NONEMPTY_CANONICAL_STRING_REQUIRED",
                    "is_pre_market": "BOOLEAN_REQUIRED",
                    "in_delisting": "OPTIONAL_BOOLEAN",
                    "create_time": "OPTIONAL_POSITIVE_NUMERIC_EPOCH_SECONDS",
                },
                queried_surface_predicate="PUBLIC_USDT_CONTRACT_ARRAY",
            ),
            "pagination_contract": "NONE_SINGLE_RESPONSE",
        },
    ]


def _shared_single_writer_contract() -> dict[str, Any]:
    return {
        "preflight_checks": ["SHARED_WRITER_CLAIM", "ACTIVE_RUN_RECORD"],
        "stale_claim": "REPORT_NEVER_CLEAR_AUTOMATICALLY",
        "registry_quarantine": {
            "initial_and_commit_preflight": True,
            "global_claim_acquisition": (
                "AFTER_COMMIT_PREFLIGHT_AND_SOURCE_CAS_BEFORE_ARCHIVE_PUBLISH"
            ),
            "hold_until": "SOURCE_DEACTIVATED_EXACT_BOUNDARY_VERIFIED",
            "release_order": [
                "GLOBAL_MARKET_WRITER_CLAIM",
                "REGISTRY_LOCK_TO_DURABLE_TERMINAL_PROOF",
            ],
            "claim_release_failure": "RETAIN_BOTH_LOCKS_MANUAL_RECOVERY",
        },
    }


def _registry_quarantine_contract() -> dict[str, Any]:
    return {
        "archive_schema": "premarket_perp_registry_quarantine_v2",
        "state_schema": "premarket_perp_registry_quarantine_state_v1",
        "result_schema": "premarket_perp_registry_quarantine_result_v2",
        "receipt_archive_schema": "premarket_perp_registry_receipt_bytes_v1",
        "network_requests": "FORBIDDEN",
        "preflight": ["INITIAL", "COMMIT"],
        "operator_cas": "EXACT_SOURCE_BYTES_AND_NAMES",
        "lock": {
            "registry": "O_EXCL_HELD_FROM_SNAPSHOT_TO_FINAL_DURABLE_PROOF",
            "global_market_writer": (
                "ACQUIRED_AFTER_COMMIT_CAS_BEFORE_ARCHIVE_PUBLICATION"
            ),
            "hold": "BOTH_LOCKS_THROUGH_SOURCE_DEACTIVATION_VERIFICATION",
            "release_order": [
                "GLOBAL_MARKET_WRITER_CLAIM",
                "REGISTRY_LOCK_MOVED_TO_TERMINAL_PROOF_FINAL_FALLIBLE_IO",
            ],
            "failure": "RETAIN_REMAINING_LOCKS_MANUAL_RECOVERY",
        },
        "durability": {
            "same_volume_move_required": True,
            "windows": "MoveFileExW_REPLACE_WHEN_ALLOWED_PLUS_WRITE_THROUGH",
            "posix": "RENAME_OR_REPLACE_PLUS_BOTH_PARENT_DIRECTORY_FSYNC",
            "exclusive_file_creation": "FSYNC_TEMP_THEN_DURABLE_NO_CLOBBER_MOVE",
            "final_registry_lock_move": "NO_FALLIBLE_IO_AFTERWARD",
        },
        "publish_order": [
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
        "registry_deactivated": "LAST_SOURCE_COMPONENT_RETAINED_AS_TOMBSTONE",
        "receipt_archival": {
            "encoding": "RAW_BYTES_BASE64",
            "per_entry_integrity": ["ORIGINAL_NAME", "SIZE", "SHA256"],
            "round_trip": "EXACT_BYTES_REQUIRED",
        },
        "source_change_after_archive": {
            "verification_mismatch": "RESTORE_OR_RETAIN_FAIL_CLOSED",
            "canonical_reappearance": "NEVER_DELETE_UNARCHIVED_BYTES",
            "canonical_path_absence_after_source_deactivation": {
                "scope": ["REGISTRY", "SUMMARY", "MUTATION_RECEIPTS"],
                "original_presence": "IRRELEVANT",
                "reappearance": "RECOVERY_REQUIRED_RETAIN_REMAINING_LOCKS",
            },
            "retained_tombstones": (
                "EXACT_NAMES_AND_BYTES_BOUND_IN_SOURCE_DEACTIVATED_STATE"
            ),
        },
        "automatic_recovery": "FORBIDDEN_MANUAL_FAIL_CLOSED",
        "terminal_status_verification": {
            "before_source_deactivation": (
                "MANIFEST_PREPARED_DECLARED_FILES_EXACT_ENTRY_SET_AND_SOURCE_CAS"
            ),
            "before_global_claim_release": (
                "RECOVERY_REQUIRED_SOURCE_DEACTIVATED_ZERO_PROBLEMS"
            ),
            "after_global_claim_release_before_registry_lock_release": (
                "RECOVERY_REQUIRED_SOURCE_DEACTIVATED_ZERO_PROBLEMS"
            ),
            "completed_requires": [
                "MANIFEST_AND_DECLARED_FILE_HASHES",
                "SOURCE_GENERATION_CAS",
                "PREPARED_ARCHIVE_DURABLE_SOURCE_DEACTIVATED_STATE_CHAIN",
                "RETAINED_TOMBSTONE_EXACT_SET_NAMES_AND_BYTES",
                "REGISTRY_LOCK_TERMINAL_PROOF",
                "EXACT_ARCHIVE_ENTRY_SET",
            ],
        },
    }


def _mutation_receipt_chain_contract() -> dict[str, Any]:
    return {
        "creation": "O_EXCL",
        "immutable": True,
        "link_field": "previous_mutation_receipt_hash",
        "record_fields_before_receipt_hash": [
            "schema",
            "mutation_seq",
            "previous_mutation_receipt_hash",
            "registry_path_name",
            "registry_sha256",
            "registry_entries",
            "registry_head_record_hash",
            "summary_content_hash",
            "mutation_type",
            "mutation_run_id",
            "plan_id",
            "plan_hash",
            "active_contract_ids_by_venue",
            "active_lifecycle_generations_by_venue",
            "lifecycle_generation_high_water_by_venue",
            "last_complete_metadata_refresh_received_at_utc",
            "raw_universe_rows_by_venue",
            "raw_universe_rows_by_surface",
            "relevant_identity_ids_by_surface",
            "relevant_identity_set_sha256_by_surface",
            "explicit_terminal_ids_by_surface",
        ],
        "receipt_hash": "CANONICAL_HASH_OF_EXACT_PREHASH_RECORD",
        "failure_recovery_boundary": (
            "LOCAL_CRASH_RECOVERY_EVIDENCE_ONLY_NOT_CRYPTOGRAPHIC_AUTHENTICITY"
        ),
        "process_crash_recovery": (
            "FAIL_CLOSED_MANUAL_RECOVERY_NO_AUTOMATIC_ATOMICITY_CLAIM"
        ),
        "wal_implemented": False,
    }


def build_plan(generated_at_utc: str) -> dict[str, Any]:
    files = [
        {
            "role": role,
            "repo_path": relative,
            "sha256": _sha256_file(config.PROJECT_ROOT / relative),
        }
        for role, relative in config.BOUND_RUNTIME_FILES
    ]
    plan: dict[str, Any] = {
        "schema": SCHEMA,
        "plan_id": PLAN_ID,
        "supersedes_plan_id": SUPERSEDES_PLAN_ID,
        "supersedes_plan_hash": SUPERSEDES_PLAN_HASH,
        "supersedes_plan_path": SUPERSEDES_PLAN_PATH,
        "project": "ZolotyayLopata-premarket-perp-capture",
        "strategy_branch": "premarket_perpetual_listing_impulse",
        "mode": "PlanOnly",
        "status": PLAN_STATUS,
        "generated_at_utc": generated_at_utc,
        "implementation_path_semantics": "repo_path values are relative to the runtime Git root",
        "objective": (
            "Capture public pre-market and early perpetual market data around a "
            "listing event t0 on Bybit, OKX and Gate, densely enough to replay the "
            "hypothesis 'enter before the listing, exit at t0/+5/+15/+60s' offline. "
            "Capture only: this plan authorises no execution of any kind, and the "
            "replay is a simulation over data already on disk."
        ),
        "hypothesis_under_study": (
            "LONG before the listing, exit at t0, +5s, +15s or +60s - stated here so "
            "the capture design can be judged against it, not so it can be traded"
        ),
        # What the project does, as opposed to the instrument class it observes. The
        # distinction is the entire reason this repository is separate: the spot
        # monitor forbids leverage because it never goes near it, while this one looks
        # at a leveraged market and must still never take leverage.
        "risk_contract": dict(config.RISK_CONTRACT),
        "allowed_endpoints": [list(item) for item in config.ALLOWED_ENDPOINTS],
        "resolved_path_bindings": {
            "shared_gate_path": str(config.SHARED_GATE_PATH.resolve(strict=False)),
            "shared_writer_claim_path": str(
                config.SHARED_WRITER_CLAIM_PATH.resolve(strict=False)
            ),
            "capture_root": str(config.CAPTURE_ROOT.resolve(strict=False)),
            "registry_quarantine_root": str(
                config.REGISTRY_QUARANTINE_ROOT.resolve(strict=False)
            ),
        },
        "enforcement": {
            "capability_scan": (
                "src/ and tools/ are scanned against docs/risk/forbidden-capabilities.txt "
                "and against allowed_endpoints; any order, signing, credential, "
                "leverage-changing or off-list URL marker blocks the gate"
            ),
            "plan_bindings": (
                "every bound file's SHA-256 is verified against this plan, and this "
                "plan against the external trust root src/frozen_plan_bindings.py, "
                "which sits outside the plan-binds-runtime cycle on purpose"
            ),
            "shared_single_writer": _shared_single_writer_contract(),
            "shared_gate_process": (
                "the gate subprocess must exit with code zero before its JSON status "
                "can open any write class; nonzero exit, malformed output or stderr-only "
                "failure is UNAVAILABLE and fail-closed"
            ),
            "capture_token": (
                "a capture requires a one-shot token minted only by a passing "
                "market_data_capture preflight and bound to the exact run, event, "
                "official source class, PlanOnly identity, resolved paths, gate and "
                "capability-scan hash; consumption atomically takes it once and "
                "rechecks the current plan, paths, capability scan, gate, writer "
                "claim and run record"
            ),
            "runtime_http_allow_list": (
                "GET requests require HTTPS, an exact declared host/path and declared "
                "query keys; userinfo, redirects, dot/encoded paths, non-public DNS "
                "answers and caller-supplied allow-lists fail closed; TCP connects to "
                "the already validated IP while TLS SNI and Host retain the venue name"
            ),
            "resolved_path_bindings": (
                "shared gate, shared writer claim and capture root are compared to "
                "their canonical absolute values before either write class is allowed"
            ),
        },
        "write_classes": {
            key: dict(value) for key, value in config.WRITE_CLASSES.items()
        },
        "event_registry": {
            "schema": "premarket_perp_event_registry_v3",
            "path": "docs/registry/listing-events-v3.jsonl",
            "legacy_v1_path": "docs/registry/listing-events.jsonl",
            "immutable_v2_projection": {
                "schema": "premarket_perp_event_registry_v2",
                "path": "docs/registry/listing-events-v2.jsonl",
                "registry_entries": 16,
                "registry_sha256": (
                    "fd3b864bc4b1b311b49a904246edd8980008ab5f1830df9042087df9619bc9a4"
                ),
                "registry_head_record_hash": (
                    "7d3459943cf2122c0eb39a27452a4ab28eb41a08ceb61e19011730e14d6e8696"
                ),
                "summary_path": "docs/registry/listing-events-v2.summary.json",
                "summary_sha256": (
                    "72a619aa893cd794dbc0e3702f2f6fd9ccd3a495c8b610dfa564c8e20b6df176"
                ),
                "mutation_receipt_path": (
                    "docs/registry/listing-events-v2.jsonl.mutation-receipts/"
                    "00000000000000000000-"
                    "d86913b6af93d3487ef3cdbe09a3b47f3519188eea65ebf2f18aaa8fd5976282.json"
                ),
                "mutation_receipt_file_sha256": (
                    "1c8eab6576a2d4452dfbcd9aba87563a0a50da4382da0447012e6f95cb1d865b"
                ),
                "mutation_receipt_hash": (
                    "d86913b6af93d3487ef3cdbe09a3b47f3519188eea65ebf2f18aaa8fd5976282"
                ),
                "summary_content_hash": (
                    "ba4e8bbc99391cc0049810d3fd0e430f6806407b43347f4e5c6e0d1dd29b08a8"
                ),
                "policy": "READ_ONLY_BYTE_IDENTICAL_MIGRATION_SOURCE",
            },
            "asset_identity": {
                "classes": [
                    "CRYPTO_TOKEN",
                    "EQUITY_ISSUER",
                    "TOKENIZED_EQUITY",
                    "TRADFI_OTHER",
                    "UNCLASSIFIED",
                ],
                "identity_fields": [
                    "asset_class",
                    "issuer_namespace",
                    "issuer_id",
                    "asset_identity_hash",
                ],
                "capture_eligible_asset_class": "CRYPTO_TOKEN",
                "all_other_classes": "DESCRIPTIVE_ONLY",
                "ambiguous_identity": "FAIL_CLOSED_UNCLASSIFIED",
                "cross_class_merge": "FORBIDDEN",
            },
            "discovery_surfaces": _discovery_surface_contracts(),
            "lifecycle_states": [
                "SCHEDULED",
                "ACTIVE_PREMARKET",
                "TRANSITION_SCHEDULED",
                "TRANSITIONED_STANDARD",
                "CANCELLED",
                "DELISTING",
                "DELISTED",
                "UNKNOWN",
            ],
            "relevant_identity_authority": {
                "retention_scope": "ALL_RELEVANT_IDENTITIES_PER_SURFACE",
                "persisted_fields": [
                    "raw_universe_rows_by_surface",
                    "relevant_identity_ids_by_surface",
                    "relevant_identity_set_sha256_by_surface",
                    "explicit_terminal_ids_by_surface",
                ],
                "missing_tracked_identity": "ACQUISITION_FAILURE_NO_MUTATION",
                "identity_set_hash_mismatch": "FAIL_CLOSED_NO_MUTATION",
                "bybit_cross_surface_trading": "TERMINAL_TRANSITION",
                "okx_cross_surface_xperp": "TERMINAL_TRANSITION",
                "okx_cross_surface_normal": "TERMINAL_TRANSITION",
                "untracked_terminal_row": "NO_GENERATION_ALLOCATION",
                "bybit_terminal_status_surface": "NOT_PREREGISTERED_IN_V17",
                "bybit_unobserved_cancelled_or_closed": (
                    "MISSING_TRACKED_IDENTITY_ACQUISITION_FAILURE_NO_INFERENCE"
                ),
            },
            "timestamp_kinds": [
                "premarket_contract_launch_ts",
                "official_spot_t0",
                "first_trade_ts",
                "transition_ts",
                "contract_created_ts",
            ],
            "timestamp_producers": {
                "premarket_contract_launch_ts": "VENUE_INSTRUMENT_METADATA",
                "official_spot_t0": "HUMAN_VERIFIED_OFFICIAL_ATTESTATION",
                "transition_ts": (
                    "VENUE_METADATA_WHEN_EXPLICIT_ELSE_OBSERVED_LIFECYCLE_"
                    "DETECTION_TIME_PROXY"
                ),
                "first_trade_ts": "RESERVED_NO_REGISTRY_PRODUCER_CAPTURE_TRADES_SEPARATE",
                "contract_created_ts": "VENUE_INSTRUMENT_METADATA_DESCRIPTIVE_ONLY",
            },
            "source_classes": [
                "OFFICIAL_ANNOUNCEMENT",
                "VENUE_INSTRUMENT_METADATA",
                "OBSERVED_PUBLIC_TRADE",
                "OBSERVED_LIFECYCLE",
            ],
            "acceptance_anchor": {
                "timestamp_kind": "official_spot_t0",
                "source_class": "OFFICIAL_ANNOUNCEMENT",
                "asset_class": "CRYPTO_TOKEN",
            },
            "proxy_policy": {
                "source_classes": [
                    "VENUE_INSTRUMENT_METADATA",
                    "OBSERVED_PUBLIC_TRADE",
                    "OBSERVED_LIFECYCLE",
                ],
                "evidence_use": "DESCRIPTIVE_ONLY",
                "capture_eligible": False,
                "acceptance_support": "FORBIDDEN",
            },
            "episode_identity": (
                "sha256(venue,native_premarket_contract_id,lifecycle_generation); "
                "schedule revisions do not change episode identity"
            ),
            "stream_identity": (
                "sha256(episode_id,timestamp_kind,instrument_role,source_class,source_identity)"
            ),
            "lineage": (
                "strict global record_seq/previous_record_hash chain plus independent "
                "stream_revision/supersedes_record_hash chains; forks and orphan "
                "revisions fail closed; a nonempty production registry requires its "
                "tail-anchoring summary receipt before capture selection"
            ),
            "capture_lineage_verification": {
                "registry_bytes": "EXACT_HISTORICAL_PREFIX",
                "registry_tail": "CAPTURE_RECORDED_TAIL_HASH",
                "mutation_receipt": "EXACT_SEQ_AND_HASH",
                "authority_state": "EXACT_RECORDED_HASH",
                "official_record": "UNSUPERSEDED_OFFICIAL_CRYPTO_ANCHOR",
                "official_source_url_and_identity": "EXACT_OFFICIAL_RECORD_MATCH",
                "current_head_substitution": "FORBIDDEN",
                "plan_and_asset_mapping": "EXACT_MATCH",
                "latest_mutation_receipt_plan_identity": "EXACT_ACTIVE_PLAN_ID_AND_HASH",
            },
            "official_attestation": {
                "policy": (
                    "OFFICIAL_ANNOUNCEMENT is a separate preflight write class. The "
                    "row must target the current active metadata lifecycle generation "
                    "and carry an approved venue announcement URL, explicit UTC "
                    "receipt time, author, exact quoted time and symbol fragments, "
                    "and a quoted time equal to official_spot_t0. received_at_utc is "
                    "taken from the writer's own UTC clock, the lead is recomputed, "
                    "and the full candidate chain is validated before append."
                ),
                "quotation_evidence": {
                    "storage": "VERBATIM_UTF8",
                    "normalization": "FORBIDDEN",
                    "time_fragment_must_be_verbatim_substring": True,
                    "symbol_fragment_must_be_verbatim_substring": True,
                    "forbidden_unicode_categories": ["Cc", "Cf"],
                },
                "announcement_url_policy": {
                    "https_exact_official_host": True,
                    "explicit_port": "FORBIDDEN",
                    "backslash": "FORBIDDEN",
                    "unicode_control_or_format": "FORBIDDEN",
                    "fragment": "DISCARDED_BEFORE_STORAGE",
                },
                "writer_clock_recheck": "UNDER_REGISTRY_LOCK_BEFORE_APPEND",
                "metadata_freshness_required": True,
                "current_generation_required": True,
                "precommit_authority_recheck": "UNDER_REGISTRY_LOCK",
                "venue_roles": {
                    "venue": "PERPETUAL_CONTRACT_VENUE",
                    "listing_venue": "OFFICIAL_SPOT_ANNOUNCEMENT_VENUE",
                },
                "locked_rebuild_field_preservation": ["venue", "listing_venue"],
            },
            "official_t0_precision_sec": 60,
            "seconds_grade_replay_precision_sec": 1,
            "seconds_grade_readiness": {
                "required_precision_sec_lte": 1,
                "current_official_producer_precision_sec": 60,
                "current_official_producer_capable": False,
                "current_result": "DESCRIPTIVE_ONLY_PRECISION_GT_ONE_SECOND",
                "authority_increase": (
                    "NEW_IMMUTABLE_PLAN_AND_SECONDS_GRADE_OFFICIAL_PRODUCER_REQUIRED"
                ),
            },
            "selection_clock": "received_at_utc",
            "capture_due_window": {
                "target": "official_spot_t0 - window_before_t0_sec",
                "early_grace_sec": config.CAPTURE_LAUNCH_EARLY_GRACE_SEC,
                "late_grace_sec": config.CAPTURE_LAUNCH_LATE_GRACE_SEC,
                "eligibility_interval": (
                    "target - early_grace_sec <= now_ts <= target + late_grace_sec"
                ),
            },
            "lifecycle_generation_persistence": (
                "each complete refresh stores exact active contract-to-generation "
                "mappings plus monotonic per-contract high-water state; a missing tracked "
                "identity is acquisition failure with no mutation; only an explicit "
                "terminal phase removes the active generation, and a later "
                "reappearance after explicit terminal evidence allocates high_water+1"
            ),
            "lifecycle_generation_transition_policy": {
                "missing_tracked_identity": "ACQUISITION_FAILURE_NO_MUTATION",
                "explicit_terminal": {
                    "action": "REMOVE_ACTIVE_GENERATION",
                    "phases": [
                        "CANCELLED",
                        "DELISTING",
                        "DELISTED",
                        "TRANSITIONED_STANDARD",
                    ],
                },
                "reappearance_after_explicit_terminal": "ALLOCATE_HIGH_WATER_PLUS_ONE",
                "untracked_terminal_row": "IGNORE_WITHOUT_HIGH_WATER_MUTATION",
            },
            "terminal_lifecycle_evidence": {
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
            "cross_surface_conflict_policy": {
                "terminal_vs_active": "EXPLICIT_TERMINAL_WINS_OVER_STALE_ACTIVE_ROW",
                "simultaneous_terminal_evidence": (
                    "EXACT_VENUE_TIMESTAMP_WINS_OVER_DETECTION_PROXY"
                ),
                "equal_strength_tie_break": "SURFACE_DECLARATION_ORDER",
            },
            "active_lifecycle_generation": {
                "identity_fields": [
                    "venue",
                    "premarket_contract_id",
                    "lifecycle_generation",
                ],
                "required_for_official_attestation": True,
                "required_for_capture": True,
                "source_of_truth": (
                    "latest_verified_mutation_receipt_carrying_last_complete_"
                    "metadata_refresh_state"
                ),
                "active_state_field": "active_lifecycle_generations_by_venue",
                "high_water_field": "lifecycle_generation_high_water_by_venue",
                "last_complete_refresh_field": (
                    "last_complete_metadata_refresh_received_at_utc"
                ),
                "raw_universe_rows_field": "raw_universe_rows_by_venue",
                "max_complete_metadata_refresh_age_sec": (
                    config.MAX_COMPLETE_METADATA_REFRESH_AGE_SEC
                ),
            },
            "production_refresh_contract": {
                "timestamp_owner": "WRITER_AFTER_ALL_HTTP_RESPONSES",
                "caller_observed_at_override": "FORBIDDEN",
                "injected_payload_destination": "EXPLICIT_NONPRODUCTION_PATH_ONLY",
                "precommit_authority_recheck": "UNDER_REGISTRY_LOCK",
                "concurrency_control": "RECEIPT_HEAD_COMPARE_AND_SWAP",
                "empty_full_universe_response": "ACQUISITION_FAILURE",
                "required_nonempty_full_universe_surfaces": list(
                    config.FULL_UNIVERSE_SURFACE_IDS
                ),
                "legitimately_empty_surfaces": list(
                    config.LEGITIMATELY_EMPTY_SURFACE_IDS
                ),
                "retention_scope": "PER_REQUIRED_FULL_UNIVERSE_SURFACE",
                "minimum_prior_universe_retention_ratio": (
                    config.MIN_FULL_UNIVERSE_RETENTION_RATIO
                ),
                "abrupt_or_truncated_response": "ACQUISITION_FAILURE_NO_STATE_MUTATION",
            },
            "capture_authority_receipt_fields": [
                "mutation_receipt_seq",
                "mutation_receipt_hash",
                "summary_content_sha256",
                "registry_authority_state_hash",
            ],
            "capture_lineage_fields": list(config.CAPTURE_LINEAGE_FIELDS),
            "mutation_receipt_chain": _mutation_receipt_chain_contract(),
            "locking": (
                "metadata refresh uses an O_EXCL registry lock from load and lineage "
                "verification through append, fsync, summary and immutable receipt; "
                "in-process exceptions roll back, but process/power loss fails closed "
                "and requires manual recovery because no durable WAL exists"
            ),
            "venue_metadata_semantics": {
                "bybit": {
                    "prelaunch_predicate": "status=PreLaunch AND isPreListing=true",
                    "asset_classification": (
                        "symbolType=stock => EQUITY_ISSUER; symbolType in "
                        "{commodity,forex,etf} => TRADFI_OTHER; "
                        "symbolType=innovation => CRYPTO_TOKEN; "
                        "missing/empty/unknown => UNCLASSIFIED"
                    ),
                    "launchTime": "DESCRIPTIVE_PREMARKET_CONTRACT_LAUNCH_TS",
                },
                "okx": {
                    "surfaces": ["SWAP", "FUTURES"],
                    "prelaunch_predicate": (
                        "ruleType=pre_market AND state in {preopen,live}"
                    ),
                    "asset_classification": (
                        "instCategory=1 => CRYPTO_TOKEN; 3 => EQUITY_ISSUER; "
                        "4/5/6 => TRADFI_OTHER; missing/empty/unknown => UNCLASSIFIED"
                    ),
                    "listTime": "DESCRIPTIVE_PREMARKET_CONTRACT_LAUNCH_TS",
                    "xperp": "CROSS_SURFACE_TERMINAL_TRANSITION",
                    "normal": "CROSS_SURFACE_TERMINAL_TRANSITION",
                    "terminal_state": {
                        "expired": "DELISTED",
                        "suspend": "DELISTED",
                    },
                },
                "gate": {
                    "surface": "USDT_FUTURES",
                    "prelaunch_predicate": (
                        "is_pre_market=true AND in_delisting!=true AND "
                        "status in {prelaunch,trading}"
                    ),
                    "asset_classification": (
                        "contract_type in {crypto,cryptocurrency,digital_asset} => "
                        "CRYPTO_TOKEN; {stock,stocks,equity,equities} => "
                        "EQUITY_ISSUER; {metal,metals,index,indices,forex,commodity,"
                        "commodities} => TRADFI_OTHER; missing/empty/unknown => UNCLASSIFIED"
                    ),
                    "capture_identity_policy": (
                        "MISSING_OR_UNVERIFIED_CONTRACT_TYPE_IS_"
                        "UNCLASSIFIED_DESCRIPTIVE_ONLY"
                    ),
                    "create_time": "CONTRACT_CREATED_TS_ONLY",
                    "launch_time": "CONTRACT_EXPIRY_NOT_TRADING_LAUNCH",
                    "terminal_predicates": {
                        "status": {
                            "delisting": "DELISTING",
                            "delisted": "DELISTED",
                            "cancelled": "CANCELLED",
                            "canceled": "CANCELLED",
                        },
                        "in_delisting_true": "DELISTING",
                        "transitioned_standard": (
                            "status=trading AND was_tracked=true AND "
                            "is_pre_market=false"
                        ),
                    },
                },
            },
        },
        "capture_bounds": {
            "window_before_t0_sec": config.CAPTURE_WINDOW_BEFORE_SEC,
            "window_after_t0_sec": config.CAPTURE_WINDOW_AFTER_SEC,
            "max_runtime_sec": config.MAX_CAPTURE_RUNTIME_SEC,
            "max_requests_per_capture": config.MAX_REQUESTS_PER_CAPTURE,
            "max_events_per_capture": config.MAX_EVENTS_PER_CAPTURE,
            "capture_root": str(config.CAPTURE_ROOT),
            "one_capture_at_a_time": True,
            "visible_terminal_required": True,
        },
        "capture_evidence": {
            "sampling_method": (
                "REST polling is a sequence of point observations, not a trade tape; "
                "failed or structurally invalid venue payloads remain in the error "
                "denominator and never count as successful samples"
            ),
            "failed_rows_in_readiness_denominator": True,
            "transport_budget": (
                "venue metadata and capture polling use max_retries=0, so each logical "
                "request is exactly one counted transport attempt; both classes are "
                "included in the manifest request total"
            ),
            "metadata_max_retries": 0,
            "sampling_cadence_sec": {
                "outside_burst": dict(config.PROBE_CADENCE_SEC),
                "burst": dict(config.BURST_CADENCE_SEC),
                "burst_half_width_sec": config.BURST_HALF_WIDTH_SEC,
            },
            "fixed_exit_offsets_sec": list(config.PRIMARY_EXIT_OFFSETS_SEC),
            "fixed_entry_lead_sec": config.PRIMARY_ENTRY_LEAD_SEC,
            "required_probes_by_venue": {
                venue: list(probes)
                for venue, probes in config.REPLAY_REQUIRED_PROBES_BY_VENUE.items()
            },
            "venue_probe_authority": {
                "okx_orderbook_instrument_identity": (
                    "EXACT_BOUND_REQUEST_RESPONSE_DOES_NOT_ECHO_INST_ID"
                ),
                "gate_ticker_exchange_timestamp": (
                    "ABSENT_FROM_PAYLOAD_OPTIONAL_DESCRIPTIVE_ONLY"
                ),
            },
            "coverage_clock": "received_ts",
            "sampling_report_clock": "received_ts",
            "exchange_timestamp_policy": {
                "required_for_replay_probes": True,
                "optional_probe_without_exchange_timestamp": (
                    "RAW_DESCRIPTIVE_PAYLOAD_WITH_ERROR_NOT_CAUSAL_EVIDENCE"
                ),
                "max_staleness_sec_by_probe": dict(config.MAX_SAMPLE_STALENESS_SEC),
                "max_future_skew_sec": config.MAX_EXCHANGE_FUTURE_SKEW_SEC,
            },
            "max_burst_gap_cadence_multiplier": (
                config.MAX_BURST_GAP_CADENCE_MULTIPLIER
            ),
            "readiness": (
                "all venue-required probes need causal structurally valid payloads, "
                "full two-sided burst-window coverage, bounded successful-sample gaps "
                "and evidence at the preregistered entry plus every exit; optional "
                "descriptive probes cannot raise or lower readiness"
            ),
            "fixed_entry_evidence_clock": (
                "first_valid_received_ts_at_or_after_target_within_one_cadence"
            ),
            "fixed_exit_evidence_clock": (
                "first_valid_received_ts_at_or_after_target_within_one_cadence"
            ),
            "pre_target_exit_sample_policy": "FORBIDDEN_NO_LOOKAHEAD",
            "lineage": (
                "manifest and immutable receipt carry episode id, exact official "
                "record/source/precision, historical registry prefix and tail, exact "
                "immutable mutation-receipt summary-content hash, and active PlanOnly "
                "id/hash; capture rechecks them before taking the writer claim"
            ),
            "terminal_accounting": {
                "samples_fsynced_before_manifest": True,
                "terminal_run_record_before_claim_release": True,
                "successful_receipt_before_claim_release": True,
                "receipt_failure": (
                    "FAILED_EXCEPTION_TERMINAL_RECORD_THEN_CLAIM_RELEASE"
                ),
                "terminal_record_failure": "KEEP_CLAIM_FAIL_CLOSED",
                "stop_request_cleanup": (
                    "WHILE_OLD_RUN_STILL_OWNS_CLAIM_BEFORE_RELEASE"
                ),
            },
            "artifact_commit": {
                "capture_directory": "EXCLUSIVE_IDENTITY",
                "samples_creation": "O_EXCL",
                "manifest_creation": "O_EXCL",
                "manifest_immutable": True,
                "receipt_creation": "O_EXCL",
                "receipt_rehashes_samples": True,
                "receipt_samples_hash_must_equal": "manifest.output_sha256",
            },
            "post_claim_revalidation": (
                "after claim and before any HTTP request, re-read the shared gate and "
                "reselect the same official registry lineage"
            ),
            "per_request_boundary_recheck": [
                "before_each_metadata_or_poll_request",
                "immediately_after_each_metadata_or_poll_response",
            ],
            "shared_gate_process_exit_code": "MUST_BE_ZERO",
            "token_mismatch_policy": (
                "a token presented by the wrong run, event, source or secret is not "
                "consumed; only a fully validated caller may atomically take it"
            ),
            "entrypoint_policy": {
                "run_capture": "EXACT_STATIC_JSON_SYNTHETIC_FIXTURE_TRANSPORT_ONLY",
                "live_public_market_data": (
                    "capture_event_ONLY_AFTER_GATE_TOKEN_AND_CLAIM"
                ),
            },
            "multi_trade_response_policy": {
                "okx": "NONEMPTY_ALL_ROWS_EXACT_INSTRUMENT",
                "gate": "NONEMPTY_ALL_ROWS_EXACT_CONTRACT",
            },
            "gate_orderbook_request_identity": {
                "required_when_response_contract_absent": True,
                "source": "validated_on_wire_request_query.contract",
                "must_equal": "capture_job.premarket_contract_id",
                "persist_in_sample": True,
            },
            "evidence_classification": {
                "ready": "CAUSAL_REPLAY_INPUT_READY",
                "not_ready": "DESCRIPTIVE_ONLY",
                "acceptance_capable": False,
                "classification_is_not_readiness": True,
            },
        },
        "replay_evidence": {
            "schema": "premarket_perp_replay_v2",
            "production_mode": "PRODUCTION_VERIFIED",
            "production_capture_path": "STRICT_DESCENDANT_OF_CAPTURE_ROOT",
            "receipt_path": "docs/evidence/<capture_id>.json",
            "production_evidence_class": "CAUSAL_REPLAY_INPUT_READY",
            "descriptive_fallback_class": "DESCRIPTIVE_ONLY",
            "acceptance_capable": False,
            "selection_clock": "received_ts",
            "target_selection": "FIRST_VALID_AT_OR_AFTER_WITHIN_ONE_CADENCE",
            "pre_target_fallback": "FORBIDDEN",
            "interpolation_or_bracketing": "FORBIDDEN",
            "exchange_ts_use": "FRESHNESS_FILTER_ONLY",
            "required_clocks": ["request_ts", "received_ts", "exchange_ts"],
            "integrity": [
                "RAW_MANIFEST_SHA256",
                "RAW_SAMPLES_SHA256",
                "CANONICAL_RECEIPT_HASH",
                "EXACT_CAPTURE_ID",
                "EXACT_PLAN_IDENTITY_AND_IMPLEMENTATION",
                "CAPTURE_AUTHORIZED_SELECTED_PLAN",
                "SELECTED_PLAN_CAPTURE_ROOT",
                "EXACT_HISTORICAL_REGISTRY_LINEAGE",
            ],
            "asset_class": "CRYPTO_TOKEN_ONLY",
            "output": "DESCRIPTIVE_GROSS_BBO_MARKOUT_ONLY",
            "entry_price": "OBSERVED_ASK",
            "exit_price": "OBSERVED_BID",
            "execution_or_fill_model": False,
            "fees_slippage_funding_or_liquidation": "NOT_MODELLED_NO_NET_PNL_CLAIM",
            "acceptance_decision": "FORBIDDEN",
            "synthetic_mode": "SYNTHETIC_DESCRIPTIVE_ONLY",
            "synthetic_mode_selection": "EXPLICIT_REQUIRED",
            "production_downgrade_to_synthetic": "FORBIDDEN",
            "fixed_exit_offsets_sec": list(config.PRIMARY_EXIT_OFFSETS_SEC),
            "fixed_entry_lead_sec": config.PRIMARY_ENTRY_LEAD_SEC,
            "production_target_override": "FORBIDDEN",
            "report_readiness": (
                "SEALED_CAPTURE_READY_AND_ENTRY_AND_ALL_FIXED_EXITS"
            ),
        },
        "registry_quarantine": _registry_quarantine_contract(),
        "implementation": {"files": files},
        "forbidden": [
            "orders of any kind, on any venue, in any mode",
            "paper or live execution",
            "private API surfaces, credentials, request signing",
            "taking leverage or margin, or changing either",
            "withdrawals or transfers",
            "acceptance or rejection decisions from captured data",
            "a second concurrent market-data writer in this workspace",
            "background or hidden capture runs",
        ],
        "authorized_after_gate_green": [
            "refresh the public metadata event registry after metadata preflight",
            "verify and materialize descriptive proxy observations offline",
            "append one human-verified official spot t0 after attestation preflight",
            "quarantine one failed registry generation after exact recovery preflight",
        ],
        "activation_gate": {
            "capture_authorized": False,
            "reason": (
                "v28 binds cross-venue official attestation and active-plan mutation "
                "receipt lineage, but status OFFICIAL_ATTESTATION_LINEAGE_HARDENED_NO_CAPTURE "
                "intentionally excludes market_data_capture; neither a direct mint nor "
                "a capture preflight can authorize network capture"
            ),
            "required_next_checkpoint": (
                "a new immutable PlanOnly must explicitly authorize market_data_capture; "
                "activating a scheduler or collector remains a separate user-approved "
                "visible-run action"
            ),
        },
        "acceptance_policy": {
            "evidence_class": "CAUSAL_REPLAY_INPUT_READY",
            "acceptance_capable": False,
            "acceptance_decision": "NONE_CAPTURE_ONLY",
            "note": (
                "no metric computed from this capture supports ACCEPT or REJECT of "
                "any strategy; a separate user-checkpointed plan is required for that"
            ),
        },
        "plan_hash_method": HASH_METHOD,
    }
    # A hash over only the clauses that govern the registry. Binding recorded data to
    # the whole plan_hash meant that adding a module, widening the announcement hosts
    # or changing a capture cadence invalidated a registry those changes never touched
    # - and the repair ran through a quarantine, which is how one edit cost a whole
    # generation. Data must still be provably produced under the rules it claims; those
    # rules are this section, not the entire document.
    plan["registry_contract_hash"] = canonical_hash(plan["event_registry"])
    plan["plan_hash"] = canonical_hash(plan)
    validate_plan(plan)
    return plan


def validate_plan(plan: dict[str, Any]) -> None:
    def require(value: bool, message: str) -> None:
        if not value:
            raise PlanBuildError(message)

    require(plan.get("schema") == SCHEMA, "schema mismatch")
    require(plan.get("plan_id") == PLAN_ID, "plan id mismatch")
    require(plan.get("mode") == "PlanOnly", "mode mismatch")
    require(plan.get("supersedes_plan_id") == SUPERSEDES_PLAN_ID, "supersedes id mismatch")
    require(
        plan.get("supersedes_plan_hash") == SUPERSEDES_PLAN_HASH,
        "supersedes hash mismatch",
    )
    require(
        plan.get("supersedes_plan_path") == SUPERSEDES_PLAN_PATH,
        "supersedes path mismatch",
    )
    require(
        plan.get("status") == PLAN_STATUS,
        "active plan must remain capture-disabled",
    )
    contract = plan.get("risk_contract") or {}
    for key in (
        "private_api", "api_keys", "request_signing", "orders", "paper_execution",  # risk-scan: allow api_key
        "live_execution", "uses_leverage", "uses_margin", "real_capital",
        "withdrawals_or_transfers",
    ):
        require(contract.get(key) is False, f"risk contract must forbid {key}")
    require(contract.get("research_only") is True, "risk contract must be research-only")
    require(contract.get("public_data_only") is True, "risk contract must be public-data-only")
    require(
        plan.get("acceptance_policy", {}).get("acceptance_decision") == "NONE_CAPTURE_ONLY",
        "plan must not carry an acceptance decision",
    )
    require(
        plan.get("acceptance_policy", {}).get("acceptance_capable") is False,
        "active-plan evidence must be explicitly non-acceptance-capable",
    )
    require(
        plan.get("activation_gate", {}).get("capture_authorized") is False,
        "active plan must not authorize capture",
    )
    require(
        plan.get("authorized_after_gate_green") == [
            "refresh the public metadata event registry after metadata preflight",
            "verify and materialize descriptive proxy observations offline",
            "append one human-verified official spot t0 after attestation preflight",
            "quarantine one failed registry generation after exact recovery preflight",
        ],
        "authorized action set differs from the capture-disabled active contract",
    )
    require(bool(plan.get("allowed_endpoints")), "plan must declare its endpoint allow-list")
    require(
        set((plan.get("resolved_path_bindings") or {}))
        == {
            "shared_gate_path",
            "shared_writer_claim_path",
            "capture_root",
            "registry_quarantine_root",
        },
        "resolved path bindings are incomplete",
    )
    enforcement = plan.get("enforcement") or {}
    require(
        enforcement.get("shared_single_writer")
        == _shared_single_writer_contract(),
        "shared single-writer contract mismatch",
    )
    registry = plan.get("event_registry") or {}
    require(
        registry.get("schema") == "premarket_perp_event_registry_v3"
        and registry.get("path") == "docs/registry/listing-events-v3.jsonl",
        "active-plan registry identity mismatch",
    )
    legacy_v2 = registry.get("immutable_v2_projection") or {}
    require(
        legacy_v2
        == {
            "schema": "premarket_perp_event_registry_v2",
            "path": "docs/registry/listing-events-v2.jsonl",
            "registry_entries": 16,
            "registry_sha256": (
                "fd3b864bc4b1b311b49a904246edd8980008ab5f1830df9042087df9619bc9a4"
            ),
            "registry_head_record_hash": (
                "7d3459943cf2122c0eb39a27452a4ab28eb41a08ceb61e19011730e14d6e8696"
            ),
            "summary_path": "docs/registry/listing-events-v2.summary.json",
            "summary_sha256": (
                "72a619aa893cd794dbc0e3702f2f6fd9ccd3a495c8b610dfa564c8e20b6df176"
            ),
            "mutation_receipt_path": (
                "docs/registry/listing-events-v2.jsonl.mutation-receipts/"
                "00000000000000000000-"
                "d86913b6af93d3487ef3cdbe09a3b47f3519188eea65ebf2f18aaa8fd5976282.json"
            ),
            "mutation_receipt_file_sha256": (
                "1c8eab6576a2d4452dfbcd9aba87563a0a50da4382da0447012e6f95cb1d865b"
            ),
            "mutation_receipt_hash": (
                "d86913b6af93d3487ef3cdbe09a3b47f3519188eea65ebf2f18aaa8fd5976282"
            ),
            "summary_content_hash": (
                "ba4e8bbc99391cc0049810d3fd0e430f6806407b43347f4e5c6e0d1dd29b08a8"
            ),
            "policy": "READ_ONLY_BYTE_IDENTICAL_MIGRATION_SOURCE",
        },
        "immutable v2 projection binding mismatch",
    )
    asset_identity = registry.get("asset_identity") or {}
    require(
        asset_identity.get("classes")
        == [
            "CRYPTO_TOKEN",
            "EQUITY_ISSUER",
            "TOKENIZED_EQUITY",
            "TRADFI_OTHER",
            "UNCLASSIFIED",
        ]
        and asset_identity.get("capture_eligible_asset_class") == "CRYPTO_TOKEN"
        and asset_identity.get("all_other_classes") == "DESCRIPTIVE_ONLY"
        and asset_identity.get("ambiguous_identity") == "FAIL_CLOSED_UNCLASSIFIED"
        and asset_identity.get("cross_class_merge") == "FORBIDDEN",
        "asset identity contract mismatch",
    )
    require(
        registry.get("discovery_surfaces") == _discovery_surface_contracts(),
        "discovery surface contract mismatch",
    )
    require(
        registry.get("lifecycle_states")
        == [
            "SCHEDULED",
            "ACTIVE_PREMARKET",
            "TRANSITION_SCHEDULED",
            "TRANSITIONED_STANDARD",
            "CANCELLED",
            "DELISTING",
            "DELISTED",
            "UNKNOWN",
        ],
        "lifecycle state vocabulary mismatch",
    )
    bybit_semantics = (
        (registry.get("venue_metadata_semantics") or {}).get("bybit") or {}
    )
    require(
        bybit_semantics.get("asset_classification")
        == (
            "symbolType=stock => EQUITY_ISSUER; symbolType in "
            "{commodity,forex,etf} => TRADFI_OTHER; "
            "symbolType=innovation => CRYPTO_TOKEN; "
            "missing/empty/unknown => UNCLASSIFIED"
        ),
        "Bybit asset classification contract mismatch",
    )
    gate_semantics = (
        (registry.get("venue_metadata_semantics") or {}).get("gate") or {}
    )
    okx_semantics = (
        (registry.get("venue_metadata_semantics") or {}).get("okx") or {}
    )
    require(
        gate_semantics.get("prelaunch_predicate")
        == (
            "is_pre_market=true AND in_delisting!=true AND "
            "status in {prelaunch,trading}"
        ),
        "Gate pre-market predicate contract mismatch",
    )
    require(
        okx_semantics.get("asset_classification")
        == (
            "instCategory=1 => CRYPTO_TOKEN; 3 => EQUITY_ISSUER; "
            "4/5/6 => TRADFI_OTHER; missing/empty/unknown => UNCLASSIFIED"
        )
        and gate_semantics.get("asset_classification")
        == (
            "contract_type in {crypto,cryptocurrency,digital_asset} => "
            "CRYPTO_TOKEN; {stock,stocks,equity,equities} => "
            "EQUITY_ISSUER; {metal,metals,index,indices,forex,commodity,"
            "commodities} => TRADFI_OTHER; missing/empty/unknown => UNCLASSIFIED"
        )
        and gate_semantics.get("capture_identity_policy")
        == "MISSING_OR_UNVERIFIED_CONTRACT_TYPE_IS_UNCLASSIFIED_DESCRIPTIVE_ONLY",
        "OKX/Gate asset classification contract mismatch",
    )
    identity_authority = registry.get("relevant_identity_authority") or {}
    require(
        identity_authority.get("retention_scope")
        == "ALL_RELEVANT_IDENTITIES_PER_SURFACE"
        and identity_authority.get("missing_tracked_identity")
        == "ACQUISITION_FAILURE_NO_MUTATION"
        and identity_authority.get("identity_set_hash_mismatch")
        == "FAIL_CLOSED_NO_MUTATION"
        and identity_authority.get("bybit_cross_surface_trading")
        == "TERMINAL_TRANSITION"
        and identity_authority.get("okx_cross_surface_xperp")
        == "TERMINAL_TRANSITION"
        and identity_authority.get("okx_cross_surface_normal")
        == "TERMINAL_TRANSITION"
        and identity_authority.get("untracked_terminal_row")
        == "NO_GENERATION_ALLOCATION"
        and identity_authority.get("bybit_terminal_status_surface")
        == "NOT_PREREGISTERED_IN_V17"
        and identity_authority.get("bybit_unobserved_cancelled_or_closed")
        == "MISSING_TRACKED_IDENTITY_ACQUISITION_FAILURE_NO_INFERENCE",
        "relevant identity authority contract mismatch",
    )
    require(
        registry.get("timestamp_kinds")
        == [
            "premarket_contract_launch_ts",
            "official_spot_t0",
            "first_trade_ts",
            "transition_ts",
            "contract_created_ts",
        ],
        "event timestamps must remain distinct",
    )
    require(
        registry.get("timestamp_producers")
        == {
            "premarket_contract_launch_ts": "VENUE_INSTRUMENT_METADATA",
            "official_spot_t0": "HUMAN_VERIFIED_OFFICIAL_ATTESTATION",
            "transition_ts": (
                "VENUE_METADATA_WHEN_EXPLICIT_ELSE_OBSERVED_LIFECYCLE_"
                "DETECTION_TIME_PROXY"
            ),
            "first_trade_ts": (
                "RESERVED_NO_REGISTRY_PRODUCER_CAPTURE_TRADES_SEPARATE"
            ),
            "contract_created_ts": "VENUE_INSTRUMENT_METADATA_DESCRIPTIVE_ONLY",
        },
        "timestamp producer contract mismatch",
    )
    proxy = registry.get("proxy_policy") or {}
    require(
        proxy.get("evidence_use") == "DESCRIPTIVE_ONLY"
        and proxy.get("capture_eligible") is False
        and proxy.get("acceptance_support") == "FORBIDDEN",
        "proxy event policy mismatch",
    )
    require(
        registry.get("seconds_grade_readiness")
        == {
            "required_precision_sec_lte": 1,
            "current_official_producer_precision_sec": 60,
            "current_official_producer_capable": False,
            "current_result": "DESCRIPTIVE_ONLY_PRECISION_GT_ONE_SECOND",
            "authority_increase": (
                "NEW_IMMUTABLE_PLAN_AND_SECONDS_GRADE_OFFICIAL_PRODUCER_REQUIRED"
            ),
        },
        "seconds-grade official timestamp boundary mismatch",
    )
    lineage_verification = registry.get("capture_lineage_verification") or {}
    require(
        lineage_verification.get("registry_bytes") == "EXACT_HISTORICAL_PREFIX"
        and lineage_verification.get("mutation_receipt") == "EXACT_SEQ_AND_HASH"
        and lineage_verification.get("official_record")
        == "UNSUPERSEDED_OFFICIAL_CRYPTO_ANCHOR"
        and lineage_verification.get("official_source_url_and_identity")
        == "EXACT_OFFICIAL_RECORD_MATCH"
        and lineage_verification.get("current_head_substitution") == "FORBIDDEN"
        and lineage_verification.get("latest_mutation_receipt_plan_identity")
        == "EXACT_ACTIVE_PLAN_ID_AND_HASH",
        "historical capture lineage contract mismatch",
    )
    due = registry.get("capture_due_window") or {}
    require(
        due == {
            "target": "official_spot_t0 - window_before_t0_sec",
            "early_grace_sec": config.CAPTURE_LAUNCH_EARLY_GRACE_SEC,
            "late_grace_sec": config.CAPTURE_LAUNCH_LATE_GRACE_SEC,
            "eligibility_interval": (
                "target - early_grace_sec <= now_ts <= target + late_grace_sec"
            ),
        },
        "capture due-window contract mismatch",
    )
    quotation = (registry.get("official_attestation") or {}).get(
        "quotation_evidence"
    ) or {}
    require(
        quotation == {
            "storage": "VERBATIM_UTF8",
            "normalization": "FORBIDDEN",
            "time_fragment_must_be_verbatim_substring": True,
            "symbol_fragment_must_be_verbatim_substring": True,
            "forbidden_unicode_categories": ["Cc", "Cf"],
        },
        "official quotation evidence contract mismatch",
    )
    attestation = registry.get("official_attestation") or {}
    require(
        attestation.get("writer_clock_recheck")
        == "UNDER_REGISTRY_LOCK_BEFORE_APPEND"
        and attestation.get("metadata_freshness_required") is True
        and attestation.get("current_generation_required") is True
        and attestation.get("precommit_authority_recheck") == "UNDER_REGISTRY_LOCK"
        and attestation.get("venue_roles")
        == {
            "venue": "PERPETUAL_CONTRACT_VENUE",
            "listing_venue": "OFFICIAL_SPOT_ANNOUNCEMENT_VENUE",
        }
        and attestation.get("locked_rebuild_field_preservation")
        == ["venue", "listing_venue"],
        "official attestation lock/freshness contract mismatch",
    )
    require(
        attestation.get("announcement_url_policy")
        == {
            "https_exact_official_host": True,
            "explicit_port": "FORBIDDEN",
            "backslash": "FORBIDDEN",
            "unicode_control_or_format": "FORBIDDEN",
            "fragment": "DISCARDED_BEFORE_STORAGE",
        },
        "official announcement URL provenance policy mismatch",
    )
    mutation_chain = registry.get("mutation_receipt_chain") or {}
    require(
        mutation_chain == _mutation_receipt_chain_contract(),
        "mutation receipt exact record contract mismatch",
    )
    lifecycle = registry.get("active_lifecycle_generation") or {}
    require(
        lifecycle.get("source_of_truth")
        == (
            "latest_verified_mutation_receipt_carrying_last_complete_"
            "metadata_refresh_state"
        ),
        "active lifecycle source-of-truth mismatch",
    )
    require(
        lifecycle.get("active_state_field")
        == "active_lifecycle_generations_by_venue"
        and lifecycle.get("high_water_field")
        == "lifecycle_generation_high_water_by_venue"
        and lifecycle.get("max_complete_metadata_refresh_age_sec")
        == config.MAX_COMPLETE_METADATA_REFRESH_AGE_SEC,
        "active lifecycle generation persistence contract mismatch",
    )
    refresh = registry.get("production_refresh_contract") or {}
    require(
        refresh.get("timestamp_owner") == "WRITER_AFTER_ALL_HTTP_RESPONSES"
        and refresh.get("caller_observed_at_override") == "FORBIDDEN"
        and refresh.get("injected_payload_destination")
        == "EXPLICIT_NONPRODUCTION_PATH_ONLY"
        and refresh.get("precommit_authority_recheck") == "UNDER_REGISTRY_LOCK"
        and refresh.get("concurrency_control") == "RECEIPT_HEAD_COMPARE_AND_SWAP"
        and refresh.get("empty_full_universe_response") == "ACQUISITION_FAILURE"
        and refresh.get("required_nonempty_full_universe_surfaces")
        == list(config.FULL_UNIVERSE_SURFACE_IDS)
        and refresh.get("legitimately_empty_surfaces")
        == list(config.LEGITIMATELY_EMPTY_SURFACE_IDS)
        and refresh.get("retention_scope")
        == "PER_REQUIRED_FULL_UNIVERSE_SURFACE"
        and refresh.get("minimum_prior_universe_retention_ratio")
        == config.MIN_FULL_UNIVERSE_RETENTION_RATIO,
        "production metadata refresh contract mismatch",
    )
    require(
        registry.get("capture_authority_receipt_fields")
        == [
            "mutation_receipt_seq",
            "mutation_receipt_hash",
            "summary_content_sha256",
            "registry_authority_state_hash",
        ],
        "capture authority receipt lineage mismatch",
    )
    require(
        registry.get("capture_lineage_fields")
        == list(config.CAPTURE_LINEAGE_FIELDS),
        "capture lineage vocabulary mismatch",
    )
    require(
        registry.get("lifecycle_generation_transition_policy")
        == {
            "missing_tracked_identity": "ACQUISITION_FAILURE_NO_MUTATION",
            "explicit_terminal": {
                "action": "REMOVE_ACTIVE_GENERATION",
                "phases": [
                    "CANCELLED",
                    "DELISTING",
                    "DELISTED",
                    "TRANSITIONED_STANDARD",
                ],
            },
            "reappearance_after_explicit_terminal": "ALLOCATE_HIGH_WATER_PLUS_ONE",
            "untracked_terminal_row": "IGNORE_WITHOUT_HIGH_WATER_MUTATION",
        },
        "lifecycle generation transition policy mismatch",
    )
    require(
        registry.get("terminal_lifecycle_evidence")
        == {
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
        "terminal lifecycle evidence contract mismatch",
    )
    require(
        registry.get("cross_surface_conflict_policy")
        == {
            "terminal_vs_active": "EXPLICIT_TERMINAL_WINS_OVER_STALE_ACTIVE_ROW",
            "simultaneous_terminal_evidence": (
                "EXACT_VENUE_TIMESTAMP_WINS_OVER_DETECTION_PROXY"
            ),
            "equal_strength_tie_break": "SURFACE_DECLARATION_ORDER",
        },
        "cross-surface conflict policy mismatch",
    )
    venue_semantics = registry.get("venue_metadata_semantics") or {}
    bybit_semantics = venue_semantics.get("bybit") or {}
    okx_semantics = venue_semantics.get("okx") or {}
    gate_semantics = venue_semantics.get("gate") or {}
    require(
        bybit_semantics.get("prelaunch_predicate")
        == "status=PreLaunch AND isPreListing=true"
        and bybit_semantics.get("launchTime")
        == "DESCRIPTIVE_PREMARKET_CONTRACT_LAUNCH_TS",
        "Bybit venue semantics mismatch",
    )
    require(
        okx_semantics.get("surfaces") == ["SWAP", "FUTURES"]
        and okx_semantics.get("prelaunch_predicate")
        == "ruleType=pre_market AND state in {preopen,live}"
        and okx_semantics.get("xperp") == "CROSS_SURFACE_TERMINAL_TRANSITION"
        and okx_semantics.get("normal") == "CROSS_SURFACE_TERMINAL_TRANSITION"
        and okx_semantics.get("terminal_state")
        == {"expired": "DELISTED", "suspend": "DELISTED"},
        "OKX venue semantics mismatch",
    )
    require(
        gate_semantics.get("create_time") == "CONTRACT_CREATED_TS_ONLY"
        and gate_semantics.get("launch_time")
        == "CONTRACT_EXPIRY_NOT_TRADING_LAUNCH"
        and gate_semantics.get("terminal_predicates")
        == {
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
        "Gate venue semantics mismatch",
    )
    evidence = plan.get("capture_evidence") or {}
    require(
        evidence.get("sampling_cadence_sec")
        == {
            "outside_burst": dict(config.PROBE_CADENCE_SEC),
            "burst": dict(config.BURST_CADENCE_SEC),
            "burst_half_width_sec": config.BURST_HALF_WIDTH_SEC,
        },
        "sampling cadence contract mismatch",
    )
    require(
        evidence.get("fixed_exit_offsets_sec")
        == list(config.PRIMARY_EXIT_OFFSETS_SEC)
        and evidence.get("fixed_entry_lead_sec")
        == config.PRIMARY_ENTRY_LEAD_SEC
        and evidence.get("required_probes_by_venue")
        == {
            venue: list(probes)
            for venue, probes in config.REPLAY_REQUIRED_PROBES_BY_VENUE.items()
        },
        "fixed capture target contract mismatch",
    )
    require(
        evidence.get("venue_probe_authority")
        == {
            "okx_orderbook_instrument_identity": (
                "EXACT_BOUND_REQUEST_RESPONSE_DOES_NOT_ECHO_INST_ID"
            ),
            "gate_ticker_exchange_timestamp": (
                "ABSENT_FROM_PAYLOAD_OPTIONAL_DESCRIPTIVE_ONLY"
            ),
        },
        "venue probe authority contract mismatch",
    )
    require(
        evidence.get("entrypoint_policy")
        == {
            "run_capture": "EXACT_STATIC_JSON_SYNTHETIC_FIXTURE_TRANSPORT_ONLY",
            "live_public_market_data": "capture_event_ONLY_AFTER_GATE_TOKEN_AND_CLAIM",
        },
        "capture entrypoint policy mismatch",
    )
    require(
        evidence.get("coverage_clock") == "received_ts"
        and evidence.get("sampling_report_clock") == "received_ts"
        and evidence.get("fixed_entry_evidence_clock")
        == "first_valid_received_ts_at_or_after_target_within_one_cadence"
        and evidence.get("fixed_exit_evidence_clock")
        == "first_valid_received_ts_at_or_after_target_within_one_cadence"
        and evidence.get("pre_target_exit_sample_policy")
        == "FORBIDDEN_NO_LOOKAHEAD"
        and evidence.get("failed_rows_in_readiness_denominator") is True,
        "causal capture evidence clock contract mismatch",
    )
    require(
        evidence.get("per_request_boundary_recheck")
        == [
            "before_each_metadata_or_poll_request",
            "immediately_after_each_metadata_or_poll_response",
        ]
        and evidence.get("shared_gate_process_exit_code") == "MUST_BE_ZERO",
        "per-request/gate fail-closed contract mismatch",
    )
    artifact_commit = evidence.get("artifact_commit") or {}
    require(
        artifact_commit.get("manifest_creation") == "O_EXCL"
        and artifact_commit.get("manifest_immutable") is True
        and artifact_commit.get("receipt_rehashes_samples") is True
        and artifact_commit.get("receipt_samples_hash_must_equal")
        == "manifest.output_sha256",
        "capture artifact commit contract mismatch",
    )
    terminal = evidence.get("terminal_accounting") or {}
    require(
        terminal.get("terminal_run_record_before_claim_release") is True
        and terminal.get("receipt_failure")
        == "FAILED_EXCEPTION_TERMINAL_RECORD_THEN_CLAIM_RELEASE"
        and terminal.get("terminal_record_failure") == "KEEP_CLAIM_FAIL_CLOSED"
        and terminal.get("stop_request_cleanup")
        == "WHILE_OLD_RUN_STILL_OWNS_CLAIM_BEFORE_RELEASE",
        "terminal accounting contract mismatch",
    )
    require(
        evidence.get("multi_trade_response_policy")
        == {
            "okx": "NONEMPTY_ALL_ROWS_EXACT_INSTRUMENT",
            "gate": "NONEMPTY_ALL_ROWS_EXACT_CONTRACT",
        },
        "multi-trade response contract mismatch",
    )
    classification = evidence.get("evidence_classification") or {}
    require(
        classification
        == {
            "ready": "CAUSAL_REPLAY_INPUT_READY",
            "not_ready": "DESCRIPTIVE_ONLY",
            "acceptance_capable": False,
            "classification_is_not_readiness": True,
        },
        "capture evidence classification mismatch",
    )
    replay = plan.get("replay_evidence") or {}
    require(
        replay.get("schema") == "premarket_perp_replay_v2"
        and replay.get("production_mode") == "PRODUCTION_VERIFIED"
        and replay.get("selection_clock") == "received_ts"
        and replay.get("target_selection")
        == "FIRST_VALID_AT_OR_AFTER_WITHIN_ONE_CADENCE"
        and replay.get("pre_target_fallback") == "FORBIDDEN"
        and replay.get("interpolation_or_bracketing") == "FORBIDDEN"
        and replay.get("exchange_ts_use") == "FRESHNESS_FILTER_ONLY"
        and replay.get("output") == "DESCRIPTIVE_GROSS_BBO_MARKOUT_ONLY"
        and replay.get("acceptance_capable") is False
        and replay.get("execution_or_fill_model") is False
        and replay.get("acceptance_decision") == "FORBIDDEN"
        and replay.get("synthetic_mode")
        == "SYNTHETIC_DESCRIPTIVE_ONLY"
        and replay.get("synthetic_mode_selection") == "EXPLICIT_REQUIRED"
        and replay.get("fixed_exit_offsets_sec")
        == list(config.PRIMARY_EXIT_OFFSETS_SEC)
        and replay.get("fixed_entry_lead_sec") == config.PRIMARY_ENTRY_LEAD_SEC
        and replay.get("production_target_override") == "FORBIDDEN"
        and "CAPTURE_AUTHORIZED_SELECTED_PLAN" in (replay.get("integrity") or [])
        and "SELECTED_PLAN_CAPTURE_ROOT" in (replay.get("integrity") or [])
        and replay.get("report_readiness")
        == "SEALED_CAPTURE_READY_AND_ENTRY_AND_ALL_FIXED_EXITS",
        "causal replay contract mismatch",
    )
    require(
        plan.get("registry_quarantine") == _registry_quarantine_contract(),
        "registry quarantine transaction mismatch",
    )
    without_hash = {k: v for k, v in plan.items() if k != "plan_hash"}
    require(plan.get("plan_hash") == canonical_hash(without_hash), "plan hash mismatch")


def write_plan(generated_at_utc: str) -> Path:
    plan = build_plan(generated_at_utc)
    validate_plan(plan)
    content = json.dumps(plan, indent=2, ensure_ascii=False) + "\n"
    path = config.PLAN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o444)
    except FileExistsError as exc:
        if path.read_text(encoding="utf-8") == content:
            return path
        raise PlanBuildError(
            f"immutable artifact mismatch: {path}. Issue a new versioned PlanOnly "
            f"path and supersede this identity; never remove or overwrite it."
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-plan", action="store_true")
    args = parser.parse_args(argv)
    if not args.write_plan:
        raise SystemExit("no action requested")
    generated = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    path = write_plan(generated)
    plan = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps({
        "status": "PLAN_WRITTEN",
        "path": str(path),
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "plan_file_sha256": _sha256_file(path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
