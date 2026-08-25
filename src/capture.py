"""One bounded, visible capture around a single listing t0.

What this is, stated plainly because it decides whether the replay means anything:
**a poll, not a tape.** REST endpoints answer at the instants we ask. Between two
samples the market did things we did not see, and for a hypothesis about +5, +15 and
+60 seconds that gap is the whole question. So the cadence is declared up front, the
achieved cadence is measured, and the gaps are published in the manifest. A capture
that called itself continuous while sampling twice a second would be the same kind of
claim the spot monitor's audit spent its length taking apart.

Everything the audit taught is carried over rather than rediscovered: rows are fsynced
before the manifest that authenticates them is written, the manifest carries
output_sha256, the deadline and the stop request are checked inside the loop rather
than between events, nothing is truncated silently, and the run ends with a committed
evidence receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import event_registry as registry
import project_config as config
import public_http
import risk_gate
from canonical_hash import canonical_hash
from global_market_writer_claim import (
    claim_global_market_writer,
    release_global_market_writer,
)


CAPTURE_SCHEMA = "premarket_perp_capture_v1"
SAMPLE_SCHEMA = "premarket_perp_capture_sample_v1"
PROBES = ("trades", "orderbook", "ticker")
LINEAGE_FIELDS = config.CAPTURE_LINEAGE_FIELDS
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class CaptureError(RuntimeError):
    pass


# --------------------------------------------------------------------- venue probes


@dataclass(frozen=True)
class Probe:
    venue: str
    probe: str
    url: str
    params_for: Callable[[str], dict[str, Any]]


class SyntheticFixtureTransport:
    """Static JSON fixtures for the public offline-only entrypoint.

    The exact-type check in :func:`run_capture` is intentional.  A generic callback
    could call ``public_http.get_json`` and turn the synthetic helper into an
    ungated market-data collector.  This transport serializes all fixture payloads at
    construction and only returns JSON copies; live capture uses the private core
    from inside ``capture_event`` after its authority checks.
    """

    __slots__ = ("_payload_json_by_probe",)

    def __init__(self, payload_by_probe: Mapping[str, Any]) -> None:
        if not isinstance(payload_by_probe, Mapping):
            raise CaptureError("synthetic fixture payloads must be a mapping")
        keys = {str(key) for key in payload_by_probe}
        unknown = sorted(keys - set(PROBES))
        if unknown:
            raise CaptureError(
                "synthetic fixture transport has unknown probes: " + ", ".join(unknown)
            )
        try:
            serialized = {
                str(probe): json.dumps(payload, ensure_ascii=False, allow_nan=False)
                for probe, payload in payload_by_probe.items()
            }
        except (TypeError, ValueError) as exc:
            raise CaptureError(f"synthetic fixture payload is not JSON: {exc}") from exc
        self._payload_json_by_probe = serialized

    def __call__(self, probe: Probe, _symbol: str, _timeout_sec: int) -> Any:
        try:
            payload_json = self._payload_json_by_probe[probe.probe]
        except KeyError as exc:
            raise CaptureError(
                f"synthetic fixture has no payload for probe {probe.probe!r}"
            ) from exc
        return json.loads(payload_json)


def _bybit_symbol(symbol: str) -> str:
    return symbol.upper()


PROBE_TABLE: tuple[Probe, ...] = (
    Probe("bybit", "trades", "https://api.bybit.com/v5/market/recent-trade",
          lambda s: {"category": "linear", "symbol": _bybit_symbol(s), "limit": 200}),
    Probe("bybit", "orderbook", "https://api.bybit.com/v5/market/orderbook",
          lambda s: {"category": "linear", "symbol": _bybit_symbol(s),
                     "limit": config.ORDERBOOK_DEPTH}),
    Probe("bybit", "ticker", "https://api.bybit.com/v5/market/tickers",
          lambda s: {"category": "linear", "symbol": _bybit_symbol(s)}),
    Probe("okx", "trades", "https://www.okx.com/api/v5/market/trades",
          lambda s: {"instId": s, "limit": 100}),
    Probe("okx", "orderbook", "https://www.okx.com/api/v5/market/books",
          lambda s: {"instId": s, "sz": config.ORDERBOOK_DEPTH}),
    # Measured, not assumed: /market/tickers ignores instId and returns every SWAP
    # instrument, so the singular endpoint is the one that samples this instrument.
    Probe("okx", "ticker", "https://www.okx.com/api/v5/market/ticker",
          lambda s: {"instId": s}),
    Probe("gate", "trades", "https://api.gateio.ws/api/v4/futures/usdt/trades",
          lambda s: {"contract": s, "limit": 100}),
    Probe("gate", "orderbook", "https://api.gateio.ws/api/v4/futures/usdt/order_book",
          lambda s: {"contract": s, "limit": config.ORDERBOOK_DEPTH}),
    Probe("gate", "ticker", "https://api.gateio.ws/api/v4/futures/usdt/tickers",
          lambda s: {"contract": s}),
)


def probes_for(venue: str) -> tuple[Probe, ...]:
    return tuple(probe for probe in PROBE_TABLE if probe.venue == venue)


def required_replay_probes_for(venue: str) -> tuple[str, ...]:
    required = tuple(config.REPLAY_REQUIRED_PROBES_BY_VENUE.get(venue, ()))
    available = {probe.probe for probe in probes_for(venue)}
    if not required or len(required) != len(set(required)) or not set(required) <= available:
        raise CaptureError(f"invalid replay-required probe policy for venue: {venue}")
    return required


def request_identity_for(probe: Probe, symbol: str) -> dict[str, Any]:
    """Return the exact request binding written beside a sampled response."""
    params = probe.params_for(symbol)
    material = {
        "venue": probe.venue,
        "probe": probe.probe,
        "symbol": symbol,
        "url": probe.url,
        "params": params,
    }
    return {
        "request_url": probe.url,
        "request_params": params,
        "request_identity_sha256": canonical_hash(material),
    }


def cadence_for(probe: str, offset_sec: float) -> float:
    """Tighten sampling near t0, which is where the budget is worth spending."""
    if abs(offset_sec) <= config.BURST_HALF_WIDTH_SEC:
        return config.BURST_CADENCE_SEC[probe]
    return config.PROBE_CADENCE_SEC[probe]


# ------------------------------------------------------------------- t0 confirmation

# What the venue's own metadata says about this instrument, read immediately before
# the loop. It is NOT the capture's t0 and cannot become it: an official_spot_t0 and a
# venue-declared contract launch are different source classes, and comparing them as
# though one confirmed the other is precisely the class-mixing this registry forbids.
#
# It is recorded because the venues are demonstrably unreliable here. Measured on
# 2026-08-22: OKX returned listTime 2026-09-09 for JP225-USDT-SWAP when asked for
# every SWAP instrument and listTime 2026-08-07 for the same instrument at the same
# moment when asked with instId - a 33-day spread from one endpoint. A capture that
# runs while the venue metadata is self-contradictory should say so in its manifest.
@dataclass(frozen=True)
class T0Source:
    label: str
    url: str
    params_for: Callable[[str], dict[str, Any]]
    rows_path: tuple[str, ...]
    symbol_field: str
    t0_field: str
    unit: str


T0_SOURCES: dict[str, tuple[T0Source, ...]] = {
    "bybit": (
        T0Source("instruments-info/by-symbol",
                 "https://api.bybit.com/v5/market/instruments-info",
                 lambda s: {"category": "linear", "symbol": s},
                 ("result", "list"), "symbol", "launchTime", "ms"),
    ),
    "okx": (
        T0Source("instruments/bulk", "https://www.okx.com/api/v5/public/instruments",
                 lambda s: {"instType": "SWAP"},
                 ("data",), "instId", "listTime", "ms"),
        T0Source("instruments/by-instId", "https://www.okx.com/api/v5/public/instruments",
                 lambda s: {"instType": "SWAP", "instId": s},
                 ("data",), "instId", "listTime", "ms"),
    ),
    "gate": (
        T0Source("contracts/bulk", "https://api.gateio.ws/api/v4/futures/usdt/contracts",
                 lambda s: {}, (), "name", "create_time", "s"),
    ),
}

T0_DISAGREEMENT_TOLERANCE_SEC = 60


def _dig(payload: Any, path: Sequence[str]) -> Any:
    for key in path:
        if not isinstance(payload, Mapping):
            return None
        payload = payload.get(key)
    return payload


def observe_venue_metadata(
    job: CaptureJob,
    *,
    fetch: Callable[[str, Mapping[str, Any]], Any],
    boundary_check: Callable[[], str | None] | None = None,
    tolerance_sec: int = T0_DISAGREEMENT_TOLERANCE_SEC,
) -> dict[str, Any]:
    """Read the venue's declared instrument times, in every query shape known to differ.

    Descriptive only. The findings ride in the manifest; they never move the capture's
    t0, which comes from the official-announcement class alone."""
    observations: list[dict[str, Any]] = []
    requests_made = 0
    successful_requests = 0
    for source in T0_SOURCES.get(job.venue, ()):
        if boundary_check is not None:
            reason = boundary_check()
            if reason is not None:
                raise CaptureError(f"venue metadata boundary reached: {reason}")
        record: dict[str, Any] = {"query": source.label, "t0_ts": None}
        requests_made += 1
        try:
            payload = fetch(source.url, source.params_for(job.symbol))
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"{type(exc).__name__}: {exc}"
            observations.append(record)
            if boundary_check is not None:
                reason = boundary_check()
                if reason is not None:
                    raise CaptureError(f"venue metadata boundary reached: {reason}")
            continue
        if boundary_check is not None:
            reason = boundary_check()
            if reason is not None:
                raise CaptureError(f"venue metadata boundary reached: {reason}")
        try:
            rows = _dig(payload, source.rows_path)
            match = next(
                (row for row in (rows or []) if row.get(source.symbol_field) == job.symbol),
                None,
            )
            if match is None:
                record["error"] = "instrument not present in this response"
            else:
                raw = int(match[source.t0_field])
                record["t0_ts"] = raw // 1000 if source.unit == "ms" else raw
                successful_requests += 1
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"{type(exc).__name__}: {exc}"
        observations.append(record)

    seen = [obs["t0_ts"] for obs in observations if obs["t0_ts"] is not None]
    spread = (max(seen) - min(seen)) if len(seen) > 1 else 0
    drift = min((abs(value - job.t0_ts) for value in seen), default=None)
    tolerance = max(tolerance_sec, job.t0_precision_sec)

    notes: list[str] = []
    if not seen:
        notes.append("the venue did not report an instrument time at all")
    if spread > tolerance:
        notes.append(
            f"the venue reports instrument times {spread}s apart across query shapes"
        )
    if drift is not None and drift > tolerance:
        notes.append(
            f"the venue's declared instrument time is {drift}s from the official t0 "
            "this capture is aimed at; a different source class, not a correction"
        )
    return {
        "role": "descriptive_only",
        "capture_t0_ts": job.t0_ts,
        "capture_t0_source_class": job.t0_source_class,
        "observations": observations,
        "venue_spread_sec": spread,
        "distance_from_capture_t0_sec": drift,
        "tolerance_sec": tolerance,
        "venue_metadata_is_self_consistent": not notes,
        "requests_made": requests_made,
        # The production metadata fetch has max_retries=0, so each logical request
        # is exactly one transport attempt.  Injected offline fetches inherit the
        # same accounting contract.
        "transport_attempts": requests_made,
        "successful_requests": successful_requests,
        "notes": notes,
    }


# --------------------------------------------------------------------------- capture


@dataclass
class CaptureJob:
    capture_id: str
    venue: str
    symbol: str
    t0_ts: int
    t0_source_class: str
    t0_precision_sec: int
    caveats: list[str] = field(default_factory=list)
    lineage: dict[str, Any] = field(default_factory=dict)


def job_from_event(event: Mapping[str, Any], *, capture_id: str) -> CaptureJob:
    return CaptureJob(
        capture_id=capture_id,
        venue=str(event["venue"]),
        symbol=str(event["symbol"]),
        # The official spot t0 is the only timestamp a capture may aim at.
        t0_ts=int(event["official_spot_t0"]),
        t0_source_class=str(event["t0_source_class"]),
        t0_precision_sec=int(event.get("t0_precision_sec") or 0),
        caveats=list(event.get("caveats") or []),
        lineage={key: event.get(key) for key in LINEAGE_FIELDS},
    )


def capture_evidence_classification(
    replay_readiness: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify causal input quality without claiming fill or strategy acceptance."""
    return {
        "evidence_class": (
            "CAUSAL_REPLAY_INPUT_READY"
            if replay_readiness.get("ready") is True
            else "DESCRIPTIVE_ONLY"
        ),
        "acceptance_capable": False,
    }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _require_safe_component(value: Any, label: str) -> str:
    component = str(value or "")
    if not SAFE_RUN_ID.fullmatch(component):
        raise CaptureError(f"{label} must be a safe single path component")
    return component


