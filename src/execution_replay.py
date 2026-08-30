"""Deterministic, pure offline execution replay for sealed market evidence.

The runtime creates no exchange instruction and has no network or filesystem
capability.  It simulates independent paper exits from causally available depth.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any


EXPECTED_MODEL = {
    "side": "LONG",
    "paper_notional_usdt": 25.0,
    "leverage_equivalent": 1.0,
    "entry_offset_sec": -60,
    "exit_offsets_sec": [0, 5, 15, 60],
    "latency_ms": 200,
    "order_ttl_ms": 500,
    "stress_leverages": [2, 5],
    "entry_liquidity": "TAKER_ASKS",
    "exit_liquidity": "TAKER_BIDS",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_result_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite_number(value: object, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if positive and number <= 0:
        return None
    return number


def _missing_inputs(request: object) -> list[str]:
    if not isinstance(request, dict):
        return ["request"]
    missing: list[str] = []
    for field in (
        "event",
        "model",
        "contract_spec",
        "depth_snapshots",
        "funding_observations",
        "mark_index_observations",
    ):
        if field not in request:
            missing.append(field)
    model = request.get("model")
    if isinstance(model, dict) and "taker_fee_bps" not in model:
        missing.append("model.taker_fee_bps")
    spec = request.get("contract_spec")
    if isinstance(spec, dict):
        for field in (
            "base_per_size_unit",
            "quantity_step_size_units",
            "maintenance_margin_rate",
        ):
            if field not in spec:
                missing.append(f"contract_spec.{field}")
    return missing


def _base_report(status: str, missing: list[str] | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": "premarket_perp_execution_replay_result_v1",
        "status": status,
        "missing_inputs": list(missing or []),
        "orders_created": 0,
        "virtual_positions_created": 0,
        "live_execution": False,
        "private_api_used": False,
        "acceptance_capable": False,
        "net_pnl_usdt": None,
        "horizons": [],
    }
    report["result_hash"] = canonical_result_hash(report)
    return report


def _payload_without_envelope(request: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(request)
    payload.pop("evidence_envelope", None)
    return payload


def _validate_evidence_boundary(request: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (mode, failure status) for explicit synthetic or sealed evidence."""

    if request.get("schema") != "premarket_perp_execution_replay_request_v1":
        return None, "NOT_RUN_EXECUTION_REQUEST_SCHEMA_MISMATCH"
    envelope = request.get("evidence_envelope")
    event = request.get("event")
    legacy_synthetic = (
        envelope is None
        and isinstance(event, dict)
        and event.get("evidence_class") == "SYNTHETIC_OFFLINE_ONLY"
        and not any(
            key in request
            for key in ("sealed", "evidence_class", "capture_manifest_sha256")
        )
    )
    if legacy_synthetic:
        return "SYNTHETIC_OFFLINE_ONLY", None
    if not isinstance(envelope, dict):
        return None, "NOT_RUN_EVIDENCE_SEAL_MISSING"
    if (
        envelope.get("schema") != "premarket_perp_execution_evidence_envelope_v1"
        or envelope.get("sealed") is not True
        or envelope.get("evidence_class") != "SEALED_L2_CAPTURE"
        or request.get("sealed") is not True
        or request.get("evidence_class") != "SEALED_L2_CAPTURE"
    ):
        return None, "NOT_RUN_EVIDENCE_SEAL_INVALID"
    manifest_hash = envelope.get("capture_manifest_sha256")
    if (
        not isinstance(manifest_hash, str)
        or _SHA256_RE.fullmatch(manifest_hash) is None
        or manifest_hash != request.get("capture_manifest_sha256")
    ):
        return None, "NOT_RUN_EVIDENCE_MANIFEST_HASH_INVALID"
    actual = canonical_result_hash(_payload_without_envelope(request))
    if envelope.get("payload_sha256") != actual:
        return None, "NOT_RUN_EVIDENCE_PAYLOAD_HASH_MISMATCH"
    # A caller can always recompute both hashes above.  Until an independent loader
    # verifies the on-disk manifest, terminal receipt and capture lineage, the pure
    # engine must not promote a self-attested request to production evidence.
    return None, "NOT_RUN_TRUSTED_EVIDENCE_LOADER_REQUIRED"


