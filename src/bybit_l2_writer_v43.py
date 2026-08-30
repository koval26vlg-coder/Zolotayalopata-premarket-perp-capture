"""Deterministic fixture-only Bybit L2 writer for future event-bound v43 work.

This module has no network, PlanOnly, token, claim, scheduling, or order capability.
It accepts only an exact in-memory fixture source and writes a create-only evidence
bundle below a caller-owned temporary directory outside this repository.  Bybit
deltas whose predecessor cannot be proven remain descriptive and are never promoted
to causal depth.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from canonical_hash import canonical_json_bytes
from l2_book import (
    ApplyStatus,
    BookLevel,
    FrameKind,
    L2Book,
    MarketPhase,
    NormalizedL2Frame,
)
from venue_ws_v43 import GAP_NONE, GAP_RESET, NormalizedEvent, parse_message
import project_config as config


RAW_SCHEMA = "premarket_perp_bybit_l2_fixture_raw_frame_v43"
DEPTH_SCHEMA = "premarket_perp_bybit_l2_fixture_depth_v43"
MANIFEST_SCHEMA = "premarket_perp_bybit_l2_fixture_manifest_v43"
RECEIPT_SCHEMA = "premarket_perp_bybit_l2_fixture_terminal_receipt_v43"
MAX_RAW_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_FIXTURE_MESSAGES = 1_000_000
MAX_FIXTURE_RUNTIME_SEC = 3_600.0
BYBIT_MAX_LEVELS = 50
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CONTRACT = re.compile(r"^[A-Z0-9]{2,40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_RESEARCH_ONLY = {
    "orders_created": 0,
    "network_used": False,
    "private_api_used": False,
    "live_execution": False,
    "claim_used": False,
    "capture_token_used": False,
    "plan_activated": False,
}


class BybitL2WriterError(RuntimeError):
    """The synthetic source or create-only output violates the fixture contract."""


def _finite(value: object, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BybitL2WriterError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise BybitL2WriterError(f"{field} must be finite{' and positive' if positive else ''}")
    return number


def _positive_int(value: object, field: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BybitL2WriterError(f"{field} must be a positive integer")
    if maximum is not None and value > maximum:
        raise BybitL2WriterError(f"{field} exceeds its fixture ceiling {maximum}")
    return value


@dataclass(frozen=True)
class FixtureCaptureSpec:
    capture_id: str
    contract_id: str
    event_lineage_hash: str
    t0_ts: int
    window_start_ts: float
    window_end_ts: float
    max_runtime_sec: float
    max_messages: int
    max_levels: int = BYBIT_MAX_LEVELS

    def __post_init__(self) -> None:
        if not isinstance(self.capture_id, str) or _SAFE_ID.fullmatch(self.capture_id) is None:
            raise BybitL2WriterError("capture_id must be a safe single path component")
        if not isinstance(self.contract_id, str) or _CONTRACT.fullmatch(self.contract_id) is None:
            raise BybitL2WriterError("contract_id must be one canonical Bybit symbol")
        if (
            not isinstance(self.event_lineage_hash, str)
            or _SHA256.fullmatch(self.event_lineage_hash) is None
        ):
            raise BybitL2WriterError("event_lineage_hash must be lowercase SHA-256")
        t0 = _positive_int(self.t0_ts, "t0_ts")
        start = _finite(self.window_start_ts, "window_start_ts")
        end = _finite(self.window_end_ts, "window_end_ts")
        runtime = _finite(self.max_runtime_sec, "max_runtime_sec", positive=True)
        if runtime > MAX_FIXTURE_RUNTIME_SEC:
            raise BybitL2WriterError("max_runtime_sec exceeds the fixture ceiling")
        if not start < t0 < end:
            raise BybitL2WriterError("capture window must strictly surround t0")
        _positive_int(self.max_messages, "max_messages", maximum=MAX_FIXTURE_MESSAGES)
        _positive_int(self.max_levels, "max_levels", maximum=BYBIT_MAX_LEVELS)
        object.__setattr__(self, "window_start_ts", start)
        object.__setattr__(self, "window_end_ts", end)
        object.__setattr__(self, "max_runtime_sec", runtime)


@dataclass(frozen=True)
class FixtureWsRecord:
    kind: str
    connection_epoch: int
    raw_payload: bytes = b""
    received_ts: float | None = None
    monotonic_ns: int | None = None
    close_reason: str | None = None

    def __post_init__(self) -> None:
        kind = str(self.kind).upper()
        epoch = _positive_int(self.connection_epoch, "connection_epoch")
        if kind not in {"OPEN", "MESSAGE", "CLOSE"}:
            raise BybitL2WriterError("fixture record kind must be OPEN, MESSAGE or CLOSE")
        raw = bytes(self.raw_payload)
        if kind == "MESSAGE":
            if not raw or len(raw) > MAX_RAW_PAYLOAD_BYTES:
                raise BybitL2WriterError("MESSAGE raw payload is empty or exceeds its bound")
            received = _finite(self.received_ts, "received_ts", positive=True)
            monotonic_ns = _positive_int(self.monotonic_ns, "monotonic_ns")
            if self.close_reason is not None:
                raise BybitL2WriterError("MESSAGE cannot carry close_reason")
            object.__setattr__(self, "received_ts", received)
            object.__setattr__(self, "monotonic_ns", monotonic_ns)
        else:
            if raw or self.received_ts is not None or self.monotonic_ns is not None:
                raise BybitL2WriterError(f"{kind} cannot carry message payload or clocks")
            if kind == "OPEN" and self.close_reason is not None:
                raise BybitL2WriterError("OPEN cannot carry close_reason")
            if kind == "CLOSE" and not isinstance(self.close_reason, str):
                raise BybitL2WriterError("CLOSE must carry a string reason")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "connection_epoch", epoch)
        object.__setattr__(self, "raw_payload", raw)

    @classmethod
    def open(cls, connection_epoch: int) -> "FixtureWsRecord":
        return cls(kind="OPEN", connection_epoch=connection_epoch)

    @classmethod
    def message(
        cls,
        connection_epoch: int,
        raw_payload: bytes,
        *,
        received_ts: float,
        monotonic_ns: int,
    ) -> "FixtureWsRecord":
        return cls(
            kind="MESSAGE",
            connection_epoch=connection_epoch,
            raw_payload=raw_payload,
            received_ts=received_ts,
            monotonic_ns=monotonic_ns,
        )

    @classmethod
    def close(cls, connection_epoch: int, *, reason: str) -> "FixtureWsRecord":
        return cls(kind="CLOSE", connection_epoch=connection_epoch, close_reason=reason)


class StaticBybitL2FixtureSource:
    """Exact in-memory source; it cannot call or wrap an external transport."""

    __slots__ = ("_records", "_index")

    def __init__(self, records: Sequence[FixtureWsRecord]) -> None:
        values = tuple(records)
        if not all(type(record) is FixtureWsRecord for record in values):
            raise BybitL2WriterError("fixture source accepts only exact FixtureWsRecord rows")
        self._records = values
        self._index = 0

    @property
    def consumed_count(self) -> int:
        return self._index

    def next_record(self) -> FixtureWsRecord | None:
        if self._index >= len(self._records):
            return None
        record = self._records[self._index]
        self._index += 1
        return record


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise BybitL2WriterError("fixture WebSocket payload is not UTF-8") from exc

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (ValueError, RecursionError) as exc:
        raise BybitL2WriterError(f"fixture WebSocket payload is not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BybitL2WriterError("fixture WebSocket payload must be one JSON object")
    return value


def _claim_hash(payload: Mapping[str, Any], field: str) -> str:
    material = dict(payload)
    material.pop(field, None)
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def _sealed(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = dict(payload)
    result[field] = _claim_hash(result, field)
    return result


def _write_line(handle, payload: Mapping[str, Any]) -> None:
    handle.write(canonical_json_bytes(dict(payload)) + b"\n")


def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> bytes:
    raw = canonical_json_bytes(dict(payload)) + b"\n"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise BybitL2WriterError(f"create-only evidence already exists: {path.name}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if path.read_bytes() != raw:
        raise BybitL2WriterError(f"exact readback failed: {path.name}")
    return raw


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    try:
        return path.is_symlink() or bool(is_junction and is_junction(path))
    except OSError as exc:
        raise BybitL2WriterError("cannot inspect fixture path identity") from exc


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left_text = os.path.normcase(str(left.resolve(strict=False)))
        right_text = os.path.normcase(str(right.resolve(strict=False)))
    except OSError as exc:
        raise BybitL2WriterError("cannot verify fixture/production path separation") from exc
    left_drive, _ = os.path.splitdrive(left_text)
    right_drive, _ = os.path.splitdrive(right_text)
    if left_drive and right_drive and left_drive != right_drive:
        return False
    try:
        common = os.path.normcase(os.path.commonpath((left_text, right_text)))
    except ValueError as exc:
        raise BybitL2WriterError("cannot verify fixture/production path separation") from exc
    return common in {left_text, right_text}


def _new_external_output_dir(output_dir: Path) -> Path:
    supplied = Path(output_dir)
    if supplied.exists():
        raise BybitL2WriterError("output directory must be new and must not already exist")
    target = supplied.resolve(strict=False)
    protected = {
        _REPOSITORY_ROOT,
        Path(config.CONTROL_ROOT),
        Path(config.CAPTURE_ROOT),
        Path(config.CAPTURE_TOKEN_PATH).parent,
        Path(config.SHARED_WRITER_CLAIM_PATH).parent,
        Path(config.OFFICIAL_T0_ARMING_ROOT),
        Path(config.EVENT_BOUND_PLAN_PROPOSAL_ROOT),
    }
    if any(_paths_overlap(target, root) for root in protected):
        raise BybitL2WriterError(
            "fixture output must be outside repository and production paths"
        )
    parent = target.parent
    if not parent.is_dir() or _is_link_or_junction(parent):
        raise BybitL2WriterError("fixture output parent must be a plain caller-owned directory")
    try:
        target.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise BybitL2WriterError("output directory must be new") from exc
    return target


def _levels(rows: Sequence[tuple[str, str]]) -> tuple[BookLevel, ...]:
    return tuple(BookLevel(price=Decimal(price), size=Decimal(size)) for price, size in rows)


def _normalized_frame(
    event: NormalizedEvent,
    record: FixtureWsRecord,
    *,
    raw_sha256: str,
    previous_sequence: int | None,
) -> NormalizedL2Frame:
    if event.exchange_ts_ms is None or event.sequence_end is None:
        raise BybitL2WriterError("Bybit L2 event lacks exchange timestamp or update id")
    return NormalizedL2Frame(
        venue="bybit",
        symbol=event.contract,
        kind=FrameKind.SNAPSHOT if event.action == "snapshot" else FrameKind.DELTA,
        phase=MarketPhase.CONTINUOUS,
        connection_epoch=record.connection_epoch,
        sequence=event.sequence_end,
        previous_sequence=previous_sequence,
        exchange_ts=event.exchange_ts_ms / 1000.0,
        received_ts=float(record.received_ts),
        monotonic_ns=int(record.monotonic_ns),
        bids=_levels(event.bids),
        asks=_levels(event.asks),
        raw_sha256=raw_sha256,
    )


def _depth_row(
    *,
    spec: FixtureCaptureSpec,
    record_sequence: int,
    source_record_hash: str,
    snapshot,
    previous_row_hash: str | None,
) -> dict[str, Any]:
    payload = {
        "schema": DEPTH_SCHEMA,
        "snapshot_id": f"depth-{record_sequence:08d}",
        "capture_id": spec.capture_id,
        "event_lineage_hash": spec.event_lineage_hash,
        "venue": "bybit",
        "contract_id": spec.contract_id,
        "source_record_sequence": record_sequence,
        "source_record_hash": source_record_hash,
        "connection_epoch": snapshot.connection_epoch,
        "venue_sequence": snapshot.sequence,
        "exchange_ts": snapshot.exchange_ts,
        "received_ts": snapshot.received_ts,
        "monotonic_ns": snapshot.monotonic_ns,
        "gap_free": snapshot.gap_free,
        "execution_ready": snapshot.execution_ready,
        "frame_chain_sha256": snapshot.frame_chain_sha256,
        "bids": [[str(row.price), str(row.size)] for row in snapshot.bids],
        "asks": [[str(row.price), str(row.size)] for row in snapshot.asks],
        "previous_row_hash": previous_row_hash,
    }
    return _sealed(payload, "row_hash")


def run_fixture_bybit_l2_capture(
    spec: FixtureCaptureSpec,
    *,
    output_dir: Path,
    source: StaticBybitL2FixtureSource,
    monotonic: Callable[[], float] = time.monotonic,
    should_stop: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Write one deterministic, synthetic Bybit L2 bundle from immutable fixtures."""

    if type(spec) is not FixtureCaptureSpec:
        raise BybitL2WriterError("spec must be exact FixtureCaptureSpec")
    if type(source) is not StaticBybitL2FixtureSource:
        raise BybitL2WriterError("source must be exact StaticBybitL2FixtureSource")
    if not callable(monotonic) or not callable(should_stop):
        raise BybitL2WriterError("monotonic and should_stop must be callable")

    target = _new_external_output_dir(Path(output_dir))
    raw_path = target / "raw-frames.jsonl"
    depth_path = target / "normalized-depth.jsonl"
    try:
        raw_handle = raw_path.open("xb")
        depth_handle = depth_path.open("xb")
    except BaseException:
        raw_path.unlink(missing_ok=True)
        depth_path.unlink(missing_ok=True)
        raise

    start = _finite(monotonic(), "monotonic start")
    book = L2Book(venue="bybit", symbol=spec.contract_id, max_levels=spec.max_levels)
    current_epoch: int | None = None
    last_epoch = 0
    epoch_rows: list[dict[str, Any]] = []
    current_epoch_row: dict[str, Any] | None = None
    last_parser_sequence: int | None = None
    last_record_hash: str | None = None
    last_depth_hash: str | None = None
    record_sequence = 0
    messages = 0
    depth_rows = 0
    gaps: list[dict[str, Any]] = []
    termination_reason = "source_exhausted"

    try:
        with raw_handle, depth_handle:
            while True:
                try:
                    stop_value = should_stop()
                except Exception as exc:
                    raise BybitL2WriterError(f"stop predicate failed closed: {exc}") from exc
                if stop_value is not True and stop_value is not False:
                    raise BybitL2WriterError("stop predicate must return bool")
                if stop_value:
                    termination_reason = "stop_requested"
                    break
                if _finite(monotonic(), "monotonic clock") - start >= spec.max_runtime_sec:
                    termination_reason = "max_runtime_sec_exceeded"
                    break
                if messages >= spec.max_messages:
                    termination_reason = "max_messages_exceeded"
                    break

                record = source.next_record()
                if record is None:
                    termination_reason = "source_exhausted"
                    break
                if record.kind == "OPEN":
                    if current_epoch is not None:
                        raise BybitL2WriterError("new epoch opened before the current epoch closed")
                    if record.connection_epoch <= last_epoch:
                        raise BybitL2WriterError("connection epochs must strictly increase")
                    current_epoch = record.connection_epoch
                    last_epoch = current_epoch
                    last_parser_sequence = None
                    book.begin_connection(current_epoch)
                    current_epoch_row = {
                        "connection_epoch": current_epoch,
                        "closed": False,
                        "snapshot_seen": False,
                        "messages": 0,
                        "depth_rows": 0,
                        "gap_count": 0,
                    }
                    epoch_rows.append(current_epoch_row)
                    continue
                if record.kind == "CLOSE":
                    if current_epoch != record.connection_epoch or current_epoch_row is None:
                        raise BybitL2WriterError("CLOSE does not match the active epoch")
                    current_epoch_row["closed"] = True
                    current_epoch_row["close_reason"] = record.close_reason
                    current_epoch = None
                    current_epoch_row = None
                    last_parser_sequence = None
                    continue
                if current_epoch != record.connection_epoch or current_epoch_row is None:
                    raise BybitL2WriterError("MESSAGE requires its explicitly opened epoch")
                if float(record.received_ts) > spec.window_end_ts:
                    termination_reason = "window_complete"
                    break
                if float(record.received_ts) < spec.window_start_ts:
                    raise BybitL2WriterError("fixture message precedes the declared capture window")

                messages += 1
                record_sequence += 1
                current_epoch_row["messages"] += 1
                raw_payload = record.raw_payload
                raw_sha256 = hashlib.sha256(raw_payload).hexdigest()
                decoded = _strict_json_object(raw_payload)
                events = parse_message(
                    "bybit",
                    decoded,
                    contract=spec.contract_id,
                    connection="public_linear",
                    last_sequence=last_parser_sequence,
                )
                if len(events) != 1:
                    raise BybitL2WriterError("fixture writer accepts one normalized event per message")
                event = events[0]
                exchange_ts = (
                    event.exchange_ts_ms / 1000.0 if event.exchange_ts_ms is not None else None
                )
                raw_row = _sealed(
                    {
                        "schema": RAW_SCHEMA,
                        "capture_id": spec.capture_id,
                        "event_lineage_hash": spec.event_lineage_hash,
                        "record_sequence": record_sequence,
                        "connection_epoch": record.connection_epoch,
                        "received_ts": record.received_ts,
                        "monotonic_ns": record.monotonic_ns,
                        "exchange_ts": exchange_ts,
                        "channel": event.channel,
                        "kind": event.kind,
                        "action": event.action,
                        "venue_sequence": event.sequence_end,
                        "gap_signal": event.gap_signal,
                        "raw_payload_b64": base64.b64encode(raw_payload).decode("ascii"),
                        "raw_payload_sha256": raw_sha256,
                        "previous_record_hash": last_record_hash,
                    },
                    "record_hash",
                )
                _write_line(raw_handle, raw_row)
                last_record_hash = raw_row["record_hash"]

                if event.kind != "book":
                    continue
                if event.sequence_end is not None and (
                    last_parser_sequence is None or event.sequence_end > last_parser_sequence
                ):
                    last_parser_sequence = event.sequence_end

                if event.action != "snapshot" or event.gap_signal != GAP_RESET:
                    signal = event.gap_signal if event.gap_signal != GAP_NONE else "PREDECESSOR_MISSING"
                    gap = {
                        "record_sequence": record_sequence,
                        "connection_epoch": record.connection_epoch,
                        "venue_sequence": event.sequence_end,
                        "gap_signal": signal,
                    }
                    gaps.append(gap)
                    current_epoch_row["gap_count"] += 1
                    continue

                frame = _normalized_frame(
                    event,
                    record,
                    raw_sha256=raw_sha256,
                    previous_sequence=None,
                )
                decision = book.apply(frame)
                if decision.status is not ApplyStatus.APPLIED_SNAPSHOT:
                    gaps.append(
                        {
                            "record_sequence": record_sequence,
                            "connection_epoch": record.connection_epoch,
                            "venue_sequence": event.sequence_end,
                            "gap_signal": decision.status.value,
                        }
                    )
                    current_epoch_row["gap_count"] += 1
                    continue
                snapshot = book.causal_snapshot()
                if snapshot is None:
                    raise BybitL2WriterError("accepted snapshot produced no causal depth")
                depth_row = _depth_row(
                    spec=spec,
                    record_sequence=record_sequence,
                    source_record_hash=raw_row["record_hash"],
                    snapshot=snapshot,
                    previous_row_hash=last_depth_hash,
                )
                _write_line(depth_handle, depth_row)
                last_depth_hash = depth_row["row_hash"]
                depth_rows += 1
                current_epoch_row["depth_rows"] += 1
                current_epoch_row["snapshot_seen"] = True

            raw_handle.flush()
            os.fsync(raw_handle.fileno())
            depth_handle.flush()
            os.fsync(depth_handle.fileno())
    except BaseException:
        raise

    raw_bytes = raw_path.read_bytes()
    depth_bytes = depth_path.read_bytes()
    all_epochs_complete = bool(epoch_rows) and all(
        row["closed"] is True and row["snapshot_seen"] is True for row in epoch_rows
    )
    structurally_complete = bool(
        termination_reason in {"source_exhausted", "window_complete"}
        and depth_rows > 0
        and not gaps
        and all_epochs_complete
        and current_epoch is None
    )
    status = "COMPLETED" if structurally_complete else "STOPPED_INCOMPLETE"
    manifest = _sealed(
        {
            "schema": MANIFEST_SCHEMA,
            "capture_id": spec.capture_id,
            "venue": "bybit",
            "contract_id": spec.contract_id,
            "event_lineage_hash": spec.event_lineage_hash,
            "status": status,
            "completion_scope": "FIXTURE_L2_TAPE_ONLY",
            "termination_reason": termination_reason,
            "evidence_class": (
                "SYNTHETIC_OFFLINE_ONLY" if structurally_complete else "DESCRIPTIVE_ONLY"
            ),
            "acceptance_capable": False,
            # A complete tape is still not an execution bundle: this writer does not
            # collect the fixed-offset cost, funding or mark/index evidence required
            # by l2_evidence.  Promotion happens only in a separate authority-bound
            # package and can never be inferred from structural tape completion.
            "execution_bundle_ready": False,
            "replay_ready": False,
            "gap_free": not gaps,
            "records_written": record_sequence,
            "depth_rows_written": depth_rows,
            "message_budget": spec.max_messages,
            "runtime_budget_sec": spec.max_runtime_sec,
            "window_start_ts": spec.window_start_ts,
            "window_end_ts": spec.window_end_ts,
            "epochs": epoch_rows,
            "gaps": gaps,
            "file_sha256": {
                "raw-frames.jsonl": hashlib.sha256(raw_bytes).hexdigest(),
                "normalized-depth.jsonl": hashlib.sha256(depth_bytes).hexdigest(),
            },
            **_RESEARCH_ONLY,
        },
        "manifest_hash",
    )
    manifest_raw = _write_json_exclusive(target / "manifest.json", manifest)
    if (
        hashlib.sha256(raw_path.read_bytes()).hexdigest()
        != manifest["file_sha256"]["raw-frames.jsonl"]
        or hashlib.sha256(depth_path.read_bytes()).hexdigest()
        != manifest["file_sha256"]["normalized-depth.jsonl"]
    ):
        raise BybitL2WriterError("evidence tape changed before terminal receipt")
    receipt = _sealed(
        {
            "schema": RECEIPT_SCHEMA,
            "capture_id": spec.capture_id,
            "status": status,
            "evidence_class": manifest["evidence_class"],
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "manifest_hash": manifest["manifest_hash"],
            "raw_frames_sha256": manifest["file_sha256"]["raw-frames.jsonl"],
            "normalized_depth_sha256": manifest["file_sha256"]["normalized-depth.jsonl"],
            **_RESEARCH_ONLY,
        },
        "receipt_hash",
    )
    _write_json_exclusive(target / "terminal-receipt.json", receipt)
    return manifest