def _validate_capture_lineage(
    job: CaptureJob,
    *,
    event_id: str,
    plan: Mapping[str, Any],
) -> None:
    lineage = job.lineage
    if lineage.get("episode_id") != event_id:
        raise CaptureError("capture event_id does not match registry episode lineage")
    if lineage.get("venue") != job.venue:
        raise CaptureError("capture venue does not match registry episode lineage")
    if not str(lineage.get("listing_venue") or "").strip():
        raise CaptureError("capture lineage listing_venue is missing")
    if lineage.get("plan_id") != plan.get("plan_id"):
        raise CaptureError("capture registry lineage plan_id is stale")
    if lineage.get("plan_hash") != plan.get("plan_hash"):
        raise CaptureError("capture registry lineage plan_hash is stale")
    if lineage.get("premarket_contract_id") != job.symbol:
        raise CaptureError("capture contract identity does not match the market symbol")
    if not str(lineage.get("spot_symbol") or "").strip():
        raise CaptureError("capture lineage spot_symbol is missing")
    if lineage.get("official_spot_t0") != job.t0_ts:
        raise CaptureError("capture lineage official_spot_t0 does not match the job")
    if lineage.get("t0_source_class") != job.t0_source_class:
        raise CaptureError("capture lineage t0 source class does not match the job")
    lineage_precision = lineage.get("t0_precision_sec")
    if (
        isinstance(lineage_precision, bool)
        or not isinstance(lineage_precision, int)
        or lineage_precision <= 0
        or lineage_precision != job.t0_precision_sec
    ):
        raise CaptureError("capture lineage t0_precision_sec does not match the job")
    if lineage.get("asset_class") != registry.ASSET_CLASS_CRYPTO_TOKEN:
        raise CaptureError("capture lineage is not an explicit crypto asset")
    for field in ("issuer_namespace", "issuer_id"):
        if not str(lineage.get(field) or "").strip():
            raise CaptureError(f"capture registry lineage {field} is missing")
    if not re.fullmatch(
        r"[0-9a-f]{64}", str(lineage.get("asset_identity_hash") or "")
    ):
        raise CaptureError("capture registry lineage asset_identity_hash is invalid")
    for field in (
        "official_record_hash",
        "registry_sha256",
        "registry_tail_record_hash",
        "mutation_receipt_hash",
        "summary_content_sha256",
        "registry_authority_state_hash",
        "plan_hash",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(lineage.get(field) or "")):
            raise CaptureError(f"capture registry lineage {field} is missing or invalid")
    receipt_seq = lineage.get("mutation_receipt_seq")
    if (
        isinstance(receipt_seq, bool)
        or not isinstance(receipt_seq, int)
        or receipt_seq < 0
    ):
        raise CaptureError(
            "capture registry lineage mutation_receipt_seq is missing or invalid"
        )
    if not str(lineage.get("official_source_url") or "").startswith("https://"):
        raise CaptureError("capture official source URL is missing or not HTTPS")
    if not str(lineage.get("official_source_identity") or "").strip():
        raise CaptureError("capture official source identity is missing")


def _write_run_record(
    *,
    run_id: str,
    status: str,
    event_id: str,
    capture_dir: Path,
    detail: str | None = None,
    manifest: Mapping[str, Any] | None = None,
) -> None:
    record: dict[str, Any] = {
        "schema": "premarket_perp_capture_run_v2",
        "run_id": run_id,
        "event_id": event_id,
        "status": status,
        "worker_pid": os.getpid(),
        "capture_dir": str(capture_dir.resolve(strict=False)),
        "updated_at_utc": utc_now_iso(),
    }
    if status == "RUNNING":
        record["started_at_utc"] = record["updated_at_utc"]
    else:
        record["finished_at_utc"] = record["updated_at_utc"]
    if detail:
        record["detail"] = detail
    if manifest is not None:
        record.update({
            "stop_reason": manifest.get("stop_reason"),
            "rows_written": manifest.get("rows_written"),
            "requests_made": manifest.get("requests_made"),
            "replay_ready": bool(
                (manifest.get("replay_readiness") or {}).get("ready")
            ),
        })
    _write_json_atomic(config.RUN_RECORD_PATH, record)


