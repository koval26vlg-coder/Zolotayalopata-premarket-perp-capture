"""Pure, create-only validator for a future event-bound v43 capture plan.

This module is deliberately additive and non-authoritative while v42 is active.  It
can structurally bind an arming proposal, a fresh public lifecycle snapshot and an
explicit one-attempt review receipt into a candidate.  It does not verify durable
proposal/registry/approval heads and therefore marks every result non-issuable.  It
cannot write a PlanOnly, edit the trust root, mint a capture token or contact a venue.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import urlsplit

from canonical_hash import canonical_hash
import event_registry as registry
import frozen_plan_bindings as trust_root
import project_config as config
import risk_gate


CANDIDATE_SCHEMA = "premarket_perp_v43_event_binding_candidate_v1"
PROPOSAL_SCHEMA = "premarket_perp_event_bound_plan_proposal_v1"
LIFECYCLE_SCHEMA = "premarket_perp_lifecycle_snapshot_v43_candidate"
APPROVAL_SCHEMA = "premarket_perp_capture_approval_v43_candidate"
PROPOSED_PLAN_SCHEMA = "premarket_perp_capture_planonly_v43"
PROPOSED_PLAN_ID = "premarket_perp_capture_20260822_v43"
OFFICIAL_SOURCE_CLASS = "OFFICIAL_ANNOUNCEMENT"
CRYPTO_ASSET_CLASS = "CRYPTO_TOKEN"
MAX_LIFECYCLE_AGE_SEC = 60
MAX_APPROVAL_TTL_SEC = 300
MIN_ACTIVATION_LEAD_SEC = 600
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")

_PROPOSAL_FIELDS = frozenset({
    "acceptance_capable", "arming_receipt", "capture_authorized",
    "capture_bounds", "capture_token_issued", "current_lifecycle_snapshot",
    "event_anchor", "event_binding_hash", "execution_prohibitions",
    "generated_at_utc", "historical_registry_anchor", "implementation_binding",
    "proposal_hash", "proposal_mode", "proposal_write_authority",
    "proposed_plan_id", "proposed_plan_path", "proposed_plan_schema",
    "proposed_plan_status", "public_endpoint_proposal",
    "requires_explicit_user_capture_approval", "requires_new_immutable_v43_plan",
    "schema", "supersedes_plan_file_sha256", "supersedes_plan_hash",
    "supersedes_plan_id", "trust_root_rebound",
})
_ANCHOR_FIELDS = frozenset(config.CAPTURE_LINEAGE_FIELDS)
_ARMING_SUMMARY_FIELDS = frozenset({
    "arming_id", "revision", "receipt_hash", "armed_at_utc", "armed_by",
    "plan_id", "plan_hash",
})
_BOUNDS_FIELDS = frozenset({
    "official_spot_t0", "capture_start_ts", "capture_end_ts",
    "window_before_sec", "window_after_sec", "max_runtime_sec", "max_requests",
    "max_events", "launch_early_grace_sec", "launch_late_grace_sec",
})
_LIFECYCLE_FIELDS = frozenset({
    "schema", "received_at_utc", "proposal_hash", "event_binding_hash", "venue",
    "premarket_contract_id", "spot_symbol", "official_spot_t0", "phase",
    "tradable", "terminal", "transition_state", "raw_payload_sha256",
    "request_identity_sha256", "exchange_ts", "received_ts", "ws_binding",
    "contract_spec", "snapshot_hash",
})
_WS_FIELDS = frozenset({"scheme", "host", "port", "path", "channels"})
_CONTRACT_FIELDS = frozenset({
    "contract_size", "tick_size", "qty_step", "min_qty", "max_qty",
    "taker_fee_rate", "funding_interval_sec", "maintenance_margin_rate",
    "price_limit_model", "source_sha256",
})
_APPROVAL_FIELDS = frozenset({
    "schema", "approval_id", "approval_nonce", "approval_mode", "approved_by",
    "approved_at_utc", "expires_at_utc", "proposal_hash", "event_binding_hash",
    "lifecycle_snapshot_hash", "approved_capture_attempts", "approval_consumed",
    "public_data_only", "orders_allowed", "capture_token_issued", "approval_hash",
})
_OFFICIAL_SOURCE_HOSTS = {
    venue: frozenset(hosts)
    for venue, hosts in config.OFFICIAL_ANNOUNCEMENT_HOSTS.items()
}
_REST_MARKET_HOSTS = {
    "bybit": frozenset({"api.bybit.com"}),
    "okx": frozenset({"www.okx.com"}),
    "gate": frozenset({"api.gateio.ws"}),
}

# v43 is event-specific.  Only the Bybit single-connection profile is sealed by this
# candidate schema today.  OKX needs public+business sockets and Gate needs an exact
# REST-snapshot bootstrap binding, so neither may be represented by this one-object
# shape and both must fail closed until a later reviewed schema binds them exactly.
_SINGLE_CONNECTION_WS_PROFILE = {
    "bybit": {
        "host": "stream.bybit.com",
        "port": 443,
        "path": "/v5/public/linear",
        "channel_templates": (
            "orderbook.50.{symbol}",
            "publicTrade.{symbol}",
            "tickers.{symbol}",
            "priceLimit.{symbol}",
        ),
    },
}


class EventBindingError(ValueError):
    """Candidate material cannot safely bind one future capture."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EventBindingError(message)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{field} must be an object")
    return value


