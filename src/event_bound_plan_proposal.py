"""Build a sealed *proposal* for a future event-bound v37 PlanOnly.

This module intentionally cannot activate a plan, rebind the external trust root, mint
a capture token, or contact an exchange.  Its only output is a deterministic JSON
proposal derived from one already-sealed no-capture official-t0 arming receipt.
Publishing and authorising the real v37 PlanOnly remains a separate, explicit user
checkpoint.  v36 is the no-capture integration patch that fixed alert dispatch.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from canonical_hash import canonical_hash
import event_registry as registry
import frozen_plan_bindings as trust_root
import official_t0_arming as arming
import project_config as config
import risk_gate


PROPOSAL_SCHEMA = "premarket_perp_event_bound_plan_proposal_v1"
PROPOSED_PLAN_SCHEMA = "premarket_perp_capture_planonly_v37"
PROPOSED_PLAN_ID = "premarket_perp_capture_20260822_v37"
PROPOSED_PLAN_PATH = (
    "docs/plans/premarket-perp-capture-planonly-20260822-v37.json"
)
ARMING_RECEIPT_SCHEMA = "premarket_official_t0_arming_receipt_v1"
ARMING_RECEIPT_TYPE = "official_t0_arming_receipt"
ARMED_STATUS = "ARMED_NO_CAPTURE_AUTHORITY"
_UTC_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VENUE_HOSTS = {
    "bybit": frozenset({"api.bybit.com"}),
    "okx": frozenset({"www.okx.com"}),
    "gate": frozenset({"api.gateio.ws"}),
}


class ProposalError(RuntimeError):
    """The arming receipt cannot safely produce an event-bound proposal."""


@contextmanager
def _current_arming_head_guard(
    receipt: Mapping[str, Any], *, run_id: str
):
    anchor = receipt.get("event_anchor")
    if not isinstance(anchor, Mapping):
        raise ProposalError("event_anchor is missing or invalid")
    episode_id = _require_text(anchor.get("episode_id"), "event_anchor.episode_id")
    receipt_hash = _require_sha256(receipt.get("receipt_hash"), "receipt_hash")
    try:
        with arming.guard_current_arming_head(
            episode_id=episode_id,
            expected_receipt_hash=receipt_hash,
            run_id=f"proposal-{run_id}",
            arming_root=config.OFFICIAL_T0_ARMING_ROOT,
        ) as current:
            yield current
    except arming.ArmingError as exc:
        raise ProposalError(
            f"supplied receipt is not the current arming head: {exc}"
        ) from exc


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProposalError(f"{field} is missing or invalid")
    return value


def _require_sha256(value: Any, field: str) -> str:
    text = _require_text(value, field)
    if _HEX_SHA256.fullmatch(text) is None:
        raise ProposalError(f"{field} must be a lowercase SHA-256 value")
    return text


def _require_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProposalError(f"{field} must be a non-negative integer")
    return value


def _parse_utc_seconds(value: Any, field: str) -> datetime:
    text = _require_text(value, field)
    if _UTC_SECONDS.fullmatch(text) is None:
        raise ProposalError(f"{field} must be canonical UTC seconds ending in Z")
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ProposalError(f"{field} is not a valid UTC timestamp") from exc
    return parsed


def _validate_no_capture_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    try:
        receipt = arming.validate_arming_receipt(receipt)
    except arming.ArmingError as exc:
        raise ProposalError(f"arming receipt validation failed: {exc}") from exc
    if receipt.get("schema") != ARMING_RECEIPT_SCHEMA:
        raise ProposalError(
            f"arming receipt schema must be {ARMING_RECEIPT_SCHEMA}"
        )
    if receipt.get("record_type") != ARMING_RECEIPT_TYPE:
        raise ProposalError("arming receipt record_type is invalid")
    if receipt.get("status") != ARMED_STATUS:
        raise ProposalError("event is not armed with no-capture authority")
    if receipt.get("capture_authorized") is not False:
        raise ProposalError("armed receipt must not authorize capture")
    if receipt.get("capture_token_issued") is not False:
        raise ProposalError("armed receipt must not issue a capture token")
    if receipt.get("event_bound_plan_generated") is not False:
        raise ProposalError("armed receipt already generated an event-bound plan")
    if "capture_token" in receipt:
        raise ProposalError("armed receipt must not contain capture-token material")

    _require_text(receipt.get("arming_id"), "arming_id")
    revision = _require_nonnegative_int(receipt.get("revision"), "revision")
    predecessor = receipt.get("supersedes_arming_receipt_hash")
    if revision == 0:
        if predecessor is not None:
            raise ProposalError("initial arming receipt cannot supersede another receipt")
    else:
        _require_sha256(predecessor, "supersedes_arming_receipt_hash")
    _parse_utc_seconds(receipt.get("armed_at_utc"), "armed_at_utc")
    _require_text(receipt.get("armed_by"), "armed_by")
    _require_sha256(receipt.get("receipt_hash"), "receipt_hash")

    if receipt.get("plan_id") != trust_root.PLAN_ID:
        raise ProposalError("arming receipt plan_id is not the active no-capture plan")
    if receipt.get("plan_hash") != trust_root.PLAN_HASH:
        raise ProposalError("arming receipt plan_hash is not the active no-capture plan")

    raw_anchor = receipt.get("event_anchor")
    if not isinstance(raw_anchor, Mapping):
        raise ProposalError("event_anchor is missing or invalid")
    missing = [field for field in config.CAPTURE_LINEAGE_FIELDS if field not in raw_anchor]
    if missing:
        raise ProposalError("event_anchor is missing fields: " + ", ".join(missing))
    # Copy only the preregistered lineage vocabulary.  Unknown receipt metadata cannot
    # silently become part of the proposed event authority.
    anchor = {field: raw_anchor[field] for field in config.CAPTURE_LINEAGE_FIELDS}

    text_fields = (
        "episode_id",
        "venue",
        "listing_venue",
        "premarket_contract_id",
        "spot_symbol",
        "official_source_url",
        "official_source_identity",
        "plan_id",
        "asset_class",
        "issuer_namespace",
        "issuer_id",
    )
    for field in text_fields:
        _require_text(anchor.get(field), f"event_anchor.{field}")
    sha_fields = (
        "official_record_hash",
        "registry_sha256",
        "registry_tail_record_hash",
        "mutation_receipt_hash",
        "summary_content_sha256",
        "registry_authority_state_hash",
        "plan_hash",
        "asset_identity_hash",
    )
    for field in sha_fields:
        _require_sha256(anchor.get(field), f"event_anchor.{field}")
    _require_nonnegative_int(
        anchor.get("mutation_receipt_seq"), "event_anchor.mutation_receipt_seq"
    )
    t0 = anchor.get("official_spot_t0")
    if isinstance(t0, bool) or not isinstance(t0, int) or t0 <= 0:
        raise ProposalError("event_anchor.official_spot_t0 must be a positive integer")
    precision = anchor.get("t0_precision_sec")
    if isinstance(precision, bool) or precision != 1:
        raise ProposalError("event_anchor requires exact one-second t0 precision")
    if anchor.get("t0_source_class") != registry.SOURCE_OFFICIAL_ANNOUNCEMENT:
        raise ProposalError("event_anchor must use OFFICIAL_ANNOUNCEMENT")
    if anchor.get("asset_class") != registry.ASSET_CLASS_CRYPTO_TOKEN:
        raise ProposalError("event_anchor must be the explicit CRYPTO_TOKEN asset class")
    if anchor.get("issuer_namespace") != "crypto_asset":
        raise ProposalError("event_anchor issuer_namespace must be crypto_asset")
    venue = str(anchor["venue"])
    if venue not in config.PERP_VENUES:
        raise ProposalError("event_anchor venue is not a supported perpetual venue")
    if not registry.market_symbols_equivalent(
        venue, anchor["premarket_contract_id"], anchor["spot_symbol"]
    ):
        raise ProposalError("event_anchor contract and spot symbol do not match")
    if anchor.get("plan_id") != receipt.get("plan_id"):
        raise ProposalError("event_anchor plan_id does not match the arming receipt")
    if anchor.get("plan_hash") != receipt.get("plan_hash"):
        raise ProposalError("event_anchor plan_hash does not match the arming receipt")

    source = urlsplit(str(anchor["official_source_url"]))
    try:
        port = source.port
    except ValueError as exc:
        raise ProposalError("event_anchor official_source_url has an invalid port") from exc
    if (
        source.scheme != "https"
        or not source.hostname
        or source.username is not None
        or source.password is not None
        or port not in (None, 443)
    ):
        raise ProposalError("event_anchor official_source_url must be public HTTPS")
    return anchor


def _validate_preflight(
    receipt: Mapping[str, Any], *, run_id: str
) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise ProposalError("event-bound proposal preflight returned a non-object")
    expected = {
        "schema": "premarket_write_preflight_v2",
        "ok": True,
        "verified": True,
        "decision": "ALLOW_EVENT_BOUND_PLAN_PROPOSAL",
        "write_class": "event_bound_plan_proposal",
        "run_id": run_id,
        "action": risk_gate.EVENT_BOUND_PLAN_PROPOSAL_ACTION,
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
    }
    mismatched = [
        field for field, expected_value in expected.items()
        if receipt.get(field) != expected_value
    ]
    if mismatched:
        raise ProposalError(
            "event-bound proposal preflight is not exact: " + ", ".join(mismatched)
        )
    _require_sha256(receipt.get("resolved_paths_hash"), "resolved_paths_hash")
    if "capture_token" in receipt or receipt.get("capture_token_issued") is True:
        raise ProposalError("event-bound proposal preflight issued capture authority")
    return dict(receipt)


def _utc_seconds(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _selected_market_endpoints(venue: str) -> list[list[str]]:
    hosts = _VENUE_HOSTS[venue]
    selected = [
        [host, path]
        for host, path in config.MARKET_DATA_ALLOWED_ENDPOINTS
        if host in hosts
    ]
    if not selected:
        raise ProposalError(f"no public market-data endpoints are bound for {venue}")
    return selected


def build_event_bound_plan_proposal(
    arming_receipt: Mapping[str, Any],
    *,
    generated_at_utc: str,
) -> dict[str, Any]:
    """Return a deterministic, non-authorising proposal for one exact event."""
    if not isinstance(arming_receipt, Mapping):
        raise ProposalError("arming receipt must be an object")
    anchor = _validate_no_capture_receipt(arming_receipt)
    generated_at = _parse_utc_seconds(generated_at_utc, "generated_at_utc")
    armed_at = _parse_utc_seconds(arming_receipt.get("armed_at_utc"), "armed_at_utc")
    if generated_at < armed_at:
        raise ProposalError("generated_at_utc cannot precede armed_at_utc")
    generated_ts = int(generated_at.timestamp())
    t0 = int(anchor["official_spot_t0"])
    capture_start = t0 - config.CAPTURE_WINDOW_BEFORE_SEC
    if generated_ts > capture_start:
        raise ProposalError("event no longer has the full pre-listing capture window")

    venue = str(anchor["venue"])
    proposal: dict[str, Any] = {
        "schema": PROPOSAL_SCHEMA,
        "generated_at_utc": generated_at_utc,
        "proposal_mode": "CREATE_ONLY_PROPOSAL_NO_TRUST_ROOT_REBIND",
        "proposed_plan_schema": PROPOSED_PLAN_SCHEMA,
        "proposed_plan_id": PROPOSED_PLAN_ID,
        "proposed_plan_path": PROPOSED_PLAN_PATH,
        "proposed_plan_status": "EVENT_BOUND_VISIBLE_CAPTURE_REQUIRES_APPROVAL",
        "supersedes_plan_id": trust_root.PLAN_ID,
        "supersedes_plan_hash": trust_root.PLAN_HASH,
        "supersedes_plan_file_sha256": trust_root.PLAN_FILE_SHA256,
        "arming_receipt": {
            "arming_id": arming_receipt["arming_id"],
            "revision": arming_receipt["revision"],
            "receipt_hash": arming_receipt["receipt_hash"],
            "armed_at_utc": arming_receipt["armed_at_utc"],
            "armed_by": arming_receipt["armed_by"],
            "plan_id": arming_receipt["plan_id"],
            "plan_hash": arming_receipt["plan_hash"],
        },
        "event_anchor": anchor,
        "historical_registry_anchor": {
            "registry_sha256": anchor["registry_sha256"],
            "registry_tail_record_hash": anchor["registry_tail_record_hash"],
            "mutation_receipt_seq": anchor["mutation_receipt_seq"],
            "mutation_receipt_hash": anchor["mutation_receipt_hash"],
            "summary_content_sha256": anchor["summary_content_sha256"],
            "registry_authority_state_hash": anchor[
                "registry_authority_state_hash"
            ],
            "anchor_plan_id": anchor["plan_id"],
            "anchor_plan_hash": anchor["plan_hash"],
        },
        "current_lifecycle_snapshot": "REQUIRED_UNDER_V37_BEFORE_CAPTURE",
        "capture_bounds": {
            "official_spot_t0": t0,
            "capture_start_ts": capture_start,
            "capture_end_ts": t0 + config.CAPTURE_WINDOW_AFTER_SEC,
            "window_before_sec": config.CAPTURE_WINDOW_BEFORE_SEC,
            "window_after_sec": config.CAPTURE_WINDOW_AFTER_SEC,
            "max_runtime_sec": config.MAX_CAPTURE_RUNTIME_SEC,
            "max_requests": config.MAX_REQUESTS_PER_CAPTURE,
            "max_events": config.MAX_EVENTS_PER_CAPTURE,
            "launch_early_grace_sec": config.CAPTURE_LAUNCH_EARLY_GRACE_SEC,
            "launch_late_grace_sec": config.CAPTURE_LAUNCH_LATE_GRACE_SEC,
        },
        "public_endpoint_proposal": {
            "venue": venue,
            "selected_market_data_endpoints": _selected_market_endpoints(venue),
            "authenticated_endpoints_allowed": False,
            "order_endpoints_allowed": False,
            "requires_exact_v37_capability_scan": True,
        },
        "implementation_binding": {
            "mode": "RECOMPUTE_AND_FREEZE_ALL_CODE_SHA256_AT_V37_ISSUE",
            "active_plan_file_sha256": trust_root.PLAN_FILE_SHA256,
            "v37_plan_hash_assigned": False,
        },
        "execution_prohibitions": {
            "private_api": True,
            "authenticated_api": True,
            "venue_orders": True,
            "venue_paper_or_testnet_execution": True,
            "live_execution": True,
            "real_capital": True,
            "leverage_or_margin_changes": True,
        },
        "capture_authorized": False,
        "capture_token_issued": False,
        "trust_root_rebound": False,
        "requires_explicit_user_capture_approval": True,
        "requires_new_immutable_v37_plan": True,
        "acceptance_capable": False,
    }
    proposal["event_binding_hash"] = canonical_hash({
        "arming_receipt_hash": arming_receipt["receipt_hash"],
        "event_anchor": anchor,
    })
    proposal["proposal_hash"] = canonical_hash(proposal)
    return proposal


def write_event_bound_plan_proposal(
    arming_receipt: Mapping[str, Any],
    *,
    run_id: str,
    preflight: Any | None = None,
    clock: Any | None = None,
) -> Path:
    """Preflight twice and create one root-confined proposal with fresh wall time."""
    run_id = _require_text(run_id, "run_id")
    clock_fn = clock or time.time
    initial_ts = int(clock_fn())
    # Validate the complete receipt and remaining event window before any preflight or
    # directory write.  The same checks are repeated using a fresh clock at commit.
    build_event_bound_plan_proposal(
        arming_receipt, generated_at_utc=_utc_seconds(initial_ts)
    )
    preflight_call = preflight or risk_gate.preflight
    try:
        initial_raw = preflight_call(
            write_class="event_bound_plan_proposal", run_id=run_id
        )
    except Exception as exc:  # noqa: BLE001 - proposal write must fail closed
        raise ProposalError(
            f"event-bound proposal preflight failed: {type(exc).__name__}: {exc}"
        ) from exc
    initial_preflight = _validate_preflight(initial_raw, run_id=run_id)

    validated = arming.validate_arming_receipt(arming_receipt)
    arming_id = _require_text(validated.get("arming_id"), "arming_id")
    if re.fullmatch(r"arming-[a-z0-9-]{3,64}", arming_id) is None:
        raise ProposalError("arming_id is not path-safe")
    revision = _require_nonnegative_int(validated.get("revision"), "revision")
    receipt_hash = _require_sha256(validated.get("receipt_hash"), "receipt_hash")
    with _current_arming_head_guard(validated, run_id=run_id):
        try:
            commit_raw = preflight_call(
                write_class="event_bound_plan_proposal", run_id=run_id
            )
        except Exception as exc:  # noqa: BLE001 - fail closed before O_EXCL
            raise ProposalError(
                f"event-bound proposal commit preflight failed: {type(exc).__name__}: {exc}"
            ) from exc
        commit_preflight = _validate_preflight(commit_raw, run_id=run_id)
        authority_fields = ("plan_id", "plan_hash", "resolved_paths_hash")
        if any(
            commit_preflight.get(field) != initial_preflight.get(field)
            for field in authority_fields
        ):
            raise ProposalError("event-bound proposal authority changed before commit")

        # The final clock is sampled only after the commit preflight.  No preflight
        # latency can therefore be hidden behind an earlier generated_at timestamp.
        final_write_ts = max(initial_ts, int(clock_fn()))
        proposal = build_event_bound_plan_proposal(
            validated, generated_at_utc=_utc_seconds(final_write_ts)
        )
        proposal.pop("proposal_hash", None)
        proposal["proposal_write_authority"] = {
            "run_id": run_id,
            "decision": commit_preflight["decision"],
            "plan_id": commit_preflight["plan_id"],
            "plan_hash": commit_preflight["plan_hash"],
            "resolved_paths_hash": commit_preflight["resolved_paths_hash"],
        }
        proposal["proposal_hash"] = canonical_hash(proposal)

        root = Path(config.EVENT_BOUND_PLAN_PROPOSAL_ROOT)
        path = root / arming_id / (
            f"{revision:020d}-{receipt_hash}-v37-proposal.json"
        )
        payload = (
            json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ProposalError(f"proposal already exists: {path}") from exc
        except OSError as exc:
            raise ProposalError(f"cannot create proposal {path}: {exc}") from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            # The path was created with O_EXCL. Never delete or replace a possibly
            # partial evidence artifact automatically; inspect it explicitly.
            raise
        return path
