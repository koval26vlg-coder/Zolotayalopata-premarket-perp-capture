"""Bounded post-hoc public OHLCV acquisition with append-only evidence roots."""

from __future__ import annotations

import argparse
import copy
import csv
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import global_market_writer_claim as writer_claim
import historical_event_builder
import project_config as config
import public_http
import risk_gate


WRITE_CLASS = "historical_market_data_acquisition"
OHLCV_ENDPOINTS = {
    "bybit": "https://api.bybit.com/v5/market/kline",
    "okx": "https://www.okx.com/api/v5/market/history-candles",
    "gate": "https://download.gatedata.org/futures_usdt/candlesticks_1m/202602/AZTEC_USDT-202602.csv.gz",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_MAX_GATE_ARCHIVE_ROWS = 50_000
_MAX_GATE_DECOMPRESSED_BYTES = 16 * 1024 * 1024


class HistoricalAcquisitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class HistoricalAcquisitionRoots:
    raw_root: Path
    manifest_root: Path
    receipt_root: Path

    def __post_init__(self) -> None:
        resolved = {
            Path(self.raw_root).resolve(strict=False),
            Path(self.manifest_root).resolve(strict=False),
            Path(self.receipt_root).resolve(strict=False),
        }
        if len(resolved) != 3:
            raise ValueError("historical evidence roots must be distinct")


@dataclass(frozen=True)
class HistoricalAcquisitionLimits:
    max_events: int
    max_requests: int
    max_runtime_sec: int
    max_retries: int = 0

    def __post_init__(self) -> None:
        for field in ("max_events", "max_requests", "max_runtime_sec"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        if self.max_retries != 0:
            raise ValueError("historical acquisition retries are fixed at zero")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_json_exclusive(path: Path, payload: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _require_safe_id(value: object, field: str) -> str:
    text = str(value or "")
    if not text or _SAFE_ID.fullmatch(text) is None:
        raise HistoricalAcquisitionError(f"unsafe {field}")
    return text


def _parse_received_at(value: str) -> int:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise HistoricalAcquisitionError("received_at_utc must be UTC seconds")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise HistoricalAcquisitionError("received_at_utc must be UTC seconds") from exc
    return int(parsed.timestamp())


def _request_params(seed: dict[str, object]) -> dict[str, object]:
    venue = str(seed.get("venue") or "").lower()
    contract = str(seed.get("premarket_contract_id") or "")
    start = seed.get("history_start_ts")
    end = seed.get("history_end_ts")
    if (
        venue not in OHLCV_ENDPOINTS
        or not contract
        or isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start >= end
    ):
        raise HistoricalAcquisitionError("historical seed request bounds are invalid")
    if venue == "bybit":
        return {
            "category": "linear",
            "symbol": contract,
            "interval": "1",
            "start": start * 1000,
            "end": end * 1000,
            "limit": 200,
        }
    if venue == "okx":
        return {
            "instId": contract,
            "bar": "1m",
            "after": end * 1000,
            "before": start * 1000,
            "limit": 200,
        }
    return {"from": start, "to": end}


def _parse_gate_archive(
    compressed: bytes,
    *,
    start_ts: int,
    end_ts: int,
) -> dict[str, object]:
    if not isinstance(compressed, bytes) or not compressed:
        raise HistoricalAcquisitionError("Gate archive body is empty")
    rows: list[list[str]] = []
    decompressed_bytes = 0
    total_rows = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as stream:
            for raw_line in stream:
                decompressed_bytes += len(raw_line)
                if decompressed_bytes > _MAX_GATE_DECOMPRESSED_BYTES:
                    raise HistoricalAcquisitionError("Gate archive decompressed byte limit exceeded")
                total_rows += 1
                if total_rows > _MAX_GATE_ARCHIVE_ROWS:
                    raise HistoricalAcquisitionError("Gate archive row limit exceeded")
                fields = next(csv.reader([raw_line.decode("utf-8").strip()]))
                if len(fields) != 6:
                    raise HistoricalAcquisitionError("Gate archive candle row is malformed")
                try:
                    timestamp = int(fields[0])
                except ValueError as exc:
                    raise HistoricalAcquisitionError(
                        "Gate archive candle timestamp is malformed"
                    ) from exc
                if start_ts <= timestamp <= end_ts:
                    rows.append(fields)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise HistoricalAcquisitionError("Gate archive is not valid gzip CSV") from exc
    if not rows:
        raise HistoricalAcquisitionError("Gate archive contains no rows in requested window")
    return {
        "archive_schema": "gate_futures_candlesticks_1m_v1",
        "rows": rows,
    }


def default_public_transport(
    url: str,
    params: dict[str, object],
    *,
    timeout_sec: int,
    max_retries: int,
) -> object:
    """Use the hardened public HTTP layer for the three preregistered sources."""

    if url == OHLCV_ENDPOINTS["gate"]:
        start = params.get("from")
        end = params.get("to")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
        ):
            raise HistoricalAcquisitionError("Gate archive filter bounds are invalid")
        body = public_http.get_bytes(
            url,
            params=None,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
        )
        return _parse_gate_archive(body, start_ts=start, end_ts=end)
    if url not in {OHLCV_ENDPOINTS["bybit"], OHLCV_ENDPOINTS["okx"]}:
        raise HistoricalAcquisitionError("historical endpoint is not preregistered")
    return public_http.get_json(
        url,
        params=params,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
    )


def _artifact_paths(
    roots: HistoricalAcquisitionRoots,
    run_id: str,
    event_id: str,
) -> tuple[Path, Path, Path]:
    stem = f"{run_id}--{event_id}"
    return (
        Path(roots.raw_root) / f"{stem}.json",
        Path(roots.manifest_root) / f"{stem}.json",
        Path(roots.receipt_root) / f"{run_id}.json",
    )


def _validate_preflight(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("ok") is not True or value.get("verified") is not True:
        raise HistoricalAcquisitionError("historical acquisition preflight blocked")
    plan_id = value.get("plan_id")
    plan_hash = value.get("plan_hash")
    if not isinstance(plan_id, str) or not plan_id or not isinstance(plan_hash, str):
        raise HistoricalAcquisitionError("historical acquisition preflight is incomplete")
    return value


def run_historical_acquisition(
    *,
    run_id: str,
    seeds: list[dict[str, object]],
    roots: HistoricalAcquisitionRoots,
    limits: HistoricalAcquisitionLimits,
    transport: Callable[..., object],
    received_at_utc: str,
) -> dict[str, object]:
    """Acquire one bounded batch after risk preflight, claim and post-claim gate."""

    safe_run_id = _require_safe_id(run_id, "run_id")
    if not isinstance(seeds, list) or not all(isinstance(item, dict) for item in seeds):
        raise HistoricalAcquisitionError("seeds must be a list of objects")
    if not callable(transport):
        raise HistoricalAcquisitionError("transport must be injected")
    retrieved_at_ts = _parse_received_at(received_at_utc)
    preflight = _validate_preflight(
        risk_gate.preflight(write_class=WRITE_CLASS, run_id=safe_run_id)
    )
    owner_pid = os.getpid()
    claim: dict[str, object] | None = None
    terminal_status = "RETRY_NEXT_INTERVAL"
    started = time.monotonic()
    try:
        claim = writer_claim.claim_global_market_writer(
            config.SHARED_WRITER_CLAIM_PATH,
            run_id=safe_run_id,
            owner_pid=owner_pid,
            owner_kind="premarket_historical_public_data",
            plan_hash=str(preflight["plan_hash"]),
            output_namespace=str(Path(roots.raw_root).resolve(strict=False)),
        )
        gate = risk_gate.read_shared_gate()
        if not isinstance(gate, dict) or gate.get("open") is not True:
            raise HistoricalAcquisitionError("shared gate closed after writer claim")

        raw_root = Path(roots.raw_root)
        manifest_root = Path(roots.manifest_root)
        receipt_root = Path(roots.receipt_root)
        raw_root.mkdir(parents=True, exist_ok=True)
        manifest_root.mkdir(parents=True, exist_ok=True)
        receipt_root.mkdir(parents=True, exist_ok=True)

        receipt_path = receipt_root / f"{safe_run_id}.json"
        if receipt_path.exists():
            raise HistoricalAcquisitionError("historical acquisition run identity exists")

        completed: list[str] = []
        failed: list[str] = []
        queued: list[str] = []
        venue_errors: dict[str, list[str]] = {}
        request_count = 0
        boundary_reason: str | None = None
        for index, original_seed in enumerate(seeds):
            if len(completed) + len(failed) >= limits.max_events:
                queued.extend(str(item.get("event_id") or "") for item in seeds[index:])
                boundary_reason = "max_events"
                break
            if request_count >= limits.max_requests:
                queued.extend(str(item.get("event_id") or "") for item in seeds[index:])
                boundary_reason = "max_requests"
                break
            if time.monotonic() - started > limits.max_runtime_sec:
                queued.extend(str(item.get("event_id") or "") for item in seeds[index:])
                boundary_reason = "max_runtime_sec"
                break

            seed = copy.deepcopy(original_seed)
            event_id = _require_safe_id(seed.get("event_id"), "event_id")
            venue = str(seed.get("venue") or "").lower()
            if venue not in OHLCV_ENDPOINTS:
                raise HistoricalAcquisitionError(f"unsupported venue: {venue}")
            raw_path, manifest_path, _ = _artifact_paths(
                roots, safe_run_id, event_id
            )
            if raw_path.exists() or manifest_path.exists():
                raise HistoricalAcquisitionError("historical event identity exists")
            params = _request_params(seed)
            source_url = OHLCV_ENDPOINTS[venue]
            request_count += 1
            try:
                payload = transport(
                    source_url,
                    params,
                    timeout_sec=20,
                    max_retries=limits.max_retries,
                )
                seed["history_source_class"] = (
                    "VENUE_PUBLIC_ARCHIVE_POSTHOC_OHLCV"
                    if venue == "gate"
                    else "VENUE_PUBLIC_REST_POSTHOC_OHLCV"
                )
                seed["history_source_url"] = source_url
                seed["history_request_params"] = copy.deepcopy(params)
                manifest = historical_event_builder.build_historical_event(
                    seed,
                    payload,
                    retrieved_at_ts,
                )
                raw_record: dict[str, object] = {
                    "schema": "premarket_perp_historical_raw_v1",
                    "run_id": safe_run_id,
                    "event_id": event_id,
                    "venue": venue,
                    "source_url": source_url,
                    "request_params": copy.deepcopy(params),
                    "received_at_utc": received_at_utc,
                    "posthoc_retrieval": True,
                    "evidence_use": "DESCRIPTIVE_ONLY",
                    "payload": copy.deepcopy(payload),
                }
                raw_record["record_sha256"] = _canonical_sha256(raw_record)
                _write_json_exclusive(raw_path, raw_record)
                try:
                    _write_json_exclusive(manifest_path, manifest)
                except Exception:
                    raw_path.unlink(missing_ok=True)
                    raise
                completed.append(event_id)
            except Exception as exc:
                failed.append(event_id)
                venue_errors.setdefault(venue, []).append(
                    f"{type(exc).__name__}: {exc}"
                )

        if queued:
            terminal_status = "BOUNDED_RETRY_NEXT_INTERVAL"
        elif failed and completed:
            terminal_status = "PARTIAL_RETRY_NEXT_INTERVAL"
        elif failed:
            terminal_status = "RETRY_NEXT_INTERVAL"
        else:
            terminal_status = "HISTORICAL_ACQUISITION_COMPLETE"
        receipt: dict[str, object] = {
            "schema": "premarket_perp_historical_acquisition_receipt_v1",
            "run_id": safe_run_id,
            "status": terminal_status,
            "plan_id": preflight["plan_id"],
            "plan_hash": preflight["plan_hash"],
            "received_at_utc": received_at_utc,
            "limits": asdict(limits),
            "requests_made": request_count,
            "completed_events": len(completed),
            "failed_events": len(failed),
            "completed_event_ids": completed,
            "failed_event_ids": failed,
            "queued_event_ids": queued,
            "venue_errors": venue_errors,
            "boundary_reason": boundary_reason,
            "pending_retry": bool(failed or queued),
            "posthoc_retrieval": True,
            "evidence_use": "DESCRIPTIVE_ONLY",
        }
        receipt["receipt_sha256"] = _canonical_sha256(receipt)
        _write_json_exclusive(receipt_path, receipt)
        return receipt
    finally:
        if claim is not None:
            token = claim.get("ownership_token")
            claim_owner = claim.get("owner_pid", owner_pid)
            writer_claim.release_global_market_writer(
                config.SHARED_WRITER_CLAIM_PATH,
                run_id=safe_run_id,
                owner_pid=int(claim_owner),
                ownership_token=str(token),
                final_status=terminal_status,
                archive_dir=config.CLAIM_ARCHIVE_DIR,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bounded public post-hoc OHLCV acquisition (research only)."
    )
    parser.add_argument("--seed-file", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--received-at-utc", required=True)
    args = parser.parse_args(argv)
    try:
        seed_set = json.loads(args.seed_file.read_text(encoding="utf-8"))
        if (
            not isinstance(seed_set, dict)
            or seed_set.get("schema") != "premarket_perp_historical_seed_set_v1"
            or not isinstance(seed_set.get("events"), list)
        ):
            raise HistoricalAcquisitionError("historical seed set is invalid")
        result = run_historical_acquisition(
            run_id=args.run_id,
            seeds=seed_set["events"],
            roots=HistoricalAcquisitionRoots(
                raw_root=config.HISTORICAL_RAW_ROOT,
                manifest_root=config.HISTORICAL_MANIFEST_ROOT,
                receipt_root=config.HISTORICAL_RECEIPT_ROOT,
            ),
            limits=HistoricalAcquisitionLimits(
                max_events=config.MAX_HISTORICAL_EVENTS_PER_RUN,
                max_requests=config.MAX_HISTORICAL_REQUESTS_PER_RUN,
                max_runtime_sec=config.MAX_HISTORICAL_RUNTIME_SEC,
                max_retries=config.MAX_HISTORICAL_RETRIES_PER_REQUEST,
            ),
            transport=default_public_transport,
            received_at_utc=args.received_at_utc,
        )
    except (OSError, ValueError, HistoricalAcquisitionError) as exc:
        result = {
            "status": "RETRY_NEXT_INTERVAL",
            "pending_retry": True,
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0 if result.get("status") == "HISTORICAL_ACQUISITION_COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