def _validate_event_and_spec(
    event: object,
    spec: object,
    *,
    evidence_mode: str,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(event, dict) or not isinstance(spec, dict):
        return ["event_or_contract_spec"]
    if spec.get("schema") != "premarket_perp_contract_spec_v1":
        errors.append("contract_spec.schema")
    if event.get("venue") != spec.get("venue"):
        errors.append("event.venue")
    if event.get("contract_id") != spec.get("contract_id"):
        errors.append("event.contract_id")
    if evidence_mode == "SEALED_L2_CAPTURE":
        if event.get("t0_source_class") != "OFFICIAL_ANNOUNCEMENT":
            errors.append("event.t0_source_class")
        if event.get("t0_precision_sec") != 1:
            errors.append("event.t0_precision_sec")
        official_hash = event.get("official_record_hash")
        if not isinstance(official_hash, str) or _SHA256_RE.fullmatch(official_hash) is None:
            errors.append("event.official_record_hash")
        source_url = event.get("official_source_url")
        if not isinstance(source_url, str) or not source_url.startswith("https://"):
            errors.append("event.official_source_url")
    maintenance = _finite_number(spec.get("maintenance_margin_rate"))
    if maintenance is None or maintenance < 0 or maintenance >= 1:
        errors.append("contract_spec.maintenance_margin_rate")
    return errors


def _validate_depth_evidence(snapshots: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    identifiers: set[str] = set()
    for row in snapshots:
        if not isinstance(row, dict) or row.get("schema") != "premarket_perp_depth_snapshot_v1":
            errors.append("depth_snapshots.schema")
            continue
        snapshot_id = row.get("snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id or snapshot_id in identifiers:
            errors.append("depth_snapshots.snapshot_id")
            continue
        identifiers.add(snapshot_id)
        received = _finite_number(row.get("received_ts"))
        requested = _finite_number(row.get("request_ts"))
        if received is None or requested is None or received < requested:
            errors.append(f"depth_snapshots.{snapshot_id}.clock")
        bids = _valid_levels(row, "SELL")
        asks = _valid_levels(row, "BUY")
        raw_bids = row.get("bids")
        raw_asks = row.get("asks")
        if not isinstance(raw_bids, list) or len(bids) != len(raw_bids):
            errors.append(f"depth_snapshots.{snapshot_id}.bids")
        if not isinstance(raw_asks, list) or len(asks) != len(raw_asks):
            errors.append(f"depth_snapshots.{snapshot_id}.asks")
        if any(left[0] < right[0] for left, right in zip(bids, bids[1:])):
            errors.append(f"depth_snapshots.{snapshot_id}.bids_order")
        if any(left[0] > right[0] for left, right in zip(asks, asks[1:])):
            errors.append(f"depth_snapshots.{snapshot_id}.asks_order")
        if bids and asks and bids[0][0] >= asks[0][0]:
            errors.append(f"depth_snapshots.{snapshot_id}.crossed")
    return errors


def _validate_funding_evidence(
    rows: list[dict[str, Any]],
    *,
    strict_mark: bool,
) -> list[str]:
    errors: list[str] = []
    identifiers: set[str] = set()
    settlements: set[float] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("schema") != "premarket_perp_funding_settlement_v1":
            errors.append("funding_observations.schema")
            continue
        observation_id = row.get("observation_id")
        settlement = _finite_number(row.get("settlement_ts"))
        received = _finite_number(row.get("received_ts"))
        rate = _finite_number(row.get("rate"))
        mark = _finite_number(row.get("settlement_mark_price"), positive=True)
        if not isinstance(observation_id, str) or not observation_id or observation_id in identifiers:
            errors.append("funding_observations.observation_id")
        else:
            identifiers.add(observation_id)
        if settlement is None or settlement in settlements:
            errors.append("funding_observations.settlement_ts")
        else:
            settlements.add(settlement)
        if received is None or settlement is None or received < settlement:
            errors.append("funding_observations.received_ts")
        if rate is None:
            errors.append("funding_observations.rate")
        if strict_mark and mark is None:
            errors.append("funding_observations.settlement_mark_price")
    return errors


def _validate_fixed_model(model: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    for key, expected in EXPECTED_MODEL.items():
        if model.get(key) != expected:
            mismatches.append(f"model.{key}")
    fee = _finite_number(model.get("taker_fee_bps"))
    if fee is None or fee < 0:
        mismatches.append("model.taker_fee_bps")
    return mismatches


def _select_snapshot(
    snapshots: list[dict[str, Any]],
    target_send_ts: float,
    latency_ms: float,
    ttl_ms: float,
) -> dict[str, Any] | None:
    eligible = target_send_ts + latency_ms / 1000.0
    deadline = eligible + ttl_ms / 1000.0
    candidates: list[dict[str, Any]] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        received = _finite_number(snapshot.get("received_ts"))
        if received is None or received < eligible or received > deadline:
            continue
        if not isinstance(snapshot.get("snapshot_id"), str):
            continue
        candidates.append(snapshot)
    if not candidates:
        return None
    return min(candidates, key=lambda row: (float(row["received_ts"]), row["snapshot_id"]))


def _empty_attempt(side: str, target: float, latency_ms: float, ttl_ms: float) -> dict[str, Any]:
    eligible = target + latency_ms / 1000.0
    return {
        "status": "UNFILLED_NO_CAUSAL_DEPTH",
        "side": side,
        "target_send_ts": target,
        "eligible_received_ts": eligible,
        "deadline_received_ts": eligible + ttl_ms / 1000.0,
        "selected_snapshot_id": None,
        "arrival_bbo_price": None,
        "fills": [],
        "filled_base_qty": 0.0,
        "filled_quote_notional": 0.0,
        "vwap_price": None,
        "observed_slippage_bps": None,
    }


def _floor_step(value: float, step: float) -> float:
    if value <= 0 or step <= 0:
        return 0.0
    units = math.floor((value + step * 1e-10) / step)
    return units * step


def _valid_levels(snapshot: dict[str, Any], side: str) -> list[tuple[float, float]]:
    field = "asks" if side == "BUY" else "bids"
    raw = snapshot.get(field)
    if not isinstance(raw, list):
        return []
    levels: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, list) or len(item) < 2:
            continue
        price = _finite_number(item[0], positive=True)
        size = _finite_number(item[1], positive=True)
        if price is None or size is None:
            continue
        levels.append((price, size))
    return levels


def _sweep_entry(
    snapshot: dict[str, Any] | None,
    *,
    target: float,
    latency_ms: float,
    ttl_ms: float,
    desired_quote: float,
    base_per_unit: float,
    step_units: float,
) -> dict[str, Any]:
    if snapshot is None:
        attempt = _empty_attempt("BUY", target, latency_ms, ttl_ms)
        attempt["desired_quote_notional"] = desired_quote
        attempt["notional_utilization"] = 0.0
        return attempt
    levels = _valid_levels(snapshot, "BUY")
    attempt = _empty_attempt("BUY", target, latency_ms, ttl_ms)
    attempt["selected_snapshot_id"] = snapshot["snapshot_id"]
    attempt["arrival_bbo_price"] = levels[0][0] if levels else None
    remaining = desired_quote
    fills: list[dict[str, float]] = []
    for price, available_units in levels:
        if remaining <= 1e-12:
            break
        capacity_units = min(available_units, remaining / (price * base_per_unit))
        fill_units = _floor_step(capacity_units, step_units)
        if fill_units <= 0:
            continue
        base_qty = fill_units * base_per_unit
        quote = base_qty * price
        if quote > remaining + 1e-9:
            continue
        fills.append(
            {
                "price": price,
                "size_units": fill_units,
                "base_qty": base_qty,
                "quote_notional": quote,
            }
        )
        remaining -= quote
    filled_quote = sum(item["quote_notional"] for item in fills)
    filled_base = sum(item["base_qty"] for item in fills)
    utilization = min(1.0, filled_quote / desired_quote) if desired_quote else 0.0
    if filled_base <= 0:
        status = "UNFILLED_NO_LIQUIDITY"
    elif desired_quote - filled_quote <= max(
        1e-8,
        desired_quote * 1e-8,
        max((price for price, _ in levels), default=0.0) * base_per_unit * step_units,
    ):
        status = "FULL"
    else:
        status = "PARTIAL"
    vwap = filled_quote / filled_base if filled_base else None
    bbo = attempt["arrival_bbo_price"]
    slippage = ((vwap - bbo) / bbo * 10_000.0) if vwap is not None and bbo else None
    attempt.update(
        {
            "status": status,
            "desired_quote_notional": desired_quote,
            "fills": fills,
            "filled_base_qty": filled_base,
            "filled_quote_notional": filled_quote,
            "vwap_price": vwap,
            "observed_slippage_bps": slippage,
            "notional_utilization": utilization,
        }
    )
    return attempt


def _sweep_exit(
    snapshot: dict[str, Any] | None,
    *,
    target: float,
    latency_ms: float,
    ttl_ms: float,
    desired_base: float,
    base_per_unit: float,
    step_units: float,
) -> dict[str, Any]:
    if snapshot is None:
        attempt = _empty_attempt("SELL", target, latency_ms, ttl_ms)
        attempt["desired_base_qty"] = desired_base
        return attempt
    levels = _valid_levels(snapshot, "SELL")
    attempt = _empty_attempt("SELL", target, latency_ms, ttl_ms)
    attempt["selected_snapshot_id"] = snapshot["snapshot_id"]
    attempt["arrival_bbo_price"] = levels[0][0] if levels else None
    remaining_base = desired_base
    fills: list[dict[str, float]] = []
    for price, available_units in levels:
        if remaining_base <= 1e-12:
            break
        desired_units = remaining_base / base_per_unit
        fill_units = _floor_step(min(available_units, desired_units), step_units)
        if fill_units <= 0:
            continue
        base_qty = min(remaining_base, fill_units * base_per_unit)
        quote = base_qty * price
        fills.append(
            {
                "price": price,
                "size_units": fill_units,
                "base_qty": base_qty,
                "quote_notional": quote,
            }
        )
        remaining_base -= base_qty
    filled_base = sum(item["base_qty"] for item in fills)
    filled_quote = sum(item["quote_notional"] for item in fills)
    if filled_base <= 0:
        status = "UNFILLED_NO_LIQUIDITY"
    elif desired_base - filled_base <= max(1e-10, desired_base * 1e-8):
        status = "FULL"
    else:
        status = "PARTIAL"
    vwap = filled_quote / filled_base if filled_base else None
    bbo = attempt["arrival_bbo_price"]
    slippage = ((bbo - vwap) / bbo * 10_000.0) if vwap is not None and bbo else None
    attempt.update(
        {
            "status": status,
            "desired_base_qty": desired_base,
            "fills": fills,
            "filled_base_qty": filled_base,
            "filled_quote_notional": filled_quote,
            "vwap_price": vwap,
            "observed_slippage_bps": slippage,
        }
    )
    return attempt


def _observed_funding(
    observations: list[dict[str, Any]],
    entry_received: float,
    exit_received: float,
    position_base_qty: float,
    fallback_mark_price: float,
) -> tuple[dict[str, Any], float]:
    selected: list[dict[str, Any]] = []
    for row in observations:
        if not isinstance(row, dict):
            continue
        settlement = _finite_number(row.get("settlement_ts"))
        received = _finite_number(row.get("received_ts"))
        rate = _finite_number(row.get("rate"))
        observation_id = row.get("observation_id")
        if (
            settlement is None
            or received is None
            or rate is None
            or not isinstance(observation_id, str)
        ):
            continue
        if (
            entry_received < settlement <= exit_received
            and settlement <= received <= exit_received
        ):
            selected.append(row)
    selected.sort(key=lambda row: (float(row["settlement_ts"]), row["observation_id"]))
    funding_pnl = -sum(
        position_base_qty
        * float(row.get("settlement_mark_price") or fallback_mark_price)
        * float(row["rate"])
        for row in selected
    )
    return (
        {
            "settlement_ids": [row["observation_id"] for row in selected],
            "observed_rate_sum": sum(float(row["rate"]) for row in selected),
            "valuation": "POSITION_BASE_QTY_X_SETTLEMENT_MARK_PRICE",
            "extrapolated": False,
        },
        funding_pnl,
    )


def _liquidation_and_divergence(
    observations: list[dict[str, Any]],
    entry_received: float,
    exit_received: float,
    entry_price: float,
    maintenance_margin_rate: float,
    leverages: list[int],
) -> tuple[dict[str, Any], float | None, bool]:
    path: list[dict[str, Any]] = []
    for row in observations:
        if not isinstance(row, dict):
            continue
        received = _finite_number(row.get("received_ts"))
        mark = _finite_number(row.get("mark_price"), positive=True)
        index = _finite_number(row.get("index_price"), positive=True)
        if received is None or mark is None or index is None:
            continue
        if entry_received <= received <= exit_received:
            path.append(row)
    path.sort(key=lambda row: (float(row["received_ts"]), str(row.get("observation_id", ""))))
    max_divergence = None
    if path:
        max_divergence = max(
            abs(float(row["mark_price"]) - float(row["index_price"]))
            / float(row["index_price"])
            * 10_000.0
            for row in path
        )
    stress: dict[str, Any] = {}
    if not path:
        for leverage in leverages:
            stress[f"{leverage}x"] = {
                "liquidated": None,
                "evidence_coverage": "MISSING",
                "liquidation_threshold": None,
                "trigger_observation_id": None,
                "trigger_mark_price": None,
                "index_price_at_trigger": None,
            }
        return stress, None, False
    for leverage in leverages:
        threshold = entry_price * (1.0 - 1.0 / float(leverage) + maintenance_margin_rate)
        trigger = next((row for row in path if float(row["mark_price"]) <= threshold), None)
        stress[f"{leverage}x"] = {
            "liquidated": trigger is not None,
            "evidence_coverage": "OBSERVED_PATH",
            "liquidation_threshold": threshold,
            "trigger_observation_id": None if trigger is None else trigger.get("observation_id"),
            "trigger_mark_price": None if trigger is None else float(trigger["mark_price"]),
            "index_price_at_trigger": None if trigger is None else float(trigger["index_price"]),
        }
    return stress, max_divergence, True


def replay_fixed_long(request: dict[str, Any]) -> dict[str, Any]:
    """Replay the preregistered fixed LONG model over in-memory evidence."""

    request_copy = copy.deepcopy(request)
    if not isinstance(request_copy, dict):
        return _base_report("NOT_RUN_EXECUTION_REQUEST_INVALID", ["request"])
    evidence_mode, boundary_error = _validate_evidence_boundary(request_copy)
    if boundary_error is not None:
        return _base_report(boundary_error)
    missing = _missing_inputs(request_copy)
    if missing:
        return _base_report("NOT_RUN_MANDATORY_INPUT_MISSING", sorted(missing))
    model = request_copy["model"]
    spec = request_copy["contract_spec"]
    if not isinstance(model, dict) or not isinstance(spec, dict):
        return _base_report("NOT_RUN_MANDATORY_INPUT_MISSING", ["model_or_contract_spec"])
    identity_errors = _validate_event_and_spec(
        request_copy.get("event"),
        spec,
        evidence_mode=str(evidence_mode),
    )
    if identity_errors:
        return _base_report("NOT_RUN_EVENT_OR_CONTRACT_IDENTITY_INVALID", sorted(identity_errors))
    mismatches = _validate_fixed_model(model)
    if mismatches:
        return _base_report("NOT_RUN_FIXED_MODEL_MISMATCH", sorted(mismatches))

    base_per_unit = _finite_number(spec.get("base_per_size_unit"), positive=True)
    step_units = _finite_number(spec.get("quantity_step_size_units"), positive=True)
    maintenance = _finite_number(spec.get("maintenance_margin_rate"))
    t0 = _finite_number(request_copy["event"].get("official_spot_t0"), positive=True)
    if base_per_unit is None or step_units is None or maintenance is None or t0 is None:
        return _base_report("NOT_RUN_INVALID_MANDATORY_INPUT", ["numeric_contract_or_t0"])
    snapshots = request_copy["depth_snapshots"]
    funding_rows = request_copy["funding_observations"]
    mark_rows = request_copy["mark_index_observations"]
    if not all(isinstance(value, list) for value in (snapshots, funding_rows, mark_rows)):
        return _base_report("NOT_RUN_INVALID_MANDATORY_INPUT", ["evidence_lists"])
    depth_errors = _validate_depth_evidence(snapshots)
    if depth_errors:
        return _base_report("NOT_RUN_INVALID_DEPTH_EVIDENCE", sorted(set(depth_errors)))
    funding_errors = _validate_funding_evidence(
        funding_rows,
        strict_mark=evidence_mode == "SEALED_L2_CAPTURE",
    )
    if funding_errors:
        return _base_report("NOT_RUN_INVALID_FUNDING_EVIDENCE", sorted(set(funding_errors)))

    latency_ms = float(model["latency_ms"])
    ttl_ms = float(model["order_ttl_ms"])
    entry_target = t0 + float(model["entry_offset_sec"])
    entry_snapshot = _select_snapshot(snapshots, entry_target, latency_ms, ttl_ms)
    entry = _sweep_entry(
        entry_snapshot,
        target=entry_target,
        latency_ms=latency_ms,
        ttl_ms=ttl_ms,
        desired_quote=float(model["paper_notional_usdt"]),
        base_per_unit=base_per_unit,
        step_units=step_units,
    )
    if entry["filled_base_qty"] <= 0:
        report = _base_report("NO_POSITION_ENTRY_UNFILLED")
        report.update(
            {
                "event": copy.deepcopy(request_copy["event"]),
                "model": copy.deepcopy(model),
                "contract_spec": copy.deepcopy(spec),
                "entry": entry,
                "horizons": [
                    {"offset_sec": offset, "net_pnl_usdt": None}
                    for offset in model["exit_offsets_sec"]
                ],
            }
        )
        report["result_hash"] = canonical_result_hash(
            {key: value for key, value in report.items() if key != "result_hash"}
        )
        return report

    entry_received = float(entry_snapshot["received_ts"])
    entry_fee = entry["filled_quote_notional"] * float(model["taker_fee_bps"]) / 10_000.0
    horizons: list[dict[str, Any]] = []
    for offset in model["exit_offsets_sec"]:
        exit_target = t0 + float(offset)
        exit_snapshot = _select_snapshot(snapshots, exit_target, latency_ms, ttl_ms)
        exit_attempt = _sweep_exit(
            exit_snapshot,
            target=exit_target,
            latency_ms=latency_ms,
            ttl_ms=ttl_ms,
            desired_base=entry["filled_base_qty"],
            base_per_unit=base_per_unit,
            step_units=step_units,
        )
        residual = max(0.0, entry["filled_base_qty"] - exit_attempt["filled_base_qty"])
        fully_closed = residual <= max(1e-10, entry["filled_base_qty"] * 1e-8)
        exit_fee = exit_attempt["filled_quote_notional"] * float(model["taker_fee_bps"]) / 10_000.0
        gross = (
            exit_attempt["filled_quote_notional"] - entry["filled_quote_notional"]
            if fully_closed
            else None
        )
        exit_received = (
            float(exit_snapshot["received_ts"])
            if exit_snapshot is not None
            else exit_target + latency_ms / 1000.0 + ttl_ms / 1000.0
        )
        funding_summary, funding_pnl = _observed_funding(
            funding_rows,
            entry_received,
            exit_received,
            entry["filled_base_qty"],
            float(entry["vwap_price"]),
        )
        net = (
            gross - entry_fee - exit_fee + funding_pnl
            if gross is not None
            else None
        )
        stress, max_divergence, liquidation_evidence_observed = _liquidation_and_divergence(
            mark_rows,
            entry_received,
            exit_received,
            float(entry["vwap_price"]),
            maintenance,
            list(model["stress_leverages"]),
        )
        horizons.append(
            {
                "offset_sec": int(offset),
                "exit": exit_attempt,
                "position_fully_closed": fully_closed,
                "residual_base_qty": residual,
                "gross_pnl_usdt": gross,
                "fees_usdt": {
                    "entry": entry_fee,
                    "exit": exit_fee,
                    "total": entry_fee + exit_fee,
                },
                "funding": funding_summary,
                "funding_pnl_usdt": funding_pnl,
                "net_pnl_usdt": net,
                "liquidation_stress": stress,
                "liquidation_model_missing": not liquidation_evidence_observed,
                "max_abs_mark_index_divergence_bps": max_divergence,
            }
        )

    report = {
        "schema": "premarket_perp_execution_replay_result_v1",
        "status": "COMPLETE",
        "missing_inputs": [],
        "event": copy.deepcopy(request_copy["event"]),
        "model": copy.deepcopy(model),
        "contract_spec": copy.deepcopy(spec),
        "entry": entry,
        "horizons": horizons,
        "orders_created": 0,
        "virtual_positions_created": 1,
        "live_execution": False,
        "private_api_used": False,
        "evidence_mode": evidence_mode,
        "acceptance_capable": False,
        "net_pnl_usdt": None,
    }
    report["result_hash"] = canonical_result_hash(report)
    return report