def _exact_mapping(
    value: Any, field: str, expected_fields: frozenset[str]
) -> Mapping[str, Any]:
    result = _mapping(value, field)
    actual = frozenset(result)
    _require(
        actual == expected_fields,
        f"{field} fields mismatch: missing={sorted(expected_fields - actual)}, "
        f"extra={sorted(actual - expected_fields)}",
    )
    return result


def _text(value: Any, field: str) -> str:
    _require(isinstance(value, str) and value.strip() == value and bool(value),
             f"{field} must be a canonical nonempty string")
    return value


def _hash(value: Any, field: str) -> str:
    _require(isinstance(value, str) and _SHA256.fullmatch(value) is not None,
             f"{field} must be a lowercase SHA-256")
    return value


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    _require(not isinstance(value, bool) and isinstance(value, (int, float)),
             f"{field} must be numeric")
    result = float(value)
    _require(math.isfinite(result), f"{field} must be finite")
    if positive:
        _require(result > 0, f"{field} must be positive")
    return result


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    _require(not isinstance(value, bool) and isinstance(value, int),
             f"{field} must be an integer")
    if positive:
        _require(value > 0, f"{field} must be positive")
    return value


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    _require(
        not isinstance(value, bool) and isinstance(value, (int, float)),
        f"{field} must be a JSON number",
    )
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise EventBindingError(f"{field} must be numeric") from exc
    _require(result.is_finite(), f"{field} must be finite")
    if positive:
        _require(result > 0, f"{field} must be positive")
    return result


def _utc_seconds(value: Any, field: str) -> int:
    text = _text(value, field)
    _require(text.endswith("Z") and "." not in text,
             f"{field} must be exact UTC seconds")
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise EventBindingError(f"{field} must be exact UTC seconds") from exc
    return int(parsed.timestamp())


def _verify_canonical_hash(record: Mapping[str, Any], field: str) -> str:
    expected = _hash(record.get(field), field)
    unsigned = {key: value for key, value in record.items() if key != field}
    _require(expected == canonical_hash(unsigned), f"{field} does not match content")
    return expected


