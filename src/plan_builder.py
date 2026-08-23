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


SCHEMA = "premarket_perp_capture_planonly_v9"
PLAN_ID = "premarket_perp_capture_20260822_v9"
SUPERSEDES_PLAN_ID = "premarket_perp_capture_20260822_v8"
SUPERSEDES_PLAN_HASH = "fb9a44f17ca2f3ffcb8f9ef87c7e9ad42684bfd80ad03dfe5ad48d05f34d223f"
SUPERSEDES_PLAN_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v8.json"
HASH_METHOD = "sha256_canonical_json_excluding_plan_hash"


class PlanBuildError(ValueError):
    pass


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        raise PlanBuildError(f"bound file missing: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        "status": "CAPTURE_IMPLEMENTATION_AUDIT_GREEN_NO_CAPTURE",
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
            "shared_single_writer": (
                "the workspace-wide active-run gate must be open and the shared "
                "market-data writer claim unheld; a stale claim is reported, never "
                "cleared automatically"
            ),
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
            "schema": "premarket_perp_event_registry_v2",
            "path": "docs/registry/listing-events-v2.jsonl",
            "legacy_v1_path": "docs/registry/listing-events.jsonl",
            "timestamp_kinds": [
                "premarket_contract_launch_ts",
                "official_spot_t0",
                "first_trade_ts",
                "transition_ts",
                "contract_created_ts",
            ],
            "source_classes": [
                "OFFICIAL_ANNOUNCEMENT",
                "VENUE_INSTRUMENT_METADATA",
                "OBSERVED_PUBLIC_TRADE",
                "OBSERVED_LIFECYCLE",
            ],
            "acceptance_anchor": {
                "timestamp_kind": "official_spot_t0",
                "source_class": "OFFICIAL_ANNOUNCEMENT",
            },
            "proxy_policy": (
                "venue metadata, first-trade observations and observed lifecycle "
                "timestamps are DESCRIPTIVE_ONLY and cannot enter capture selection"
            ),
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
            },
            "official_t0_precision_sec": 60,
            "seconds_grade_replay_precision_sec": 1,
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
                "mappings plus monotonic per-contract high-water state; disappearance, "
                "cancellation or transition removes the active generation, and a later "
                "reappearance allocates high_water+1 even when no new t0 is present"
            ),
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
                "minimum_prior_universe_retention_ratio": (
                    config.MIN_FULL_UNIVERSE_RETENTION_RATIO
                ),
                "abrupt_or_truncated_response": "ACQUISITION_FAILURE_NO_STATE_MUTATION",
            },
            "capture_authority_lineage_fields": [
                "mutation_receipt_seq",
                "mutation_receipt_hash",
                "summary_content_sha256",
                "registry_authority_state_hash",
            ],
            "mutation_receipt_chain": {
                "creation": "O_EXCL",
                "immutable": True,
                "link_field": "previous_mutation_receipt_hash",
                "anchors": [
                    "registry_sha256",
                    "registry_head_record_hash",
                    "summary_content_hash",
                    "active_lifecycle_generations_by_venue",
                    "lifecycle_generation_high_water_by_venue",
                    "last_complete_metadata_refresh_received_at_utc",
                    "raw_universe_rows_by_venue",
                    "plan_id",
                    "plan_hash",
                    "run_id",
                ],
                "failure_recovery_boundary": (
                    "LOCAL_CRASH_RECOVERY_EVIDENCE_ONLY_NOT_CRYPTOGRAPHIC_AUTHENTICITY"
                ),
                "process_crash_recovery": (
                    "FAIL_CLOSED_MANUAL_RECOVERY_NO_AUTOMATIC_ATOMICITY_CLAIM"
                ),
                "wal_implemented": False,
            },
            "locking": (
                "metadata refresh uses an O_EXCL registry lock from load and lineage "
                "verification through append, fsync, summary and immutable receipt; "
                "in-process exceptions roll back, but process/power loss fails closed "
                "and requires manual recovery because no durable WAL exists"
            ),
            "venue_metadata_semantics": {
                "bybit": (
                    "status=PreLaunch,isPreListing=true LinearPerpetual launchTime; "
                    "descriptive contract launch only"
                ),
                "okx": (
                    "instType=FUTURES,ruleType=pre_market listTime is active; xperp "
                    "is lifecycle-relevant terminal transition and is never active "
                    "pre-market"
                ),
                "gate": (
                    "status=prelaunch create_time; contract_created_ts only, not "
                    "trading start"
                ),
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
            "required_probes": ["trades", "orderbook", "ticker"],
            "coverage_clock": "received_ts",
            "sampling_report_clock": "received_ts",
            "exchange_timestamp_policy": {
                "required": True,
                "max_staleness_sec_by_probe": dict(config.MAX_SAMPLE_STALENESS_SEC),
                "max_future_skew_sec": config.MAX_EXCHANGE_FUTURE_SKEW_SEC,
            },
            "max_burst_gap_cadence_multiplier": (
                config.MAX_BURST_GAP_CADENCE_MULTIPLIER
            ),
            "readiness": (
                "all probes need causal structurally valid payloads, full two-sided "
                "burst-window coverage, bounded successful-sample gaps and evidence "
                "at every preregistered exit; process completion is not replay readiness"
            ),
            "fixed_exit_evidence_clock": (
                "first_valid_received_ts_at_or_after_target_within_one_cadence"
            ),
            "pre_target_exit_sample_policy": "FORBIDDEN_NO_LOOKAHEAD",
            "lineage": (
                "manifest and immutable receipt carry episode id, exact official "
                "record/source, registry and summary hashes/tail, and active PlanOnly "
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
        },
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
        ],
        "activation_gate": {
            "capture_authorized": False,
            "reason": (
                "v9 binds the independently reviewed implementation, but status "
                "CAPTURE_IMPLEMENTATION_AUDIT_GREEN_NO_CAPTURE intentionally excludes "
                "market_data_capture; neither a direct mint nor a capture preflight can "
                "authorize network capture"
            ),
            "required_next_checkpoint": (
                "a new immutable PlanOnly must explicitly authorize market_data_capture; "
                "activating a scheduler or collector remains a separate user-approved "
                "visible-run action"
            ),
        },
        "acceptance_policy": {
            "evidence_class": "PUBLIC_PREMARKET_PERP_CAPTURE",
            "acceptance_decision": "NONE_CAPTURE_ONLY",
            "note": (
                "no metric computed from this capture supports ACCEPT or REJECT of "
                "any strategy; a separate user-checkpointed plan is required for that"
            ),
        },
        "plan_hash_method": HASH_METHOD,
    }
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
        plan.get("status") == "CAPTURE_IMPLEMENTATION_AUDIT_GREEN_NO_CAPTURE",
        "v9 must remain capture-disabled",
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
        plan.get("activation_gate", {}).get("capture_authorized") is False,
        "v9 must not authorize capture",
    )
    require(
        plan.get("authorized_after_gate_green") == [
            "refresh the public metadata event registry after metadata preflight",
            "verify and materialize descriptive proxy observations offline",
            "append one human-verified official spot t0 after attestation preflight",
        ],
        "authorized action set differs from the capture-disabled v9 contract",
    )
    require(bool(plan.get("allowed_endpoints")), "plan must declare its endpoint allow-list")
    require(
        set((plan.get("resolved_path_bindings") or {}))
        == {"shared_gate_path", "shared_writer_claim_path", "capture_root"},
        "resolved path bindings are incomplete",
    )
    registry = plan.get("event_registry") or {}
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
        and attestation.get("precommit_authority_recheck") == "UNDER_REGISTRY_LOCK",
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
    require(mutation_chain.get("creation") == "O_EXCL", "mutation receipts must be exclusive")
    require(mutation_chain.get("immutable") is True, "mutation receipts must be immutable")
    require(
        mutation_chain.get("failure_recovery_boundary")
        == "LOCAL_CRASH_RECOVERY_EVIDENCE_ONLY_NOT_CRYPTOGRAPHIC_AUTHENTICITY",
        "mutation receipt trust boundary mismatch",
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
        and refresh.get("minimum_prior_universe_retention_ratio")
        == config.MIN_FULL_UNIVERSE_RETENTION_RATIO,
        "production metadata refresh contract mismatch",
    )
    require(
        registry.get("capture_authority_lineage_fields")
        == [
            "mutation_receipt_seq",
            "mutation_receipt_hash",
            "summary_content_sha256",
            "registry_authority_state_hash",
        ],
        "capture authority receipt lineage mismatch",
    )
    require(
        mutation_chain.get("process_crash_recovery")
        == "FAIL_CLOSED_MANUAL_RECOVERY_NO_AUTOMATIC_ATOMICITY_CLAIM"
        and mutation_chain.get("wal_implemented") is False,
        "registry crash-recovery boundary mismatch",
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
    without_hash = {k: v for k, v in plan.items() if k != "plan_hash"}
    require(plan.get("plan_hash") == canonical_hash(without_hash), "plan hash mismatch")


def write_plan(generated_at_utc: str) -> Path:
    plan = build_plan(generated_at_utc)
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
