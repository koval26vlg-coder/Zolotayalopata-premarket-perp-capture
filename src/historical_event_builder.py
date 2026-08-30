"""Pure post-hoc historical candle normalisation.

This module deliberately has no network or filesystem capability.  Historical
OHLCV can describe a price path, but it cannot prove BBO availability, queue
position, a fill, slippage, or net execution economics.
"""

from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any


SUPPORTED_VENUES = frozenset({"bybit", "okx", "gate"})


class HistoricalEventValidationError(ValueError):
    """Raised when a seed or a venue response is not safe to materialise."""


def _canonical_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HistoricalEventValidationError("payload is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _decimal(value: object, field: str, *, allow_zero: bool = True) -> Decimal:
    if isinstance(value, bool):
        raise HistoricalEventValidationError(f"{field} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise HistoricalEventValidationError(f"{field} must be numeric") from exc
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise HistoricalEventValidationError(f"invalid {field}")
    return parsed


def _timestamp_seconds(value: object, *, milliseconds: bool) -> int:
    if isinstance(value, bool):
        raise HistoricalEventValidationError("timestamp must be an integer")
    try:
        raw = int(str(value))
    except (TypeError, ValueError) as exc:
        raise HistoricalEventValidationError("timestamp must be an integer") from exc
    if raw <= 0:
        raise HistoricalEventValidationError("timestamp must be positive")
    if milliseconds:
        if raw % 1000:
            raise HistoricalEventValidationError("millisecond timestamp is not second-aligned")
        raw //= 1000
    return raw


def _normalised_row(
    *,
    open_ts: int,
    open_price: object,
    high_price: object,
    low_price: object,
    close_price: object,
    base_volume: object,
    quote_volume: object | None,
    closed: bool,
) -> dict[str, object]:
    open_value = _decimal(open_price, "open", allow_zero=False)
    high_value = _decimal(high_price, "high", allow_zero=False)
    low_value = _decimal(low_price, "low", allow_zero=False)
    close_value = _decimal(close_price, "close", allow_zero=False)
    _decimal(base_volume, "base_volume")
    if quote_volume is not None:
        _decimal(quote_volume, "quote_volume")
    if high_value < max(open_value, close_value) or low_value > min(open_value, close_value):
        raise HistoricalEventValidationError("OHLC envelope is inconsistent")
    if high_value < low_value:
        raise HistoricalEventValidationError("high is below low")
    if not closed:
        raise HistoricalEventValidationError("unclosed candle is not historical evidence")
    return {
        "open_ts": open_ts,
        "open": str(open_price),
        "high": str(high_price),
        "low": str(low_price),
        "close": str(close_price),
        "base_volume": str(base_volume),
        "quote_volume": None if quote_volume is None else str(quote_volume),
        "closed": True,
    }


def _strictly_ordered(rows: list[dict[str, object]], venue: str) -> list[dict[str, object]]:
    if not rows:
        raise HistoricalEventValidationError("empty candle response")
    stamps = [int(row["open_ts"]) for row in rows]
    if len(stamps) != len(set(stamps)):
        raise HistoricalEventValidationError("duplicate candle timestamp")
    ascending = all(left < right for left, right in zip(stamps, stamps[1:]))
    descending = all(left > right for left, right in zip(stamps, stamps[1:]))
    expected_descending = venue in {"bybit", "okx"}
    if expected_descending and not descending:
        raise HistoricalEventValidationError(f"{venue} candles are not newest-first")
    if not expected_descending and not ascending:
        raise HistoricalEventValidationError(f"{venue} candles are not oldest-first")
    return list(reversed(rows)) if descending else rows


def parse_venue_candles(venue: str, payload: object) -> list[dict[str, object]]:
    """Validate one public venue response and return canonical oldest-first OHLCV."""

    venue = str(venue).lower()
    if venue not in SUPPORTED_VENUES:
        raise HistoricalEventValidationError(f"unsupported venue: {venue}")

    rows: Any
    normalised: list[dict[str, object]] = []
    if venue == "bybit":
        if not isinstance(payload, dict) or payload.get("retCode") != 0:
            raise HistoricalEventValidationError("Bybit returned an error envelope")
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("list"), list):
            raise HistoricalEventValidationError("Bybit candle list is missing")
        rows = result["list"]
        for row in rows:
            if not isinstance(row, list) or len(row) < 7:
                raise HistoricalEventValidationError("invalid Bybit candle row")
            normalised.append(
                _normalised_row(
                    open_ts=_timestamp_seconds(row[0], milliseconds=True),
                    open_price=row[1],
                    high_price=row[2],
                    low_price=row[3],
                    close_price=row[4],
                    base_volume=row[5],
                    quote_volume=row[6],
                    closed=True,
                )
            )
    elif venue == "okx":
        if not isinstance(payload, dict) or payload.get("code") != "0":
            raise HistoricalEventValidationError("OKX returned an error envelope")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise HistoricalEventValidationError("OKX candle list is missing")
        for row in rows:
            if not isinstance(row, list) or len(row) < 9:
                raise HistoricalEventValidationError("invalid OKX candle row")
            normalised.append(
                _normalised_row(
                    open_ts=_timestamp_seconds(row[0], milliseconds=True),
                    open_price=row[1],
                    high_price=row[2],
                    low_price=row[3],
                    close_price=row[4],
                    base_volume=row[5],
                    quote_volume=row[7],
                    closed=str(row[8]) == "1",
                )
            )
    else:
        archive = (
            isinstance(payload, dict)
            and payload.get("archive_schema") == "gate_futures_candlesticks_1m_v1"
            and isinstance(payload.get("rows"), list)
        )
        if archive:
            rows = payload["rows"]
        elif isinstance(payload, list):
            rows = payload
        else:
            raise HistoricalEventValidationError("Gate returned an error envelope")
        for row in rows:
            required_columns = 6 if archive else 7
            if not isinstance(row, list) or len(row) < required_columns:
                raise HistoricalEventValidationError("invalid Gate candle row")
            normalised.append(
                _normalised_row(
                    open_ts=_timestamp_seconds(row[0], milliseconds=False),
                    open_price=row[5],
                    high_price=row[3],
                    low_price=row[4],
                    close_price=row[2],
                    base_volume=row[1] if archive else row[6],
                    quote_volume=None if archive else row[1],
                    closed=True,
                )
            )
    return _strictly_ordered(normalised, venue)


def _required_seed_value(seed: dict[str, object], field: str) -> object:
    if field not in seed or seed[field] in (None, ""):
        raise HistoricalEventValidationError(f"seed is missing {field}")
    return seed[field]


def build_historical_event(
    seed: dict[str, object],
    venue_payload: object,
    retrieved_at_ts: int,
) -> dict[str, object]:
    """Build deterministic descriptive-only evidence from an injected fixture."""

    if not isinstance(seed, dict):
        raise HistoricalEventValidationError("seed must be an object")
    seed_copy = copy.deepcopy(seed)
    payload_copy = copy.deepcopy(venue_payload)
    venue = str(_required_seed_value(seed_copy, "venue")).lower()
    if venue not in SUPPORTED_VENUES:
        raise HistoricalEventValidationError(f"unsupported venue: {venue}")
    if str(seed_copy.get("listing_venue", venue)).lower() != venue:
        raise HistoricalEventValidationError("listing venue does not match contract venue")
    if seed_copy.get("asset_class") != "CRYPTO_TOKEN":
        raise HistoricalEventValidationError("historical crypto builder requires CRYPTO_TOKEN")
    history_source_class = seed_copy.get("history_source_class")
    if history_source_class not in {
        "VENUE_PUBLIC_REST_POSTHOC_OHLCV",
        "VENUE_PUBLIC_ARCHIVE_POSTHOC_OHLCV",
    }:
        raise HistoricalEventValidationError("unsupported history source class")
    if (
        history_source_class == "VENUE_PUBLIC_ARCHIVE_POSTHOC_OHLCV"
        and venue != "gate"
    ):
        raise HistoricalEventValidationError("archive source is only registered for Gate")
    official_t0 = _required_seed_value(seed_copy, "official_spot_t0")
    if isinstance(official_t0, bool) or not isinstance(official_t0, int) or official_t0 <= 0:
        raise HistoricalEventValidationError("official_spot_t0 must be positive integer seconds")
    if isinstance(retrieved_at_ts, bool) or not isinstance(retrieved_at_ts, int):
        raise HistoricalEventValidationError("retrieved_at_ts must be integer seconds")
    if retrieved_at_ts < official_t0:
        raise HistoricalEventValidationError("historical retrieval predates official t0")

    candles = parse_venue_candles(venue, payload_copy)
    result: dict[str, object] = {
        "schema": "premarket_perp_historical_event_v1",
        "event_id": str(_required_seed_value(seed_copy, "event_id")),
        "venue": venue,
        "listing_venue": venue,
        "premarket_contract_id": str(
            _required_seed_value(seed_copy, "premarket_contract_id")
        ),
        "spot_symbol": str(_required_seed_value(seed_copy, "spot_symbol")),
        "asset_class": "CRYPTO_TOKEN",
        "premarket_contract_launch_ts": seed_copy.get("premarket_contract_launch_ts"),
        "official_spot_t0": official_t0,
        "first_trade_ts": seed_copy.get("first_trade_ts"),
        "transition_ts": seed_copy.get("transition_ts"),
        "t0_source_class": str(_required_seed_value(seed_copy, "t0_source_class")),
        "t0_precision_sec": seed_copy.get("t0_precision_sec"),
        "official_source_url": str(
            _required_seed_value(seed_copy, "official_source_url")
        ),
        "official_record_hash": str(
            _required_seed_value(seed_copy, "official_record_hash")
        ),
        "history_source_class": str(history_source_class),
        "history_source_url": str(
            _required_seed_value(seed_copy, "history_source_url")
        ),
        "history_request_params": copy.deepcopy(
            _required_seed_value(seed_copy, "history_request_params")
        ),
        "retrieved_at_ts": retrieved_at_ts,
        "posthoc_retrieval": True,
        "evidence_use": "DESCRIPTIVE_ONLY",
        "price_evidence_class": "POSTHOC_OHLCV_NOT_EXECUTION_EVIDENCE",
        "acceptance_capable": False,
        "capture_eligible": False,
        "execution_evidence": False,
        "orders_allowed": False,
        "candle_count": len(candles),
        "candles": candles,
        "raw_payload_sha256": _canonical_sha256(payload_copy),
    }
    result["manifest_sha256"] = _canonical_sha256(result)
    return result
