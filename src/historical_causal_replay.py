"""Pure historical markout and sealed-evidence delegation boundary."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any, Callable


ENTRY_OFFSET_SEC = -60
EXIT_OFFSETS_SEC = (0, 5, 15, 60)
EXPECTED_GRANULARITY_SEC = 60


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _with_hash(report: dict[str, Any]) -> dict[str, Any]:
    report["result_hash"] = _canonical_sha256(report)
    return report


def _finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _observed_granularity(
    candles: list[dict[str, Any]],
    request_params: object,
) -> int | None:
    if isinstance(request_params, dict) and request_params.get("interval") == "1m":
        return 60
    stamps = sorted(
        int(row["open_ts"])
        for row in candles
        if isinstance(row, dict)
        and isinstance(row.get("open_ts"), int)
        and not isinstance(row.get("open_ts"), bool)
    )
    gaps = [right - left for left, right in zip(stamps, stamps[1:]) if right > left]
    return min(gaps) if gaps else None


def _descriptive_failure(
    status: str,
    *,
    requested: int,
    observed: int | None,
) -> dict[str, Any]:
    return _with_hash(
        {
            "schema": "premarket_perp_historical_markout_v1",
            "status": status,
            "evidence_use": "DESCRIPTIVE_ONLY",
            "price_evidence_class": (
                "POSTHOC_OHLCV_FIXED_CANDLE_MARKOUT_NOT_EXECUTION_EVIDENCE"
            ),
            "acceptance_capable": False,
            "execution_evidence": False,
            "requested_candle_granularity_sec": requested,
            "observed_candle_granularity_sec": observed,
            "markouts": [],
        }
    )


def replay_historical_ohlcv(
    historical_event: dict[str, object],
    candle_granularity_sec: int,
) -> dict[str, Any]:
    """Calculate preregistered post-hoc candle-bucket markouts only."""

    event = copy.deepcopy(historical_event)
    if not isinstance(event, dict):
        return _descriptive_failure(
            "NOT_RUN_INVALID_HISTORICAL_EVENT",
            requested=candle_granularity_sec,
            observed=None,
        )
    unhashed = dict(event)
    claimed_manifest = unhashed.pop("manifest_sha256", None)
    if claimed_manifest != _canonical_sha256(unhashed):
        return _descriptive_failure(
            "NOT_RUN_HISTORICAL_EVENT_HASH_MISMATCH",
            requested=candle_granularity_sec,
            observed=None,
        )
    if (
        event.get("schema") != "premarket_perp_historical_event_v1"
        or event.get("evidence_use") != "DESCRIPTIVE_ONLY"
        or event.get("execution_evidence") is not False
        or event.get("acceptance_capable") is not False
    ):
        return _descriptive_failure(
            "NOT_RUN_HISTORICAL_EVENT_CLASS_MISMATCH",
            requested=candle_granularity_sec,
            observed=None,
        )
    candles = event.get("candles")
    if not isinstance(candles, list):
        return _descriptive_failure(
            "NOT_RUN_HISTORICAL_CANDLES_MISSING",
            requested=candle_granularity_sec,
            observed=None,
        )
    observed = _observed_granularity(candles, event.get("history_request_params"))
    if candle_granularity_sec != EXPECTED_GRANULARITY_SEC or observed != EXPECTED_GRANULARITY_SEC:
        return _descriptive_failure(
            "NOT_RUN_CANDLE_GRANULARITY_MISMATCH",
            requested=candle_granularity_sec,
            observed=observed,
        )
    t0 = event.get("official_spot_t0")
    if isinstance(t0, bool) or not isinstance(t0, int):
        return _descriptive_failure(
            "NOT_RUN_OFFICIAL_T0_MISSING",
            requested=candle_granularity_sec,
            observed=observed,
        )
    by_open: dict[int, dict[str, Any]] = {}
    for row in candles:
        if not isinstance(row, dict) or row.get("closed") is not True:
            continue
        stamp = row.get("open_ts")
        price = _finite(row.get("open"))
        if isinstance(stamp, int) and not isinstance(stamp, bool) and price is not None:
            by_open[stamp] = row

    def observation(offset: int) -> dict[str, Any]:
        target = t0 + offset
        bucket = target - (target % candle_granularity_sec)
        row = by_open.get(bucket)
        if row is None:
            return {
                "target_offset_sec": offset,
                "target_ts": target,
                "status": "TARGET_CANDLE_BUCKET_MISSING",
                "observed_candle_open_ts": None,
                "observed_price": None,
                "quantisation_sec": target - bucket,
                "exact_target_boundary": target == bucket,
            }
        return {
            "target_offset_sec": offset,
            "target_ts": target,
            "status": "OBSERVED_CANDLE_BUCKET_OPEN",
            "observed_candle_open_ts": bucket,
            "observed_price": float(row["open"]),
            "quantisation_sec": target - bucket,
            "exact_target_boundary": target == bucket,
        }

    entry = observation(ENTRY_OFFSET_SEC)
    markouts: list[dict[str, Any]] = []
    entry_price = entry["observed_price"]
    for offset in EXIT_OFFSETS_SEC:
        item = observation(offset)
        item["candle_markout_return"] = (
            None
            if entry_price is None or item["observed_price"] is None
            else item["observed_price"] / entry_price - 1.0
        )
        markouts.append(item)
    return _with_hash(
        {
            "schema": "premarket_perp_historical_markout_v1",
            "status": "DESCRIPTIVE_ONLY_COMPLETE",
            "event_id": event.get("event_id"),
            "venue": event.get("venue"),
            "official_spot_t0": t0,
            "evidence_use": "DESCRIPTIVE_ONLY",
            "price_evidence_class": (
                "POSTHOC_OHLCV_FIXED_CANDLE_MARKOUT_NOT_EXECUTION_EVIDENCE"
            ),
            "acceptance_capable": False,
            "execution_evidence": False,
            "candle_granularity_sec": candle_granularity_sec,
            "sampling_rule": "TARGET_BUCKET_OPEN",
            "entry": entry,
            "markouts": markouts,
        }
    )


def _delegation_failure(status: str, actual_hash: str) -> dict[str, Any]:
    return _with_hash(
        {
            "schema": "premarket_perp_historical_execution_delegation_v1",
            "status": status,
            "execution_input_sha256": actual_hash,
            "evidence_use": "EXECUTION_MODEL_SENSITIVITY_ONLY",
            "acceptance_capable": False,
            "orders_created": 0,
            "net_pnl_usdt": None,
        }
    )


def delegate_sealed_l2_execution(
    *,
    sealed_request: dict[str, object],
    expected_input_sha256: str,
    execution_delegate: Callable[[dict[str, object]], dict[str, object]],
) -> dict[str, Any]:
    """Verify an exact sealed L2 request before calling an injected pure engine."""

    request_copy = copy.deepcopy(sealed_request)
    actual_hash = _canonical_sha256(request_copy)
    if actual_hash != expected_input_sha256:
        return _delegation_failure("NOT_RUN_EXECUTION_INPUT_HASH_MISMATCH", actual_hash)
    if (
        request_copy.get("sealed") is not True
        or request_copy.get("evidence_class") != "SEALED_L2_CAPTURE"
    ):
        return _delegation_failure("NOT_RUN_EXECUTION_INPUT_NOT_SEALED_L2", actual_hash)
    try:
        delegated = execution_delegate(copy.deepcopy(request_copy))
    except Exception:
        return _delegation_failure("NOT_RUN_EXECUTION_DELEGATE_FAILED", actual_hash)
    if not isinstance(delegated, dict):
        return _delegation_failure("NOT_RUN_EXECUTION_DELEGATE_INVALID", actual_hash)
    execution_report = copy.deepcopy(delegated)
    execution_report["acceptance_capable"] = False
    report = {
        "schema": "premarket_perp_historical_execution_delegation_v1",
        "status": "DELEGATED_SEALED_L2_EXECUTION",
        "execution_input_sha256": actual_hash,
        "delegated_result_hash": delegated.get("result_hash"),
        "evidence_use": "EXECUTION_MODEL_SENSITIVITY_ONLY",
        "acceptance_capable": False,
        "orders_created": 0,
        "net_pnl_usdt": execution_report.get("net_pnl_usdt"),
        "execution_report": execution_report,
    }
    return _with_hash(report)