def _require_number(value: Any, label: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CaptureError(f"{label} is missing or non-numeric") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = "positive " if positive else "finite "
        raise CaptureError(f"{label} must be a {qualifier}number")
    return number


def _exchange_timestamp(value: Any, label: str) -> float:
    timestamp = _require_number(value, label, positive=True)
    # All three venues expose epoch seconds or milliseconds on these endpoints.
    if timestamp >= 10_000_000_000:
        timestamp /= 1000.0
    if timestamp < 100_000_000:
        raise CaptureError(f"{label} is not a plausible exchange timestamp")
    return timestamp


def _require_fresh_exchange_timestamp(
    exchange_ts: float,
    *,
    probe: str,
    received_ts: float | None,
) -> None:
    if received_ts is None:
        return
    received = _require_number(received_ts, "received_ts", positive=True)
    age = received - exchange_ts
    try:
        max_staleness = float(config.MAX_SAMPLE_STALENESS_SEC[probe])
    except (KeyError, TypeError, ValueError) as exc:
        raise CaptureError(f"no staleness policy for probe {probe!r}") from exc
    if age > max_staleness:
        raise CaptureError(
            f"exchange timestamp is stale by {age:.3f}s; "
            f"maximum for {probe} is {max_staleness:g}s"
        )
    max_future_skew = float(config.MAX_EXCHANGE_FUTURE_SKEW_SEC)
    if age < -max_future_skew:
        raise CaptureError(
            f"exchange timestamp is {-age:.3f}s in the future; "
            f"maximum skew is {max_future_skew:g}s"
        )


def _require_symbol(actual: Any, expected: str, label: str) -> None:
    if str(actual or "").upper() != str(expected or "").upper():
        raise CaptureError(
            f"{label} instrument/symbol mismatch: expected {expected!r}, got {actual!r}"
        )


def _require_levels(levels: Any, label: str) -> float:
    if not isinstance(levels, list) or not levels:
        raise CaptureError(f"{label} has no depth")
    first_price: float | None = None
    for index, level in enumerate(levels):
        if isinstance(level, Mapping):
            price_raw = level.get("p")
            size_raw = level.get("s")
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price_raw = level[0]
            size_raw = level[1]
        else:
            raise CaptureError(f"{label}[{index}] is malformed")
        price = _require_number(price_raw, f"{label}[{index}].price", positive=True)
        _require_number(size_raw, f"{label}[{index}].size", positive=True)
        if first_price is None:
            first_price = price
    assert first_price is not None
    return first_price


def _require_bbo(bid: Any, ask: Any, *, label: str) -> None:
    bid_value = _require_number(bid, f"{label}.bid", positive=True)
    ask_value = _require_number(ask, f"{label}.ask", positive=True)
    if bid_value >= ask_value:
        raise CaptureError(f"{label} BBO is crossed or locked")


def _require_depth(bids: Any, asks: Any, *, label: str) -> None:
    best_bid = _require_levels(bids, f"{label}.bids")
    best_ask = _require_levels(asks, f"{label}.asks")
    if best_bid >= best_ask:
        raise CaptureError(f"{label} depth is crossed or locked")


def _require_exact_request_identity(
    venue: str,
    probe_name: str,
    symbol: str,
    *,
    request_url: Any,
    request_params: Any,
    request_identity_sha256: Any,
) -> None:
    """Bind a payload that does not echo its instrument to the sealed request."""
    candidates = tuple(
        item for item in probes_for(venue) if item.probe == probe_name
    )
    if len(candidates) != 1:
        raise CaptureError(f"{venue} {probe_name} request identity is unavailable")
    probe = candidates[0]
    expected_keys = set(probe.params_for(symbol))
    if venue == "okx":
        symbol_parameter = "instId"
    elif venue == "gate":
        symbol_parameter = "contract"
    else:
        raise CaptureError(f"{venue} {probe_name} request identity is unsupported")
    material = {
        "venue": venue,
        "probe": probe_name,
        "symbol": symbol,
        "url": request_url,
        "params": dict(request_params) if isinstance(request_params, Mapping) else None,
    }
    if (
        request_url != probe.url
        or not isinstance(request_params, Mapping)
        or set(request_params) != expected_keys
        or str(request_params.get(symbol_parameter) or "").upper()
        != str(symbol or "").upper()
        or request_identity_sha256 != canonical_hash(material)
    ):
        raise CaptureError(
            f"{venue} {probe_name} response has no echoed instrument and no exact "
            "bound request identity"
        )


def validate_probe_payload(
    venue: str,
    probe: str,
    payload: Any,
    symbol: str,
    *,
    received_ts: float | None = None,
    request_url: Any = None,
    request_params: Any = None,
    request_identity_sha256: Any = None,
) -> float:
    """Validate instrument identity, market structure and exchange freshness.

    The returned timestamp is the venue timestamp that made this sample eligible for
    replay.  A REST response that merely has HTTP success is not market evidence.
    """
    if venue == "bybit":
        ret_code = payload.get("retCode") if isinstance(payload, Mapping) else None
        exact_success = bool(
            (type(ret_code) is int and ret_code == 0)
            or (type(ret_code) is str and ret_code == "0")
        )
        if not exact_success:
            raise CaptureError("bybit payload has no successful retCode")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise CaptureError("bybit payload has no result object")
        if probe == "trades":
            rows = result.get("list")
            if not isinstance(rows, list) or not rows:
                raise CaptureError("bybit trades payload is empty")
            if not all(isinstance(row, Mapping) for row in rows):
                raise CaptureError("bybit trades rows are malformed")
            timestamps: list[float] = []
            for row in rows:
                _require_symbol(row.get("symbol"), symbol, "bybit trades")
                if not str(row.get("execId") or "").strip():
                    raise CaptureError("bybit trade has no execId")
                _require_number(row.get("price"), "bybit trade price", positive=True)
                _require_number(row.get("size"), "bybit trade size", positive=True)
                timestamps.append(_exchange_timestamp(row.get("time"), "bybit trade time"))
            exchange_ts = max(timestamps)
        elif probe == "orderbook":
            _require_symbol(result.get("s"), symbol, "bybit orderbook")
            _require_depth(result.get("b"), result.get("a"), label="bybit orderbook")
            exchange_ts = _exchange_timestamp(result.get("ts"), "bybit orderbook ts")
        elif probe == "ticker":
            rows = result.get("list")
            if not isinstance(rows, list) or not rows or not all(
                isinstance(row, Mapping) for row in rows
            ):
                raise CaptureError("bybit ticker payload is empty or malformed")
            matching = [row for row in rows if str(row.get("symbol") or "").upper() == symbol.upper()]
            if len(matching) != 1:
                raise CaptureError("bybit ticker has no unique matching instrument")
            row = matching[0]
            _require_bbo(row.get("bid1Price"), row.get("ask1Price"), label="bybit ticker")
            _require_number(row.get("markPrice"), "bybit ticker markPrice", positive=True)
            _require_number(row.get("indexPrice"), "bybit ticker indexPrice", positive=True)
            exchange_ts = _exchange_timestamp(payload.get("time"), "bybit ticker time")
        else:
            raise CaptureError(f"unsupported bybit probe: {probe}")
        _require_fresh_exchange_timestamp(
            exchange_ts, probe=probe, received_ts=received_ts
        )
        return exchange_ts
    if venue == "okx":
        if not isinstance(payload, Mapping) or str(payload.get("code")) != "0":
            raise CaptureError("okx payload has no successful code")
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows or not all(
            isinstance(row, Mapping) for row in rows
        ):
            raise CaptureError(f"okx {probe} payload is empty or malformed")
        if probe == "trades":
            timestamps: list[float] = []
            for row in rows:
                _require_symbol(row.get("instId"), symbol, "okx trades")
                if not str(row.get("tradeId") or "").strip():
                    raise CaptureError("okx trade has no tradeId")
                _require_number(row.get("px"), "okx trade price", positive=True)
                _require_number(row.get("sz"), "okx trade size", positive=True)
                timestamps.append(_exchange_timestamp(row.get("ts"), "okx trade ts"))
            exchange_ts = max(timestamps)
        elif probe == "orderbook":
            if len(rows) != 1:
                raise CaptureError("okx orderbook payload is not instrument-specific")
            row = rows[0]
            # The documented REST books response does not echo instId.  In that
            # shape, the exact on-wire request is the instrument authority.
            if row.get("instId") is not None:
                _require_symbol(row.get("instId"), symbol, "okx orderbook")
            else:
                _require_exact_request_identity(
                    "okx",
                    "orderbook",
                    symbol,
                    request_url=request_url,
                    request_params=request_params,
                    request_identity_sha256=request_identity_sha256,
                )
            _require_depth(row.get("bids"), row.get("asks"), label="okx orderbook")
            exchange_ts = _exchange_timestamp(row.get("ts"), "okx orderbook ts")
        elif probe == "ticker":
            if len(rows) != 1:
                raise CaptureError("okx ticker payload is not instrument-specific")
            row = rows[0]
            _require_symbol(row.get("instId"), symbol, "okx ticker")
            _require_bbo(row.get("bidPx"), row.get("askPx"), label="okx ticker")
            _require_number(row.get("last"), "okx ticker last", positive=True)
            exchange_ts = _exchange_timestamp(row.get("ts"), "okx ticker ts")
        else:
            raise CaptureError(f"unsupported okx probe: {probe}")
        _require_fresh_exchange_timestamp(
            exchange_ts, probe=probe, received_ts=received_ts
        )
        return exchange_ts
    if venue == "gate":
        if probe == "orderbook":
            if not isinstance(payload, Mapping):
                raise CaptureError("gate orderbook payload is malformed")
            if payload.get("contract") is not None:
                _require_symbol(payload.get("contract"), symbol, "gate orderbook")
            else:
                _require_exact_request_identity(
                    "gate",
                    "orderbook",
                    symbol,
                    request_url=request_url,
                    request_params=request_params,
                    request_identity_sha256=request_identity_sha256,
                )
            _require_depth(payload.get("bids"), payload.get("asks"), label="gate orderbook")
            exchange_ts = _exchange_timestamp(
                payload.get("current") or payload.get("update"), "gate orderbook time"
            )
        else:
            if not isinstance(payload, list) or not payload or not all(
                isinstance(row, Mapping) for row in payload
            ):
                raise CaptureError(f"gate {probe} payload is empty or malformed")
            if probe == "trades":
                timestamps: list[float] = []
                for row in payload:
                    _require_symbol(row.get("contract"), symbol, "gate trades")
                    if not str(row.get("id") or "").strip():
                        raise CaptureError("gate trade has no id")
                    _require_number(row.get("price"), "gate trade price", positive=True)
                    _require_number(row.get("size"), "gate trade size")
                    if float(row.get("size")) == 0:
                        raise CaptureError("gate trade size cannot be zero")
                    timestamps.append(_exchange_timestamp(
                        row.get("create_time_ms") or row.get("create_time"),
                        "gate trade time",
                    ))
                exchange_ts = max(timestamps)
            elif probe == "ticker":
                matching = [
                    row for row in payload
                    if str(row.get("contract") or "").upper() == symbol.upper()
                ]
                if len(matching) != 1:
                    raise CaptureError("gate ticker has no unique matching instrument")
                row = matching[0]
                _require_bbo(
                    row.get("highest_bid"), row.get("lowest_ask"), label="gate ticker"
                )
                _require_number(row.get("mark_price"), "gate ticker mark_price", positive=True)
                _require_number(row.get("index_price"), "gate ticker index_price", positive=True)
                exchange_ts = _exchange_timestamp(
                    row.get("timestamp") or row.get("time_ms") or row.get("time"),
                    "gate ticker time",
                )
            else:
                raise CaptureError(f"unsupported gate probe: {probe}")
        _require_fresh_exchange_timestamp(
            exchange_ts, probe=probe, received_ts=received_ts
        )
        return exchange_ts
    raise CaptureError(f"unsupported venue payload: {venue}")


def _run_capture_core(
    job: CaptureJob,
    *,
    capture_dir: Path,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    fetch: Callable[[Probe, str, int], Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
    timeout_sec: int = 10,
    max_requests: int | None = None,
    max_runtime_sec: int | None = None,
    venue_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect samples and return an unclassified manifest draft.

    Every knob a test needs - the clock, sleeping, fetching, the stop signal - is
    injected, so the loop's behaviour under a spent budget, an expired deadline or a
    stop request is provable without a network or a wait."""
    if fetch is None:
        raise CaptureError(
            "run_capture requires an explicitly authorized fetch; use capture_event "
            "as the live entrypoint"
        )
    if not SAFE_RUN_ID.fullmatch(str(job.capture_id)):
        raise CaptureError("capture_id must be a safe single path component")
    should_stop = should_stop or config.STOP_REQUEST_PATH.is_file
    max_requests = config.MAX_REQUESTS_PER_CAPTURE if max_requests is None else max_requests
    max_runtime_sec = (
        config.MAX_CAPTURE_RUNTIME_SEC if max_runtime_sec is None else max_runtime_sec
    )
    if not 0 < int(max_requests) <= config.MAX_REQUESTS_PER_CAPTURE:
        raise CaptureError("max_requests must be positive and cannot exceed PlanOnly")
    try:
        runtime_limit_sec = float(max_runtime_sec)
    except (TypeError, ValueError) as exc:
        raise CaptureError("max_runtime_sec must be numeric") from exc
    if (
        not math.isfinite(runtime_limit_sec)
        or runtime_limit_sec <= 0
        or runtime_limit_sec > config.MAX_CAPTURE_RUNTIME_SEC
    ):
        raise CaptureError("max_runtime_sec must be positive and cannot exceed PlanOnly")
    max_runtime_sec = runtime_limit_sec

    metadata_attempts = int(
        (venue_metadata or {}).get("transport_attempts")
        or (venue_metadata or {}).get("requests_made")
        or 0
    )
    if metadata_attempts < 0 or metadata_attempts >= int(max_requests):
        raise CaptureError(
            "venue metadata transport attempts leave no PlanOnly request budget"
        )
    poll_request_budget = int(max_requests) - metadata_attempts

    probes = probes_for(job.venue)
    if not probes:
        raise CaptureError(f"no probes defined for venue: {job.venue}")

    window_start = job.t0_ts - config.CAPTURE_WINDOW_BEFORE_SEC
    window_end = job.t0_ts + config.CAPTURE_WINDOW_AFTER_SEC
    deadline = monotonic() + max_runtime_sec

    capture_dir = capture_dir.resolve(strict=False)
    capture_dir.mkdir(parents=True, exist_ok=True)
    samples_path = capture_dir / "samples.jsonl"
    manifest_path = capture_dir / "manifest.json"
    if manifest_path.exists():
        raise CaptureError(
            f"manifest evidence already exists; exclusive capture refused: {manifest_path}"
        )
    started_at_utc = utc_now_iso()

    next_due: dict[str, float] = {probe.probe: window_start for probe in probes}
    per_probe_times: dict[str, list[float]] = {probe.probe: [] for probe in probes}
    sampled_records: list[dict[str, Any]] = []
    per_probe_errors: dict[str, int] = {probe.probe: 0 for probe in probes}
    poll_requests_made = 0
    rows_written = 0
    successful_payloads = 0
    stop_reason = "window_complete"

    def boundary_reason(*, include_request_budget: bool) -> str | None:
        if clock() >= window_end:
            return "window_complete"
        if monotonic() > deadline:
            return "max_runtime_sec_exceeded"
        if should_stop():
            return "stop_requested"
        if include_request_budget and poll_requests_made >= poll_request_budget:
            return "max_requests_exceeded"
        return None

    try:
        handle = samples_path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise CaptureError(
            f"samples evidence already exists; exclusive capture refused: {samples_path}"
        ) from exc
    with handle:
        while True:
            now = clock()
            outer_boundary = boundary_reason(include_request_budget=True)
            if outer_boundary is not None:
                stop_reason = outer_boundary
                break

            due = [probe for probe in probes if now >= next_due[probe.probe]]
            if not due:
                soonest = min(next_due[probe.probe] for probe in probes)
                # Never sleep past the end of the window or past a stop check.
                sleep_fn(max(0.0, min(soonest - now, 0.25)))
                continue

            terminate_capture = False
            for probe in due:
                pre_request_boundary = boundary_reason(include_request_budget=True)
                if pre_request_boundary is not None:
                    stop_reason = pre_request_boundary
                    terminate_capture = True
                    break
                request_ts = clock()
                offset = request_ts - job.t0_ts
                request_identity = request_identity_for(probe, job.symbol)
                bound_params = dict(request_identity["request_params"])
                bound_probe = Probe(
                    venue=probe.venue,
                    probe=probe.probe,
                    url=str(request_identity["request_url"]),
                    params_for=lambda _symbol, params=bound_params: dict(params),
                )
                record: dict[str, Any] = {
                    "schema": SAMPLE_SCHEMA,
                    "capture_id": job.capture_id,
                    "venue": job.venue,
                    "symbol": job.symbol,
                    "probe": probe.probe,
                    "t0_ts": job.t0_ts,
                    "request_ts": round(request_ts, 3),
                    "offset_sec": round(offset, 3),
                    **request_identity,
                }
                post_request_boundary: str | None = None
                try:
                    payload = fetch(bound_probe, job.symbol, timeout_sec)
                    received_ts = clock()
                    record["payload"] = payload
                    record["received_ts"] = round(received_ts, 3)
                    record["latency_ms"] = round((received_ts - request_ts) * 1000, 1)
                    post_request_boundary = boundary_reason(include_request_budget=False)
                    if post_request_boundary is not None:
                        record["error"] = (
                            "response_not_accepted_after_capture_boundary: "
                            f"{post_request_boundary}"
                        )
                        per_probe_errors[probe.probe] += 1
                    else:
                        exchange_ts = validate_probe_payload(
                            job.venue,
                            probe.probe,
                            payload,
                            job.symbol,
                            received_ts=received_ts,
                            request_url=record["request_url"],
                            request_params=record["request_params"],
                            request_identity_sha256=record["request_identity_sha256"],
                        )
                        record["exchange_ts"] = round(exchange_ts, 3)
                        record["exchange_age_sec"] = round(received_ts - exchange_ts, 3)
                        per_probe_times[probe.probe].append(received_ts)
                        successful_payloads += 1
                except Exception as exc:  # noqa: BLE001
                    received_ts = clock()
                    record["received_ts"] = round(received_ts, 3)
                    record["latency_ms"] = round((received_ts - request_ts) * 1000, 1)
                    # Before t0 an instrument may not exist yet, and a venue may rate
                    # limit us. Both are observations about the capture, not reasons to
                    # abandon it - but they are never silently dropped either.
                    record["error"] = f"{type(exc).__name__}: {exc}"
                    per_probe_errors[probe.probe] += 1
                    post_request_boundary = boundary_reason(include_request_budget=False)
                poll_requests_made += 1
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                rows_written += 1
                sampled_records.append(record)
                next_due[probe.probe] = request_ts + cadence_for(probe.probe, offset)
                if post_request_boundary is not None:
                    stop_reason = post_request_boundary
                    terminate_capture = True
                    break
            if terminate_capture:
                break
        # The samples are the only durable evidence: on the platter before the manifest
        # that vouches for them exists.
        handle.flush()
        os.fsync(handle.fileno())

    readiness = replay_readiness(
        sampled_records,
        t0_ts=job.t0_ts,
        t0_precision_sec=job.t0_precision_sec,
        required_probes=required_replay_probes_for(job.venue),
    )
    status = "COMPLETED" if stop_reason == "window_complete" else "STOPPED_INCOMPLETE"
    if successful_payloads == 0:
        status = "STOPPED_INCOMPLETE"
        if stop_reason == "window_complete":
            stop_reason = "no_successful_payloads"
    total_transport_attempts = metadata_attempts + poll_requests_made
    manifest = {
        "schema": CAPTURE_SCHEMA,
        "capture_id": job.capture_id,
        # Two different facts, deliberately not collapsed into one word. The loop can
        # run to the end of its window and still produce data that cannot answer the
        # question - a capture launched four minutes before t0 covers half the burst.
        # Reporting only "COMPLETED" would describe the process and be read as a
        # verdict on the data.
        "status": status,
        "stop_reason": stop_reason,
        "replay_readiness": readiness,
        "venue": job.venue,
        "symbol": job.symbol,
        "t0_ts": job.t0_ts,
        "t0_source_class": job.t0_source_class,
        "t0_precision_sec": job.t0_precision_sec,
        "t0_caveats": job.caveats,
        "lineage": dict(job.lineage),
        "venue_metadata_observed": dict(venue_metadata) if venue_metadata else None,
        "window": {"start_ts": window_start, "end_ts": window_end,
                   "before_sec": config.CAPTURE_WINDOW_BEFORE_SEC,
                   "after_sec": config.CAPTURE_WINDOW_AFTER_SEC},
        "started_at_utc": started_at_utc,
        "finished_at_utc": utc_now_iso(),
        "requests_made": total_transport_attempts,
        "transport_attempts": total_transport_attempts,
        "poll_requests_made": poll_requests_made,
        "metadata_requests_made": metadata_attempts,
        "request_attempts": {
            "metadata": metadata_attempts,
            "market_data_poll": poll_requests_made,
            "total_transport": total_transport_attempts,
            "plan_max": int(max_requests),
        },
        "max_requests": max_requests,
        "rows_written": rows_written,
        "successful_payloads": successful_payloads,
        "errors_by_probe": per_probe_errors,
        "output_sha256": _sha256_file(samples_path),
        # The honest part: what the sampling actually achieved, not what was intended.
        "sampling": sampling_report(per_probe_times, t0_ts=job.t0_ts),
        "sampling_clock": "received_ts",
        "sampling_method": (
            "REST polling: each sample is the venue's answer at our request instant, "
            "not a continuous tape; gaps between samples are unobserved"
        ),
    }
    return manifest


def run_capture(
    job: CaptureJob,
    *,
    capture_dir: Path,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    fetch: SyntheticFixtureTransport | None = None,
    should_stop: Callable[[], bool] | None = None,
    timeout_sec: int = 10,
    max_requests: int | None = None,
    max_runtime_sec: int | None = None,
    venue_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run static JSON fixtures; never perform live I/O or acceptance capture."""
    resolved_dir = capture_dir.resolve(strict=False)
    plan_root = config.CAPTURE_ROOT.resolve(strict=False)
    try:
        resolved_dir.relative_to(plan_root)
    except ValueError:
        pass
    else:
        raise CaptureError(
            "public run_capture cannot write inside the PlanOnly-bound capture root"
        )
    if type(fetch) is not SyntheticFixtureTransport:
        raise CaptureError(
            "public run_capture requires the exact static SyntheticFixtureTransport; "
            "arbitrary fetch callables are forbidden"
        )
    manifest = _run_capture_core(
        job,
        capture_dir=resolved_dir,
        clock=clock,
        monotonic=monotonic,
        sleep_fn=sleep_fn,
        fetch=fetch,
        should_stop=should_stop,
        timeout_sec=timeout_sec,
        max_requests=max_requests,
        max_runtime_sec=max_runtime_sec,
        venue_metadata=venue_metadata,
    )
    readiness = manifest["replay_readiness"]
    readiness["structural_ready"] = bool(readiness.get("ready"))
    readiness["ready"] = False
    notes = list(readiness.get("notes") or [])
    notes.append(
        "public run_capture is synthetic/offline-only and cannot support acceptance"
    )
    readiness["notes"] = notes
    manifest["evidence_class"] = "SYNTHETIC_OFFLINE_ONLY"
    manifest["acceptance_capable"] = False
    manifest_path = resolved_dir / "manifest.json"
    try:
        _write_json_exclusive(manifest_path, manifest)
    except FileExistsError as exc:
        raise CaptureError(
            f"manifest evidence already exists; exclusive capture refused: {manifest_path}"
        ) from exc
    return manifest


def replay_readiness(
    sampled: Sequence[Any],
    *,
    t0_ts: int,
    t0_precision_sec: int = 1,
    required_probes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Require successful, causal and sufficiently dense evidence for every probe."""
    # Compatibility for descriptive callers that only have timestamps.  Production
    # capture always passes full records and required_probes, so payload/error evidence
    # cannot disappear behind this projection.
    if required_probes is None:
        times = sorted(float(value) for value in sampled)
        notes: list[str] = []
        if not times:
            return {
                "ready": False,
                "notes": ["no samples were collected at all"],
                "covered_before_t0_sec": None,
                "covered_after_t0_sec": None,
                "covers_full_burst_window": False,
            }
        before = round(t0_ts - times[0], 3)
        after = round(times[-1] - t0_ts, 3)
        half = config.BURST_HALF_WIDTH_SEC
        covers_burst = before >= half and after >= half
        if before <= 0:
            notes.append("capture began at or after t0: there is no pre-listing baseline")
        elif before < half:
            notes.append(f"only {before}s of pre-t0 coverage, less than the {half}s burst window")
        if after < half:
            notes.append(f"only {after}s of post-t0 coverage, less than the {half}s burst window")
        return {
            "ready": covers_burst and not notes,
            "covered_before_t0_sec": before,
            "covered_after_t0_sec": after,
            "covers_full_burst_window": covers_burst,
            "notes": notes,
        }

    required = tuple(dict.fromkeys(str(probe) for probe in required_probes))
    if not required:
        return {
            "ready": False,
            "successful_samples": 0,
            "successful_probes": [],
            "required_probes": [],
            "invalid_samples": len(sampled),
            "noncausal_samples": 0,
            "covered_before_t0_sec": None,
            "covered_after_t0_sec": None,
            "covers_full_burst_window": False,
            "max_burst_gap_sec_by_probe": {},
            "required_entry_lead_sec": config.PRIMARY_ENTRY_LEAD_SEC,
            "entry_available": False,
            "required_exit_offsets_sec": list(config.PRIMARY_EXIT_OFFSETS_SEC),
            "available_exit_offsets_sec": [],
            "t0_precision_sec": int(t0_precision_sec),
            "notes": ["required probe set is empty; readiness fails closed"],
        }

    notes: list[str] = []
    unknown_probes = sorted(set(required) - set(PROBES))
    if unknown_probes:
        notes.append("unknown required probes: " + ", ".join(unknown_probes))
    valid_by_probe: dict[str, list[Mapping[str, Any]]] = {
        probe: [] for probe in required
    }
    noncausal = 0
    invalid = 0
    validation_errors: dict[str, int] = {}
    for raw in sampled:
        if not isinstance(raw, Mapping):
            invalid += 1
            continue
        probe = str(raw.get("probe") or "")
        if probe not in valid_by_probe:
            continue
        if raw.get("error") or "payload" not in raw:
            invalid += 1
            continue
        try:
            request_ts = float(raw["request_ts"])
            received_ts = float(raw["received_ts"])
            if received_ts < request_ts:
                noncausal += 1
                continue
            validate_probe_payload(
                str(raw.get("venue") or ""),
                probe,
                raw["payload"],
                str(raw.get("symbol") or ""),
                received_ts=received_ts,
                request_url=raw.get("request_url"),
                request_params=raw.get("request_params"),
                request_identity_sha256=raw.get("request_identity_sha256"),
            )
        except (KeyError, TypeError, ValueError, CaptureError) as exc:
            invalid += 1
            message = str(exc) or type(exc).__name__
            validation_errors[message] = validation_errors.get(message, 0) + 1
            continue
        valid_by_probe[probe].append(raw)

    if noncausal:
        notes.append(f"{noncausal} successful-looking samples have noncausal received_ts")
    for message, count in sorted(validation_errors.items()):
        notes.append(f"{count} invalid samples: {message}")
    successful_probes = sorted(
        probe for probe, records in valid_by_probe.items() if records
    )
    missing_probes = sorted(set(required) - set(successful_probes))
    if missing_probes:
        notes.append("no successful payloads for probes: " + ", ".join(missing_probes))

    half = float(config.BURST_HALF_WIDTH_SEC)
    covers_full_burst = True
    covered_before_values: list[float] = []
    covered_after_values: list[float] = []
    gap_by_probe: dict[str, float | None] = {}
    for probe in required:
        records = valid_by_probe[probe]
        received_times = sorted(float(record["received_ts"]) for record in records)
        if not received_times:
            covers_full_burst = False
            gap_by_probe[probe] = None
            continue
        before = t0_ts - received_times[0]
        after = received_times[-1] - t0_ts
        covered_before_values.append(before)
        covered_after_values.append(after)
        coverage_tolerance = float(cadence_for(probe, half))
        if before + coverage_tolerance < half or after + coverage_tolerance < half:
            covers_full_burst = False
            if before + coverage_tolerance < half:
                notes.append(
                    f"{probe} has only {before:.3f}s of pre-t0 coverage; requires {half:g}s"
                )
            if after + coverage_tolerance < half:
                notes.append(
                    f"{probe} has only {after:.3f}s of post-t0 coverage; requires {half:g}s"
                )
            notes.append(
                f"{probe} does not cover the full {half:g}s burst on both sides of t0"
            )
        burst_received = sorted(
            float(record["received_ts"])
            for record in records
            if abs(float(record["received_ts"]) - t0_ts) <= half
        )
        gaps = [later - earlier for earlier, later in zip(burst_received, burst_received[1:])]
        max_gap = max(gaps) if gaps else None
        gap_by_probe[probe] = round(max_gap, 3) if max_gap is not None else None
        allowed_gap = (
            float(config.BURST_CADENCE_SEC[probe])
            * float(config.MAX_BURST_GAP_CADENCE_MULTIPLIER)
        )
        if max_gap is None or max_gap > allowed_gap + 1e-9:
            notes.append(
                f"{probe} burst gap {max_gap} exceeds allowed {allowed_gap:g}s"
            )

    entry_target_ts = float(t0_ts - config.PRIMARY_ENTRY_LEAD_SEC)
    entry_available = all(
        any(
            entry_target_ts <= float(record["received_ts"])
            <= entry_target_ts
            + float(cadence_for(probe, -float(config.PRIMARY_ENTRY_LEAD_SEC)))
            for record in valid_by_probe[probe]
        )
        for probe in required
    )
    if not entry_available:
        notes.append(
            "fixed entry target lacks all-probe evidence within one cadence: "
            f"t0-{config.PRIMARY_ENTRY_LEAD_SEC}s"
        )

    available_exits: list[int] = []
    for exit_offset in config.PRIMARY_EXIT_OFFSETS_SEC:
        target_ts = float(t0_ts + exit_offset)
        if all(
            any(
                target_ts <= float(record["received_ts"])
                <= target_ts + float(cadence_for(probe, float(exit_offset)))
                for record in valid_by_probe[probe]
            )
            for probe in required
        ):
            available_exits.append(exit_offset)
    missing_exits = sorted(set(config.PRIMARY_EXIT_OFFSETS_SEC) - set(available_exits))
    if missing_exits:
        notes.append("fixed exit offsets lack all-probe evidence: " + ", ".join(map(str, missing_exits)))

    precision = int(t0_precision_sec)
    if precision > 1:
        notes.append(
            f"official t0 precision is {precision}s; seconds-grade replay requires 1s"
        )
    elif precision <= 0:
        notes.append("official t0 precision is missing or invalid")

    before = round(min(covered_before_values), 3) if covered_before_values else None
    after = round(min(covered_after_values), 3) if covered_after_values else None
    return {
        "ready": not notes,
        "successful_samples": sum(len(records) for records in valid_by_probe.values()),
        "successful_probes": successful_probes,
        "required_probes": list(required),
        "invalid_samples": invalid,
        "noncausal_samples": noncausal,
        "covered_before_t0_sec": before,
        "covered_after_t0_sec": after,
        "covers_full_burst_window": covers_full_burst,
        "max_burst_gap_sec_by_probe": gap_by_probe,
        "required_entry_lead_sec": config.PRIMARY_ENTRY_LEAD_SEC,
        "entry_available": entry_available,
        "required_exit_offsets_sec": list(config.PRIMARY_EXIT_OFFSETS_SEC),
        "available_exit_offsets_sec": available_exits,
        "t0_precision_sec": precision,
        "notes": notes,
    }


def _gap_stats(times: Sequence[float]) -> dict[str, Any]:
    ordered = sorted(times)
    gaps = [round(b - a, 3) for a, b in zip(ordered, ordered[1:])]
    return {
        "samples": len(ordered),
        # A mean flatters a capture that stalled once for thirty seconds, and a
        # thirty-second stall across t0 is exactly the failure that would invalidate
        # the replay. The maximum is the number that can disqualify a capture.
        "median_gap_sec": round(statistics.median(gaps), 3) if gaps else None,
        "max_gap_sec": max(gaps) if gaps else None,
    }


def sampling_report(
    per_probe_times: Mapping[str, Sequence[float]], *, t0_ts: int
) -> dict[str, Any]:
    """Measured cadence per probe, split by the two regimes the loop actually runs.

    Reporting one median across the whole window would average a deliberate 0.5s burst
    with a deliberate 3s background and produce a number describing neither. The split
    is not presentation: near t0 is where the hypothesis lives, so the burst carries its
    own sample count and its own worst gap, and the background is reported apart from
    it rather than diluting it."""
    report: dict[str, Any] = {}
    for probe, times in per_probe_times.items():
        burst = [t for t in times if abs(t - t0_ts) <= config.BURST_HALF_WIDTH_SEC]
        outside = [t for t in times if abs(t - t0_ts) > config.BURST_HALF_WIDTH_SEC]
        report[probe] = {
            "overall": _gap_stats(times),
            "burst": _gap_stats(burst),
            "outside_burst": _gap_stats(outside),
        }
    return report


# ------------------------------------------------------------------------ evidence


def _build_capture_receipt_from_committed_manifest(
    expected_manifest: Mapping[str, Any], capture_dir: Path
) -> dict[str, Any]:
    """Re-read committed bytes and build, but never persist, their receipt."""
    capture_dir = capture_dir.resolve(strict=False)
    manifest_path = capture_dir / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        committed_manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise CaptureError(f"committed capture manifest is unreadable: {manifest_path}") from exc
    if not isinstance(committed_manifest, Mapping):
        raise CaptureError("committed capture manifest is not an object")
    committed_manifest = dict(committed_manifest)
    if canonical_hash(committed_manifest) != canonical_hash(dict(expected_manifest)):
        raise CaptureError(
            "committed capture manifest differs from the authorized manifest draft"
        )

    capture_id = _require_safe_component(
        committed_manifest.get("capture_id"), "capture_id"
    )
    samples_path = capture_dir / "samples.jsonl"
    try:
        current_output_sha256 = _sha256_file(samples_path)
    except OSError as exc:
        raise CaptureError(f"capture samples are unreadable: {samples_path}") from exc
    declared_output_sha256 = str(committed_manifest.get("output_sha256") or "")
    if current_output_sha256 != declared_output_sha256:
        raise CaptureError(
            "capture samples output_sha256 changed after the manifest was committed"
        )
    receipt = {
        "schema": "premarket_perp_capture_receipt_v1",
        "capture_id": capture_id,
        "status": committed_manifest["status"],
        "stop_reason": committed_manifest["stop_reason"],
        "venue": committed_manifest["venue"],
        "symbol": committed_manifest["symbol"],
        "t0_ts": committed_manifest["t0_ts"],
        "t0_source_class": committed_manifest["t0_source_class"],
        "finished_at_utc": committed_manifest["finished_at_utc"],
        "rows_written": committed_manifest["rows_written"],
        "requests_made": committed_manifest["requests_made"],
        "transport_attempts": committed_manifest.get("transport_attempts"),
        "request_attempts": dict(committed_manifest.get("request_attempts") or {}),
        "evidence_class": committed_manifest.get("evidence_class"),
        "acceptance_capable": committed_manifest.get("acceptance_capable"),
        "output_sha256": current_output_sha256,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "sampling": committed_manifest["sampling"],
        "replay_readiness": committed_manifest["replay_readiness"],
        "venue_metadata_observed": committed_manifest.get("venue_metadata_observed"),
        "implementation": dict(committed_manifest.get("implementation") or {}),
        "lineage": dict(committed_manifest.get("lineage") or {}),
    }
    for key in LINEAGE_FIELDS:
        receipt[key] = receipt["lineage"].get(key)
    receipt["receipt_hash"] = canonical_hash(receipt)
    return receipt


# ----------------------------------------------------------------------- entrypoint


def _select_capture_job(
    *,
    event_id: str,
    source_class: str,
    horizon_sec: int,
    selection_now_ts: int,
    capture_id: str,
    plan: Mapping[str, Any],
) -> CaptureJob:
    eligible = registry.events_for_capture(
        now_ts=selection_now_ts,
        source_class=source_class,
        asset_class=registry.ASSET_CLASS_CRYPTO_TOKEN,
        horizon_sec=horizon_sec,
    )
    event = next(
        (
            item
            for item in eligible
            if event_id in (item.get("episode_id"), item.get("event_id"))
        ),
        None,
    )
    if event is None:
        raise CaptureError(
            f"{event_id} is not capture-eligible under {source_class}: it must be an "
            "official announcement in the verified production registry"
        )
    job = job_from_event(event, capture_id=capture_id)
    _validate_capture_lineage(job, event_id=event_id, plan=plan)
    return job


def capture_event(
    *,
    run_id: str,
    capture_token: str,
    event_id: str,
    source_class: str,
    horizon_sec: int = 24 * 3600,
    capture_root: Path | None = None,
) -> dict[str, Any]:
    """Take the token, take the claim, capture, release. In that order.

    The token proves a preflight consulted the gate, the plan and the capability scan;
    the claim proves nothing else in the workspace is writing market data at the same
    time. Neither is a formality: this is the only code here that reaches a live market
    while it is moving."""
    run_id = _require_safe_component(run_id, "run_id")
    if capture_root is not None and (
        capture_root.resolve(strict=False) != config.CAPTURE_ROOT.resolve(strict=False)
    ):
        raise CaptureError("capture_root cannot override the PlanOnly-bound path")
    capture_root = config.CAPTURE_ROOT.resolve(strict=False)
    capture_dir = (capture_root / run_id).resolve(strict=False)
    if capture_dir.parent != capture_root:
        raise CaptureError("run_id resolves outside the PlanOnly-bound capture root")

    selection_now_ts = int(time.time())
    plan = risk_gate.load_and_verify_plan()
    # Select and validate before consuming the one-shot token.  A non-eligible or
    # stale-lineage event must not be able to burn otherwise valid authority.
    job = _select_capture_job(
        event_id=event_id,
        source_class=source_class,
        horizon_sec=horizon_sec,
        selection_now_ts=selection_now_ts,
        capture_id=run_id,
        plan=plan,
    )

    risk_gate.consume_capture_token(
        token=capture_token,
        run_id=run_id,
        event_id=event_id,
        source_class=source_class,
    )

    claim = claim_global_market_writer(
        config.SHARED_WRITER_CLAIM_PATH,
        run_id=f"premarket_perp_capture__{run_id}",
        owner_pid=os.getpid(),
        owner_kind="premarket_perp_capture",
        plan_hash=str(plan["plan_hash"]),
        output_namespace=capture_dir,
    )
    terminal_status = "FAILED_EXCEPTION"
    manifest: dict[str, Any] | None = None
    terminal_detail: str | None = None
    try:
        # The token's gate view predates the writer claim.  Re-read both the gate and
        # the registry/plan lineage after exclusivity is held, before the first HTTP
        # request, closing the remaining authorization-to-network TOCTOU window.
        gate = risk_gate.read_shared_gate()
        if not (
            gate.get("open") is True
            and gate.get("status") in risk_gate.GATE_OPEN_STATUSES
        ):
            raise risk_gate.RiskGateError(
                f"post-claim shared gate is not open: {gate.get('status')}"
            )
        current_plan = risk_gate.load_and_verify_plan()
        if (
            current_plan.get("plan_id") != plan.get("plan_id")
            or current_plan.get("plan_hash") != plan.get("plan_hash")
        ):
            raise CaptureError("verified PlanOnly changed after token consumption")
        def reselect_at_current_time() -> CaptureJob:
            return _select_capture_job(
                event_id=event_id,
                source_class=source_class,
                horizon_sec=horizon_sec,
                selection_now_ts=int(time.time()),
                capture_id=run_id,
                plan=current_plan,
            )

        current_job = reselect_at_current_time()
        if current_job != job:
            raise CaptureError("capture event or registry lineage changed after claim")

        # This type and instance exist only in this authorized stack frame.  The
        # collection loop has no acceptance switch; only this one-shot permit can
        # classify and commit its draft after all live authorization checks passed.
        class CaptureCommitPermit:
            __slots__ = ()

        acceptance_permit = CaptureCommitPermit()
        commit_stage = "AUTHORIZED"

        def require_commit_permit(presented_permit: object, expected_stage: str) -> None:
            if presented_permit is not acceptance_permit:
                raise CaptureError("invalid capture commit permit")
            if commit_stage != expected_stage:
                raise CaptureError(
                    f"capture commit stage is {commit_stage}, expected {expected_stage}"
                )

        def commit_acceptance_manifest(
            draft: dict[str, Any], presented_permit: object
        ) -> None:
            nonlocal commit_stage
            require_commit_permit(presented_permit, "AUTHORIZED")
            readiness = draft.get("replay_readiness")
            if not isinstance(readiness, dict):
                readiness = {
                    "ready": False,
                    "notes": ["capture manifest draft has no replay readiness"],
                }
                draft["replay_readiness"] = readiness
            readiness["structural_ready"] = bool(readiness.get("ready"))
            draft.update(capture_evidence_classification(readiness))
            draft["plan_id"] = current_plan.get("plan_id")
            draft["plan_hash"] = current_plan.get("plan_hash")
            draft["implementation"] = dict(current_plan.get("implementation") or {})
            try:
                _write_json_exclusive(capture_dir / "manifest.json", draft)
            except FileExistsError as exc:
                raise CaptureError(
                    "manifest evidence already exists; exclusive capture refused: "
                    f"{capture_dir / 'manifest.json'}"
                ) from exc
            commit_stage = "MANIFEST_COMMITTED"

        def commit_capture_receipt(
            draft: Mapping[str, Any], presented_permit: object
        ) -> Path:
            nonlocal commit_stage
            require_commit_permit(presented_permit, "MANIFEST_COMMITTED")
            if (
                capture_dir.parent != capture_root
                or capture_dir.name != str(draft.get("capture_id") or "")
            ):
                raise CaptureError(
                    "capture receipt directory is outside the authorized capture identity"
                )
            receipt = _build_capture_receipt_from_committed_manifest(
                draft, capture_dir
            )
            evidence_root = config.EVIDENCE_DIR.resolve(strict=False)
            receipt_path = (
                evidence_root / f"{receipt['capture_id']}.json"
            ).resolve(strict=False)
            if receipt_path.parent != evidence_root:
                raise CaptureError("capture_id resolves outside the evidence path")
            evidence_root.mkdir(parents=True, exist_ok=True)
            try:
                _write_json_exclusive(receipt_path, receipt)
            except FileExistsError as exc:
                raise CaptureError(
                    f"evidence receipt already exists; exclusive commit refused: {receipt_path}"
                ) from exc
            commit_stage = "RECEIPT_COMMITTED"
            return receipt_path

        # A token that was due before a slow gate/claim is not authority to create a
        # capture directory after the narrow launch boundary has elapsed.
        if reselect_at_current_time() != job:
            raise CaptureError("capture event or registry lineage changed before setup")

        capture_root.mkdir(parents=True, exist_ok=True)
        try:
            capture_dir.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise CaptureError(f"capture directory already exists: {capture_dir}") from exc
        _write_run_record(
            run_id=run_id,
            status="RUNNING",
            event_id=event_id,
            capture_dir=capture_dir,
        )
        # Filesystem setup is outside the network boundary. Recheck once more at the
        # last possible instant before the first venue request.
        if reselect_at_current_time() != job:
            raise CaptureError("capture event or registry lineage changed before network")

        def live_metadata_fetch(url: str, params: Mapping[str, Any]) -> Any:
            return public_http.get_json(
                url,
                params=params,
                timeout_sec=10,
                max_retries=0,
            )

        network_deadline = time.monotonic() + config.MAX_CAPTURE_RUNTIME_SEC

        def production_request_boundary() -> str | None:
            if time.time() >= job.t0_ts + config.CAPTURE_WINDOW_AFTER_SEC:
                return "window_complete"
            if time.monotonic() > network_deadline:
                return "max_runtime_sec_exceeded"
            if config.STOP_REQUEST_PATH.is_file():
                return "stop_requested"
            return None

        venue_metadata = observe_venue_metadata(
            job,
            fetch=live_metadata_fetch,
            boundary_check=production_request_boundary,
        )

        def live_fetch(probe: Probe, symbol: str, timeout_sec: int) -> Any:
            return public_http.get_json(
                probe.url,
                params=probe.params_for(symbol),
                timeout_sec=timeout_sec,
                max_retries=0,
            )

        boundary = production_request_boundary()
        if boundary is not None:
            raise CaptureError(f"capture boundary reached before market polling: {boundary}")
        remaining_runtime_sec = min(
            float(config.MAX_CAPTURE_RUNTIME_SEC),
            max(0.001, network_deadline - time.monotonic()),
        )
        manifest = _run_capture_core(
            job,
            capture_dir=capture_dir,
            venue_metadata=venue_metadata,
            fetch=live_fetch,
            max_runtime_sec=remaining_runtime_sec,
        )
        commit_acceptance_manifest(manifest, acceptance_permit)
        commit_capture_receipt(manifest, acceptance_permit)
        terminal_status = str(manifest.get("status") or "STOPPED_INCOMPLETE")
    except BaseException as exc:
        terminal_status = "FAILED_EXCEPTION"
        terminal_detail = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        # If this terminal record cannot be committed, do not release the claim: an
        # apparently free writer with no durable terminal accounting is unsafe.
        _write_run_record(
            run_id=run_id,
            status=terminal_status,
            event_id=event_id,
            capture_dir=capture_dir,
            detail=terminal_detail,
            manifest=manifest,
        )
        # Clear only while this run still owns the global claim. Once release returns,
        # a new run may legitimately create its own stop request.
        config.STOP_REQUEST_PATH.unlink(missing_ok=True)
        release_global_market_writer(
            config.SHARED_WRITER_CLAIM_PATH,
            run_id=f"premarket_perp_capture__{run_id}",
            owner_pid=int(claim["owner_pid"]),
            ownership_token=str(claim["ownership_token"]),
            final_status=terminal_status,
            archive_dir=config.CLAIM_ARCHIVE_DIR,
        )

    assert manifest is not None
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One bounded visible capture around a listing t0.")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--capture-token", default="")
    parser.add_argument("--event-id", default="")
    parser.add_argument("--source-class", choices=sorted(registry.SOURCE_CLASSES),
                        help="required with --capture: which t0 source class to trust")
    parser.add_argument("--horizon-hours", type=int, default=24)
    parser.add_argument("--plan-echo", action="store_true",
                        help="print the capture bounds this build would honour")
    args = parser.parse_args(argv)

    if args.plan_echo:
        print(json.dumps({
            "window_before_sec": config.CAPTURE_WINDOW_BEFORE_SEC,
            "window_after_sec": config.CAPTURE_WINDOW_AFTER_SEC,
            "max_runtime_sec": config.MAX_CAPTURE_RUNTIME_SEC,
            "max_requests": config.MAX_REQUESTS_PER_CAPTURE,
            "cadence_sec": config.PROBE_CADENCE_SEC,
            "burst_cadence_sec": config.BURST_CADENCE_SEC,
            "burst_half_width_sec": config.BURST_HALF_WIDTH_SEC,
            "probes": sorted({probe.probe for probe in PROBE_TABLE}),
            "venues": sorted({probe.venue for probe in PROBE_TABLE}),
        }, ensure_ascii=False))
        return 0
    if not args.capture:
        raise SystemExit("no action requested")
    if not (args.run_id and args.capture_token and args.event_id and args.source_class):
        raise SystemExit(
            "--capture requires --run-id, --capture-token, --event-id and --source-class"
        )
    manifest = capture_event(
        run_id=args.run_id, capture_token=args.capture_token, event_id=args.event_id,
        source_class=args.source_class,
        horizon_sec=args.horizon_hours * 3600,
    )
    print(json.dumps({
        "status": manifest["status"],
        "capture_id": manifest["capture_id"],
        "stop_reason": manifest["stop_reason"],
        "rows_written": manifest["rows_written"],
        "requests_made": manifest["requests_made"],
        "replay_readiness": manifest["replay_readiness"],
        "venue_metadata_observed": manifest["venue_metadata_observed"],
        "sampling": manifest["sampling"],
    }, ensure_ascii=False))
    # A capture that finished its loop but cannot answer the question is not a success,
    # and a script that only checks the exit code must not be told otherwise.
    ready = bool(manifest["replay_readiness"]["ready"])
    return 0 if manifest["status"] == "COMPLETED" and ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