def issue_readiness(
    *,
    proposal: Mapping[str, Any] | None,
    lifecycle_snapshot: Mapping[str, Any] | None,
    approval_receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Report missing event-specific authority without producing a placeholder plan."""
    values = {
        "approval_receipt": approval_receipt,
        "lifecycle_snapshot": lifecycle_snapshot,
        "proposal": proposal,
    }
    missing = sorted(key for key, value in values.items() if value is None)
    return {
        "schema": "premarket_perp_v43_issue_readiness_v1",
        "status": "NOT_ISSUED_EVENT_REQUIRED" if missing else "EVENT_MATERIAL_PRESENT_UNVERIFIED",
        "missing": missing,
        "capture_authorized": False,
        "capture_token_issued": False,
    }


def _validate_proposal(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    proposal = _exact_mapping(value, "proposal", _PROPOSAL_FIELDS)
    _require(proposal.get("schema") == PROPOSAL_SCHEMA, "proposal schema is invalid")
    _require(
        proposal.get("proposal_mode") == "CREATE_ONLY_PROPOSAL_NO_TRUST_ROOT_REBIND",
        "proposal is not create-only",
    )
    _require(proposal.get("proposed_plan_schema") == PROPOSED_PLAN_SCHEMA,
             "proposal plan schema is not v43")
    _require(proposal.get("proposed_plan_id") == PROPOSED_PLAN_ID,
             "proposal plan id is not v43")
    _require(
        proposal.get("proposed_plan_path")
        == "docs/plans/premarket-perp-capture-planonly-20260822-v43.json",
        "proposal plan path is not the immutable v43 path",
    )
    _require(
        proposal.get("proposed_plan_status")
        == "EVENT_BOUND_VISIBLE_CAPTURE_REQUIRES_APPROVAL",
        "proposal plan status is invalid",
    )
    _require(
        proposal.get("supersedes_plan_id") == trust_root.PLAN_ID
        and proposal.get("supersedes_plan_hash") == trust_root.PLAN_HASH
        and proposal.get("supersedes_plan_file_sha256")
        == trust_root.PLAN_FILE_SHA256,
        "proposal does not supersede the exact active no-capture plan",
    )
    _require(proposal.get("capture_authorized") is False,
             "proposal must not self-authorize capture")
    _require(proposal.get("capture_token_issued") is False,
             "proposal must not contain a capture token")
    _require(proposal.get("trust_root_rebound") is False,
             "proposal must not claim a trust-root rebind")
    _require(
        proposal.get("requires_explicit_user_capture_approval") is True
        and proposal.get("requires_new_immutable_v43_plan") is True
        and proposal.get("acceptance_capable") is False,
        "proposal boundary flags are invalid",
    )
    _verify_canonical_hash(proposal, "proposal_hash")

    anchor = _exact_mapping(proposal.get("event_anchor"), "event_anchor", _ANCHOR_FIELDS)
    _require(anchor.get("t0_source_class") == OFFICIAL_SOURCE_CLASS,
             "event t0 must be OFFICIAL_ANNOUNCEMENT")
    _require(anchor.get("t0_precision_sec") == 1,
             "event t0 must have seconds-grade precision")
    _require(anchor.get("asset_class") == CRYPTO_ASSET_CLASS,
             "event asset class must be CRYPTO_TOKEN")
    for field in (
        "episode_id", "venue", "listing_venue", "premarket_contract_id",
        "spot_symbol", "official_source_url", "official_source_identity",
        "issuer_namespace", "issuer_id",
    ):
        _text(anchor.get(field), f"event_anchor.{field}")
    for field in (
        "official_record_hash", "registry_sha256", "registry_tail_record_hash",
        "mutation_receipt_hash", "summary_content_sha256",
        "registry_authority_state_hash", "plan_hash", "asset_identity_hash",
    ):
        _hash(anchor.get(field), f"event_anchor.{field}")
    _integer(anchor.get("official_spot_t0"), "event_anchor.official_spot_t0", positive=True)
    _integer(anchor.get("mutation_receipt_seq"), "event_anchor.mutation_receipt_seq")
    _require(
        anchor.get("plan_id") == trust_root.PLAN_ID
        and anchor.get("plan_hash") == trust_root.PLAN_HASH,
        "event anchor is not bound to the active no-capture plan",
    )
    _require(anchor.get("issuer_namespace") == "crypto_asset",
             "event anchor issuer namespace is invalid")
    venue = str(anchor["venue"])
    _require(
        venue in config.PERP_VENUES and venue in _REST_MARKET_HOSTS,
        "event anchor venue is unsupported",
    )
    _require(
        registry.market_symbols_equivalent(
            venue,
            str(anchor["premarket_contract_id"]),
            str(anchor["spot_symbol"]),
        ),
        "event contract and spot symbol are not equivalent",
    )

    listing_venue = str(anchor["listing_venue"])
    source_hosts = _OFFICIAL_SOURCE_HOSTS.get(listing_venue)
    _require(source_hosts is not None, "listing venue has no official source profile")
    parsed = urlsplit(str(anchor["official_source_url"]))
    try:
        port = parsed.port
    except ValueError as exc:
        raise EventBindingError("official source URL has an invalid port") from exc
    path = parsed.path
    path_segments = path.split("/")
    canonical_path = bool(
        path.startswith("/")
        and path != "/"
        and "\\" not in path
        and "%" not in path
        and not any(ord(char) <= 0x20 or ord(char) == 0x7F for char in path)
        and all(segment not in {".", ".."} for segment in path_segments)
    )
    _require(
        parsed.scheme == "https"
        and parsed.hostname in source_hosts
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and canonical_path,
        "official source URL is not the exact public listing-venue profile",
    )

    arming = _exact_mapping(
        proposal.get("arming_receipt"), "arming_receipt", _ARMING_SUMMARY_FIELDS
    )
    arming_hash = _hash(arming.get("receipt_hash"), "arming_receipt.receipt_hash")
    _require(
        arming.get("plan_id") == trust_root.PLAN_ID
        and arming.get("plan_hash") == trust_root.PLAN_HASH,
        "arming summary is not bound to the active no-capture plan",
    )
    _require(
        isinstance(arming.get("revision"), int)
        and not isinstance(arming.get("revision"), bool)
        and int(arming["revision"]) >= 0,
        "arming receipt revision is invalid",
    )
    _require(
        _SAFE_ID.fullmatch(str(arming.get("arming_id") or "")) is not None,
        "arming id is invalid",
    )
    _utc_seconds(arming.get("armed_at_utc"), "arming_receipt.armed_at_utc")
    _text(arming.get("armed_by"), "arming_receipt.armed_by")
    expected_event_binding = canonical_hash(
        {"arming_receipt_hash": arming_hash, "event_anchor": dict(anchor)}
    )
    _require(proposal.get("event_binding_hash") == expected_event_binding,
             "proposal event binding hash is invalid")

    history = _exact_mapping(
        proposal.get("historical_registry_anchor"),
        "historical_registry_anchor",
        frozenset({
            "registry_sha256", "registry_tail_record_hash", "mutation_receipt_seq",
            "mutation_receipt_hash", "summary_content_sha256",
            "registry_authority_state_hash", "anchor_plan_id", "anchor_plan_hash",
        }),
    )
    history_expectations = {
        "registry_sha256": anchor["registry_sha256"],
        "registry_tail_record_hash": anchor["registry_tail_record_hash"],
        "mutation_receipt_seq": anchor["mutation_receipt_seq"],
        "mutation_receipt_hash": anchor["mutation_receipt_hash"],
        "summary_content_sha256": anchor["summary_content_sha256"],
        "registry_authority_state_hash": anchor["registry_authority_state_hash"],
        "anchor_plan_id": trust_root.PLAN_ID,
        "anchor_plan_hash": trust_root.PLAN_HASH,
    }
    _require(dict(history) == history_expectations,
             "historical registry anchor does not match event lineage")

    authority = _exact_mapping(
        proposal.get("proposal_write_authority"),
        "proposal_write_authority",
        frozenset({"run_id", "decision", "plan_id", "plan_hash", "resolved_paths_hash"}),
    )
    _text(authority.get("run_id"), "proposal_write_authority.run_id")
    _require(
        authority.get("decision") == "ALLOW_EVENT_BOUND_PLAN_PROPOSAL"
        and authority.get("plan_id") == trust_root.PLAN_ID
        and authority.get("plan_hash") == trust_root.PLAN_HASH
        and authority.get("resolved_paths_hash")
        == canonical_hash(risk_gate.resolved_path_bindings()),
        "proposal write authority is not current",
    )

    _require(
        proposal.get("current_lifecycle_snapshot")
        == "REQUIRED_UNDER_V43_BEFORE_CAPTURE",
        "proposal lifecycle requirement is invalid",
    )
    endpoints = _exact_mapping(
        proposal.get("public_endpoint_proposal"),
        "public_endpoint_proposal",
        frozenset({
            "venue", "selected_market_data_endpoints", "authenticated_endpoints_allowed",
            "order_endpoints_allowed", "requires_exact_v43_capability_scan",
        }),
    )
    _require(
        endpoints.get("venue") == anchor.get("venue")
        and endpoints.get("authenticated_endpoints_allowed") is False
        and endpoints.get("order_endpoints_allowed") is False
        and endpoints.get("requires_exact_v43_capability_scan") is True,
        "proposal endpoint boundary is invalid",
    )
    selected = endpoints.get("selected_market_data_endpoints")
    _require(isinstance(selected, list) and bool(selected),
             "proposal public endpoints are missing")
    for index, endpoint in enumerate(selected):
        _require(
            isinstance(endpoint, list)
            and len(endpoint) == 2
            and all(isinstance(part, str) and part for part in endpoint),
            f"proposal public endpoint {index} is invalid",
        )
    expected_selected = [
        [host, path]
        for host, path in config.MARKET_DATA_ALLOWED_ENDPOINTS
        if host in _REST_MARKET_HOSTS[str(anchor["venue"])]
    ]
    _require(selected == expected_selected,
             "proposal public endpoints do not match the active allow-list")

    implementation = _exact_mapping(
        proposal.get("implementation_binding"),
        "implementation_binding",
        frozenset({"mode", "active_plan_file_sha256", "v43_plan_hash_assigned"}),
    )
    _require(
        implementation.get("mode")
        == "RECOMPUTE_AND_FREEZE_ALL_CODE_SHA256_AT_V43_ISSUE"
        and implementation.get("active_plan_file_sha256")
        == trust_root.PLAN_FILE_SHA256
        and implementation.get("v43_plan_hash_assigned") is False,
        "proposal implementation binding is invalid",
    )
    prohibitions = _exact_mapping(
        proposal.get("execution_prohibitions"),
        "execution_prohibitions",
        frozenset({
            "private_api", "authenticated_api", "venue_orders",
            "venue_paper_or_testnet_execution", "live_execution", "real_capital",
            "leverage_or_margin_changes",
        }),
    )
    _require(all(value is True for value in prohibitions.values()),
             "proposal execution prohibitions are incomplete")
    return proposal, anchor


def _validate_lifecycle(
    value: Mapping[str, Any],
    *,
    proposal: Mapping[str, Any],
    anchor: Mapping[str, Any],
    generated_ts: int,
) -> Mapping[str, Any]:
    snapshot = _exact_mapping(value, "lifecycle_snapshot", _LIFECYCLE_FIELDS)
    _require(snapshot.get("schema") == LIFECYCLE_SCHEMA,
             "lifecycle snapshot schema is invalid")
    _verify_canonical_hash(snapshot, "snapshot_hash")
    _require(snapshot.get("proposal_hash") == proposal.get("proposal_hash"),
             "lifecycle proposal hash mismatch")
    _require(snapshot.get("event_binding_hash") == proposal.get("event_binding_hash"),
             "lifecycle event binding hash mismatch")
    for field in ("venue", "premarket_contract_id", "spot_symbol", "official_spot_t0"):
        _require(snapshot.get(field) == anchor.get(field),
                 f"lifecycle {field.replace('_', ' ')} mismatch")
    _require(snapshot.get("tradable") is True, "lifecycle is not tradable")
    _require(snapshot.get("terminal") is False, "lifecycle is terminal")
    _require(snapshot.get("phase") in {"continuous", "spot_listing_pending"},
             "lifecycle phase is not execution-observable")
    _require(snapshot.get("transition_state") == "spot_listing_pending",
             "lifecycle transition is not spot-listing-pending")

    received_iso_ts = _utc_seconds(snapshot.get("received_at_utc"), "received_at_utc")
    received_ts = int(_number(snapshot.get("received_ts"), "received_ts", positive=True))
    exchange_ts = int(_number(snapshot.get("exchange_ts"), "exchange_ts", positive=True))
    _require(abs(received_iso_ts - received_ts) <= 1,
             "lifecycle received clocks disagree")
    age = generated_ts - received_ts
    _require(0 <= age <= MAX_LIFECYCLE_AGE_SEC, "lifecycle snapshot is stale")
    _require(0 <= received_ts - exchange_ts <= MAX_LIFECYCLE_AGE_SEC,
             "lifecycle exchange timestamp is stale or noncausal")
    _hash(snapshot.get("raw_payload_sha256"), "raw_payload_sha256")
    _hash(snapshot.get("request_identity_sha256"), "request_identity_sha256")

    ws = _exact_mapping(snapshot.get("ws_binding"), "ws_binding", _WS_FIELDS)
    _require(ws.get("scheme") == "wss", "lifecycle WebSocket scheme must be wss")
    venue = str(anchor["venue"])
    profile = _SINGLE_CONNECTION_WS_PROFILE.get(venue)
    _require(
        profile is not None,
        "venue requires a different reviewed multi-connection WebSocket schema",
    )
    _require(
        ws.get("host") == profile["host"]
        and ws.get("port") == profile["port"]
        and ws.get("path") == profile["path"],
        "WebSocket endpoint does not match the exact venue profile",
    )
    channels = ws.get("channels")
    symbol = str(anchor["premarket_contract_id"])
    expected_channels = [
        template.format(symbol=symbol)
        for template in profile["channel_templates"]
    ]
    _require(
        channels == expected_channels,
        "WebSocket channels are not the exact contract-bound venue profile",
    )

    spec = _exact_mapping(snapshot.get("contract_spec"), "contract_spec", _CONTRACT_FIELDS)
    for field in ("contract_size", "tick_size", "qty_step", "min_qty", "max_qty"):
        _decimal(spec.get(field), f"contract_spec.{field}", positive=True)
    funding_interval = _integer(
        spec.get("funding_interval_sec"), "contract_spec.funding_interval_sec", positive=True
    )
    _require(funding_interval <= 7 * 24 * 60 * 60,
             "contract_spec.funding_interval_sec is outside policy")
    maintenance = _decimal(
        spec.get("maintenance_margin_rate"),
        "contract_spec.maintenance_margin_rate",
        positive=True,
    )
    _require(maintenance < 1, "contract_spec.maintenance_margin_rate is outside policy")
    taker_fee = _number(spec.get("taker_fee_rate"), "contract_spec.taker_fee_rate")
    _require(0 <= taker_fee < 0.1, "contract_spec.taker_fee_rate is outside policy")
    _require(
        spec.get("price_limit_model")
        in {"OBSERVED_PUBLIC_PRICE_LIMIT_CHANNEL", "PUBLIC_RULE_DERIVED"},
        "contract_spec.price_limit_model is not preregistered",
    )
    _hash(spec.get("source_sha256"), "contract_spec.source_sha256")
    step = _decimal(spec["qty_step"], "contract_spec.qty_step", positive=True)
    minimum = _decimal(spec["min_qty"], "contract_spec.min_qty", positive=True)
    maximum = _decimal(spec["max_qty"], "contract_spec.max_qty", positive=True)
    _require(minimum <= maximum, "contract_spec min_qty exceeds max_qty")
    _require(
        minimum % step == 0 and maximum % step == 0,
        "contract_spec quantity bounds are not aligned to qty_step",
    )
    return snapshot


def _validate_approval(
    value: Mapping[str, Any],
    *,
    proposal: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
    generated_ts: int,
) -> Mapping[str, Any]:
    approval = _exact_mapping(value, "approval_receipt", _APPROVAL_FIELDS)
    _require(approval.get("schema") == APPROVAL_SCHEMA, "approval schema is invalid")
    _verify_canonical_hash(approval, "approval_hash")
    _require(
        _SAFE_ID.fullmatch(str(approval.get("approval_id") or "")) is not None,
        "approval_id is invalid",
    )
    _hash(approval.get("approval_nonce"), "approval_nonce")
    _require(
        approval.get("approval_mode")
        == "EXPLICIT_USER_VISIBLE_ONE_SHOT_V43_REVIEW",
        "approval mode is invalid",
    )
    _text(approval.get("approved_by"), "approved_by")
    approved_ts = _utc_seconds(approval.get("approved_at_utc"), "approved_at_utc")
    expires_ts = _utc_seconds(approval.get("expires_at_utc"), "expires_at_utc")
    _require(approved_ts <= generated_ts, "approval is from the future")
    lifecycle_received_ts = int(
        _number(lifecycle.get("received_ts"), "lifecycle.received_ts", positive=True)
    )
    _require(
        lifecycle_received_ts <= approved_ts <= generated_ts <= expires_ts,
        "approval ordering does not follow the fresh lifecycle snapshot",
    )
    _require(
        0 < expires_ts - approved_ts <= MAX_APPROVAL_TTL_SEC,
        "approval expiry exceeds the one-shot review TTL",
    )
    _require(approval.get("proposal_hash") == proposal.get("proposal_hash"),
             "approval proposal hash mismatch")
    _require(approval.get("event_binding_hash") == proposal.get("event_binding_hash"),
             "approval event binding hash mismatch")
    _require(approval.get("lifecycle_snapshot_hash") == lifecycle.get("snapshot_hash"),
             "approval lifecycle snapshot hash mismatch")
    _require(approval.get("approved_capture_attempts") == 1,
             "approval must authorize exactly one capture attempt")
    _require(approval.get("approval_consumed") is False,
             "approval has already been consumed")
    _require(approval.get("public_data_only") is True,
             "approval must remain public-data-only")
    _require(approval.get("orders_allowed") is False,
             "approval must forbid orders")
    _require(approval.get("capture_token_issued") is False,
             "approval must not issue a capture token")
    return approval


def build_event_binding_candidate(
    proposal: Mapping[str, Any],
    lifecycle_snapshot: Mapping[str, Any],
    approval_receipt: Mapping[str, Any],
    *,
    generated_at_utc: str,
) -> dict[str, Any]:
    """Bind one event for review while retaining zero runtime authority."""
    generated_ts = _utc_seconds(generated_at_utc, "generated_at_utc")
    proposal, anchor = _validate_proposal(proposal)
    proposal_generated_ts = _utc_seconds(
        proposal.get("generated_at_utc"), "proposal.generated_at_utc"
    )
    armed_ts = _utc_seconds(
        proposal["arming_receipt"].get("armed_at_utc"),
        "arming_receipt.armed_at_utc",
    )
    _require(
        armed_ts <= proposal_generated_ts <= generated_ts,
        "proposal clocks are noncausal",
    )
    bounds = _exact_mapping(proposal.get("capture_bounds"), "capture_bounds", _BOUNDS_FIELDS)
    capture_start = _integer(bounds.get("capture_start_ts"), "capture_start_ts", positive=True)
    capture_end = _integer(bounds.get("capture_end_ts"), "capture_end_ts", positive=True)
    t0 = _integer(anchor.get("official_spot_t0"), "official_spot_t0", positive=True)
    expected_bounds = {
        "official_spot_t0": t0,
        "capture_start_ts": t0 - config.CAPTURE_WINDOW_BEFORE_SEC,
        "capture_end_ts": t0 + config.CAPTURE_WINDOW_AFTER_SEC,
        "window_before_sec": config.CAPTURE_WINDOW_BEFORE_SEC,
        "window_after_sec": config.CAPTURE_WINDOW_AFTER_SEC,
        "max_runtime_sec": config.MAX_CAPTURE_RUNTIME_SEC,
        "max_requests": config.MAX_REQUESTS_PER_CAPTURE,
        "max_events": config.MAX_EVENTS_PER_CAPTURE,
        "launch_early_grace_sec": config.CAPTURE_LAUNCH_EARLY_GRACE_SEC,
        "launch_late_grace_sec": config.CAPTURE_LAUNCH_LATE_GRACE_SEC,
    }
    _require(dict(bounds) == expected_bounds,
             "capture bounds or hard ceilings drifted from the active contract")
    _require(capture_start - generated_ts >= MIN_ACTIVATION_LEAD_SEC,
             "insufficient activation lead before capture start")
    lifecycle = _validate_lifecycle(
        lifecycle_snapshot,
        proposal=proposal,
        anchor=anchor,
        generated_ts=generated_ts,
    )
    approval = _validate_approval(
        approval_receipt,
        proposal=proposal,
        lifecycle=lifecycle,
        generated_ts=generated_ts,
    )
    candidate: dict[str, Any] = {
        "schema": CANDIDATE_SCHEMA,
        "status": "STRUCTURAL_CANDIDATE_ONLY_EXTERNAL_AUTHORITY_REQUIRED",
        "generated_at_utc": generated_at_utc,
        "proposed_plan_schema": PROPOSED_PLAN_SCHEMA,
        "proposed_plan_id": PROPOSED_PLAN_ID,
        "proposal_hash": proposal["proposal_hash"],
        "event_binding_hash": proposal["event_binding_hash"],
        "arming_receipt_hash": proposal["arming_receipt"]["receipt_hash"],
        "lifecycle_snapshot_hash": lifecycle["snapshot_hash"],
        "approval_hash": approval["approval_hash"],
        "episode_id": anchor["episode_id"],
        "venue": anchor["venue"],
        "listing_venue": anchor["listing_venue"],
        "premarket_contract_id": anchor["premarket_contract_id"],
        "spot_symbol": anchor["spot_symbol"],
        "official_spot_t0": t0,
        "official_record_hash": anchor["official_record_hash"],
        "registry_sha256": anchor["registry_sha256"],
        "mutation_receipt_hash": anchor["mutation_receipt_hash"],
        "capture_start_ts": capture_start,
        "capture_end_ts": capture_end,
        "ws_binding": dict(lifecycle["ws_binding"]),
        "contract_spec": dict(lifecycle["contract_spec"]),
        "approved_by": approval["approved_by"],
        "approved_capture_attempts": 1,
        "approval_id": approval["approval_id"],
        "approval_nonce": approval["approval_nonce"],
        "approval_expires_at_utc": approval["expires_at_utc"],
        "one_writer_required": True,
        "one_writer_verified": False,
        "one_event_required": True,
        "one_event_verified": False,
        "one_attempt_requested": True,
        "one_attempt_consumed": False,
        "post_event_no_capture_plan_required": True,
        "requires_fresh_v43_registry_refresh": True,
        "requires_trust_root_rebind": True,
        "durable_proposal_head_verified": False,
        "registry_prefix_verified": False,
        "durable_approval_consumption_verified": False,
        "external_authority_verified": False,
        "issuable": False,
        "capture_authorized": False,
        "capture_token_issued": False,
        "orders_allowed": False,
        "public_data_only": True,
    }
    candidate["binding_hash"] = canonical_hash(candidate)
    return candidate


__all__ = [
    "EventBindingError",
    "build_event_binding_candidate",
    "issue_readiness",
]
