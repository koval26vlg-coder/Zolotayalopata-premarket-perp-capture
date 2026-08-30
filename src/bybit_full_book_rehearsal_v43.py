"""Strictly offline, unbound rehearsal for Bybit's full order book protocol.

Only exact in-memory transcript records are accepted.  The module has no network,
PlanOnly, claim, scheduling, or order surface.  It writes a create-only synthetic
evidence bundle under a caller-provided temporary directory and never promotes a
synchronized book to replay, execution, acceptance, or capture authority.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from bybit_full_book_v43 import (
    FULL_ORDERBOOK_REST_CATEGORY,
    FULL_ORDERBOOK_REST_PATH,
    FULL_ORDERBOOK_WS_CATEGORY,
    FULL_ORDERBOOK_WS_PATH,
    MAX_LEVELS_PER_SIDE,
    REST_REQUEST_PROVENANCE,
    RPI_COVERAGE,
    WS_CONNECTION_PROVENANCE,
    BybitFullBookDelta,
    BybitFullBookRestAttempt,
    BybitFullBookRestSnapshot,
    BybitFullBookSynchronizer,
)
from canonical_hash import canonical_json_bytes
import project_config as config


RAW_SCHEMA = "premarket_perp_bybit_full_book_raw_ingress_v43"
DECISION_SCHEMA = "premarket_perp_bybit_full_book_sync_decision_v43"
DEPTH_SCHEMA = "premarket_perp_bybit_full_book_normalized_depth_v43"
MANIFEST_SCHEMA = "premarket_perp_bybit_full_book_rehearsal_manifest_v43"
RECEIPT_SCHEMA = "premarket_perp_bybit_full_book_rehearsal_terminal_receipt_v43"
COMPLETION_SCOPE = "OFFLINE_UNBOUND_FULL_BOOK_SYNC_V43"
MAX_TRANSCRIPT_RECORDS = 1_000_000
MAX_RAW_BYTES = 32_000_000

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SYMBOL = re.compile(r"^[A-Z0-9]{2,40}$")
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CONTROL_ROOT = Path("C:/Users/koval/Documents/ZolotyayLopata")
_DEFAULT_CAPTURE_ROOT = Path("E:/trading_mvp/premarket-perp-capture/captures")

_NO_AUTHORITY = {
    "network_used": False,
    "orders_created": 0,
    "private_api_used": False,
    "live_execution": False,
    "claim_used": False,
    "capture_token_used": False,
    "plan_activated": False,
    "replay_ready": False,
    "execution_bundle_ready": False,
    "acceptance_capable": False,
}


class FullBookRehearsalError(RuntimeError):
    """The fixture transcript or output violates the offline rehearsal contract."""


def _positive_int(value: object, field: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise FullBookRehearsalError(f"{field} must be between 1 and {maximum}")
    return value


def _clock(value: object, field: str, *, integer: bool) -> int | float:
    if integer:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise FullBookRehearsalError(f"{field} must be a positive integer")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FullBookRehearsalError(f"{field} must be a finite positive number")
    rendered = float(value)
    if not math.isfinite(rendered) or rendered <= 0:
        raise FullBookRehearsalError(f"{field} must be a finite positive number")
    return rendered


@dataclass(frozen=True)
class FullBookRehearsalSpec:
    rehearsal_id: str
    contract_id: str
    max_levels_per_side: int
    max_buffered_deltas: int

    def __post_init__(self) -> None:
        if not isinstance(self.rehearsal_id, str) or _SAFE_ID.fullmatch(self.rehearsal_id) is None:
            raise FullBookRehearsalError("rehearsal_id must be one safe path component")
        if not isinstance(self.contract_id, str) or _SYMBOL.fullmatch(self.contract_id) is None:
            raise FullBookRehearsalError("contract_id must be one canonical Bybit symbol")
        _positive_int(
            self.max_levels_per_side,
            "max_levels_per_side",
            maximum=MAX_LEVELS_PER_SIDE,
        )
        _positive_int(
            self.max_buffered_deltas,
            "max_buffered_deltas",
            maximum=MAX_TRANSCRIPT_RECORDS,
        )


@dataclass(frozen=True)
class FullBookTranscriptRecord:
    kind: str
    transport_epoch: int
    raw_payload: bytes = b""
    received_ts: float | None = None
    monotonic_ns: int | None = None
    close_reason: str | None = None

    def __post_init__(self) -> None:
        kind = str(self.kind).upper()
        if kind not in {"OPEN", "WS_DELTA", "REST_ATTEMPT", "REST_SNAPSHOT", "CLOSE"}:
            raise FullBookRehearsalError("unknown full-book transcript record kind")
        epoch = _positive_int(self.transport_epoch, "transport_epoch", maximum=2**63 - 1)
        if type(self.raw_payload) is not bytes:
            raise FullBookRehearsalError("raw_payload must be exact immutable bytes")
        raw = self.raw_payload
        if kind in {"WS_DELTA", "REST_SNAPSHOT"}:
            if not raw or len(raw) > MAX_RAW_BYTES:
                raise FullBookRehearsalError("raw payload is empty or exceeds its bound")
            received = _clock(self.received_ts, "received_ts", integer=False)
            monotonic = _clock(self.monotonic_ns, "monotonic_ns", integer=True)
            if self.close_reason is not None:
                raise FullBookRehearsalError(f"{kind} cannot carry close_reason")
            object.__setattr__(self, "received_ts", received)
            object.__setattr__(self, "monotonic_ns", monotonic)
        elif kind == "REST_ATTEMPT":
            if raw or self.close_reason is not None:
                raise FullBookRehearsalError("REST_ATTEMPT cannot carry raw bytes or close_reason")
            object.__setattr__(
                self, "received_ts", _clock(self.received_ts, "received_ts", integer=False)
            )
            object.__setattr__(
                self, "monotonic_ns", _clock(self.monotonic_ns, "monotonic_ns", integer=True)
            )
        else:
            if raw or self.received_ts is not None or self.monotonic_ns is not None:
                raise FullBookRehearsalError(f"{kind} cannot carry payload clocks")
            if kind == "OPEN" and self.close_reason is not None:
                raise FullBookRehearsalError("OPEN cannot carry close_reason")
            if kind == "CLOSE" and (
                not isinstance(self.close_reason, str)
                or not self.close_reason
                or len(self.close_reason) > 256
            ):
                raise FullBookRehearsalError("CLOSE must carry a bounded non-empty reason")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "transport_epoch", epoch)

    @classmethod
    def open(cls, transport_epoch: int) -> "FullBookTranscriptRecord":
        return cls(kind="OPEN", transport_epoch=transport_epoch)

    @classmethod
    def ws_delta(
        cls,
        transport_epoch: int,
        raw_payload: bytes,
        *,
        received_ts: float,
        monotonic_ns: int,
    ) -> "FullBookTranscriptRecord":
        return cls(
            kind="WS_DELTA",
            transport_epoch=transport_epoch,
            raw_payload=raw_payload,
            received_ts=received_ts,
            monotonic_ns=monotonic_ns,
        )

    @classmethod
    def rest_attempt(
        cls,
        transport_epoch: int,
        *,
        received_ts: float,
        monotonic_ns: int,
    ) -> "FullBookTranscriptRecord":
        return cls(
            kind="REST_ATTEMPT",
            transport_epoch=transport_epoch,
            received_ts=received_ts,
            monotonic_ns=monotonic_ns,
        )

    @classmethod
    def rest_snapshot(
        cls,
        transport_epoch: int,
        raw_payload: bytes,
        *,
        received_ts: float,
        monotonic_ns: int,
    ) -> "FullBookTranscriptRecord":
        return cls(
            kind="REST_SNAPSHOT",
            transport_epoch=transport_epoch,
            raw_payload=raw_payload,
            received_ts=received_ts,
            monotonic_ns=monotonic_ns,
        )

    @classmethod
    def close(cls, transport_epoch: int, *, reason: str) -> "FullBookTranscriptRecord":
        return cls(kind="CLOSE", transport_epoch=transport_epoch, close_reason=reason)


class StaticBybitFullBookTranscript:
    """Bounded exact in-memory transcript with no transport capability."""

    __slots__ = ("_records", "_index")

    def __init__(self, records: Sequence[FullBookTranscriptRecord]) -> None:
        values = tuple(records)
        if len(values) > MAX_TRANSCRIPT_RECORDS:
            raise FullBookRehearsalError("transcript exceeds its record ceiling")
        if not all(type(row) is FullBookTranscriptRecord for row in values):
            raise FullBookRehearsalError("transcript accepts only exact record objects")
        self._records = tuple(_clone_transcript_record(row) for row in values)
        self._index = 0

    @property
    def consumed_count(self) -> int:
        return self._index

    def next_record(self) -> FullBookTranscriptRecord | None:
        if self._index >= len(self._records):
            return None
        row = self._records[self._index]
        self._index += 1
        return _clone_transcript_record(row)


def _clone_transcript_record(record: FullBookTranscriptRecord) -> FullBookTranscriptRecord:
    return FullBookTranscriptRecord(
        kind=record.kind,
        transport_epoch=record.transport_epoch,
        raw_payload=record.raw_payload,
        received_ts=record.received_ts,
        monotonic_ns=record.monotonic_ns,
        close_reason=record.close_reason,
    )


def _hash_claim(payload: Mapping[str, Any], field: str) -> str:
    material = dict(payload)
    material.pop(field, None)
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def _sealed(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    row = dict(payload)
    row[field] = _hash_claim(row, field)
    return row


def _append_durable(handle: Any, payload: Mapping[str, Any]) -> bytes:
    raw = canonical_json_bytes(dict(payload)) + b"\n"
    handle.write(raw)
    handle.flush()
    os.fsync(handle.fileno())
    return raw


def _stat_identity(stat: os.stat_result) -> tuple[int, int]:
    return int(stat.st_dev), int(stat.st_ino)


def _handle_identity(handle: Any) -> tuple[int, int]:
    try:
        return _stat_identity(os.fstat(handle.fileno()))
    except (OSError, ValueError) as exc:
        raise FullBookRehearsalError("cannot read owned file identity") from exc


def _plain_file_identity(path: Path) -> tuple[int, int]:
    if not path.is_file() or _is_link_or_junction(path):
        raise FullBookRehearsalError("owned artifact must remain one plain file")
    try:
        return _stat_identity(os.lstat(path))
    except OSError as exc:
        raise FullBookRehearsalError("cannot read owned artifact identity") from exc


def _assert_plain_file_identity(path: Path, expected: tuple[int, int]) -> None:
    if _plain_file_identity(path) != expected:
        raise FullBookRehearsalError(f"owned artifact identity changed: {path.name}")


def _read_owned_handle(
    handle: Any,
    path: Path,
    expected: tuple[int, int],
) -> bytes:
    if _handle_identity(handle) != expected:
        raise FullBookRehearsalError(f"owned handle identity changed: {path.name}")
    _assert_plain_file_identity(path, expected)
    try:
        handle.seek(0)
        raw = handle.read()
        handle.seek(0, os.SEEK_END)
    except (OSError, ValueError) as exc:
        raise FullBookRehearsalError(f"cannot read owned artifact: {path.name}") from exc
    if type(raw) is not bytes:
        raise FullBookRehearsalError(f"owned artifact read was not bytes: {path.name}")
    _assert_plain_file_identity(path, expected)
    return raw


def _write_exclusive(
    path: Path,
    payload: Mapping[str, Any],
    *,
    parent_identity: tuple[int, int],
) -> tuple[bytes, tuple[int, int]]:
    _assert_plain_directory_identity(path.parent, parent_identity)
    raw = canonical_json_bytes(dict(payload)) + b"\n"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError as exc:
        raise FullBookRehearsalError(f"create-only artifact already exists: {path.name}") from exc
    identity = _stat_identity(os.fstat(descriptor))
    try:
        handle = os.fdopen(descriptor, "w+b")
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    try:
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
            if _read_owned_handle(handle, path, identity) != raw:
                raise FullBookRehearsalError(f"exact handle readback failed: {path.name}")
    except BaseException:
        raise
    _assert_plain_directory_identity(path.parent, parent_identity)
    _assert_plain_file_identity(path, identity)
    return raw, identity


def _verify_jsonl_chain(
    raw: bytes,
    *,
    label: str,
    previous_field: str,
    hash_field: str,
    expected_count: int,
    expected_head: str | None,
) -> None:
    lines = raw.splitlines(keepends=True)
    if len(lines) != expected_count:
        raise FullBookRehearsalError(f"{label} row count does not match semantic state")
    previous: str | None = None
    for index, line in enumerate(lines, start=1):
        if not line.endswith(b"\n") or line.endswith(b"\r\n"):
            raise FullBookRehearsalError(f"{label} row {index} is not canonical JSONL")
        try:
            row = json.loads(line[:-1].decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FullBookRehearsalError(f"{label} row {index} is not strict JSON") from exc
        if not isinstance(row, dict) or canonical_json_bytes(row) + b"\n" != line:
            raise FullBookRehearsalError(f"{label} row {index} is not canonical")
        if row.get(previous_field) != previous:
            raise FullBookRehearsalError(f"{label} row {index} breaks its previous-hash chain")
        claimed = row.get(hash_field)
        if not isinstance(claimed, str) or claimed != _hash_claim(row, hash_field):
            raise FullBookRehearsalError(f"{label} row {index} has an invalid hash claim")
        previous = claimed
    if previous != expected_head:
        raise FullBookRehearsalError(f"{label} chain head does not match semantic state")


def _resolved(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve(strict=False)))
    except OSError as exc:
        raise FullBookRehearsalError("cannot resolve output path") from exc


def _paths_overlap(left: Path, right: Path) -> bool:
    left_text = _resolved(left)
    right_text = _resolved(right)
    left_drive, _ = os.path.splitdrive(left_text)
    right_drive, _ = os.path.splitdrive(right_text)
    if left_drive and right_drive and left_drive != right_drive:
        return False
    try:
        common = os.path.normcase(os.path.commonpath((left_text, right_text)))
    except ValueError as exc:
        raise FullBookRehearsalError("cannot prove path separation") from exc
    return common in {left_text, right_text}


def _is_below(path: Path, root: Path) -> bool:
    path_text = _resolved(path)
    root_text = _resolved(root)
    path_drive, _ = os.path.splitdrive(path_text)
    root_drive, _ = os.path.splitdrive(root_text)
    if path_drive and root_drive and path_drive != root_drive:
        return False
    try:
        return os.path.normcase(os.path.commonpath((path_text, root_text))) == root_text
    except ValueError:
        return False


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    try:
        return path.is_symlink() or bool(is_junction and is_junction(path))
    except OSError as exc:
        raise FullBookRehearsalError("cannot inspect output parent identity") from exc


def _plain_directory_identity(path: Path) -> tuple[int, int]:
    if not path.is_dir() or _is_link_or_junction(path):
        raise FullBookRehearsalError("output directory must remain one plain directory")
    try:
        stat = os.lstat(path)
    except OSError as exc:
        raise FullBookRehearsalError("cannot read output directory identity") from exc
    return int(stat.st_dev), int(stat.st_ino)


def _assert_plain_directory_identity(
    path: Path,
    expected: tuple[int, int],
) -> None:
    if _plain_directory_identity(path) != expected:
        raise FullBookRehearsalError("output directory identity changed during rehearsal")


class _PinnedPlainDirectory:
    """Keep a verified parent from being renamed while its child is created."""

    __slots__ = ("path", "_fd", "_handle", "_kernel32")

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None
        self._handle: int | None = None
        self._kernel32: Any | None = None

    def __enter__(self) -> "_PinnedPlainDirectory":
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateFileW.argtypes = (
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            )
            kernel32.CreateFileW.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.CreateFileW(
                str(self.path),
                0x80000000,  # GENERIC_READ
                0x00000001 | 0x00000002,  # share read/write, deliberately not delete
                None,
                3,  # OPEN_EXISTING
                0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
                None,
            )
            if handle == ctypes.c_void_p(-1).value:
                error = ctypes.get_last_error()
                raise FullBookRehearsalError(
                    f"cannot pin output parent directory: winerror {error}"
                )
            self._handle = int(handle)
            self._kernel32 = kernel32
        else:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            try:
                self._fd = os.open(self.path, flags)
            except OSError as exc:
                raise FullBookRehearsalError("cannot pin output parent directory") from exc
        return self

    def create_child(self, name: str) -> None:
        if os.name == "nt":
            (self.path / name).mkdir(exist_ok=False)
            return
        if self._fd is None:
            raise FullBookRehearsalError("output parent pin is unavailable")
        os.mkdir(name, dir_fd=self._fd)

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._handle is not None and self._kernel32 is not None:
            self._kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None
            self._kernel32 = None


class _PinnedEvidenceReader:
    """Read one owned file while denying write/delete sharing on Windows."""

    __slots__ = ("path", "identity", "_handle")

    def __init__(self, path: Path, expected_identity: tuple[int, int]) -> None:
        self.path = path
        self._handle: Any | None = None
        if os.name == "nt":
            import msvcrt

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateFileW.argtypes = (
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            )
            kernel32.CreateFileW.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.CloseHandle.restype = ctypes.c_int
            native = kernel32.CreateFileW(
                str(path),
                0x80000000,  # GENERIC_READ
                0x00000001,  # FILE_SHARE_READ only
                None,
                3,  # OPEN_EXISTING
                0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
                None,
            )
            if native == ctypes.c_void_p(-1).value:
                error = ctypes.get_last_error()
                raise FullBookRehearsalError(
                    f"cannot pin owned evidence file: {path.name}; winerror {error}"
                )
            descriptor: int | None = None
            try:
                descriptor = msvcrt.open_osfhandle(int(native), os.O_RDONLY)
                self._handle = os.fdopen(descriptor, "rb")
            except BaseException:
                if descriptor is None:
                    kernel32.CloseHandle(ctypes.c_void_p(native))
                else:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                raise
        else:
            self._handle = path.open("rb")
        try:
            self.identity = _handle_identity(self._handle)
            if self.identity != expected_identity:
                raise FullBookRehearsalError(
                    f"owned evidence changed before pin: {path.name}"
                )
            _assert_plain_file_identity(path, expected_identity)
        except BaseException:
            self.close()
            raise

    def read_bytes(self) -> bytes:
        if self._handle is None:
            raise FullBookRehearsalError("owned evidence pin is closed")
        return _read_owned_handle(self._handle, self.path, self.identity)

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


@contextmanager
def _new_external_temp_dir(output_dir: Path):
    supplied = Path(output_dir)
    if supplied.exists():
        raise FullBookRehearsalError("output directory must be new and not already exist")
    target = supplied.resolve(strict=False)
    protected = (
        _REPOSITORY_ROOT,
        _DEFAULT_CONTROL_ROOT,
        _DEFAULT_CAPTURE_ROOT,
        config.CONTROL_ROOT,
        config.CAPTURE_ROOT,
    )
    if any(_paths_overlap(target, root) for root in protected):
        raise FullBookRehearsalError(
            "output path overlaps the repository or protected production/state roots"
        )
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    if not _is_below(target, temp_root):
        raise FullBookRehearsalError("output must be below the system temporary root")
    parent = target.parent
    parent_identity = _plain_directory_identity(parent)
    expected_target = os.path.normcase(str(target))
    with _PinnedPlainDirectory(parent) as pin:
        try:
            _assert_plain_directory_identity(parent, parent_identity)
            pin.create_child(target.name)
            created_identity = _plain_directory_identity(target)
            _assert_plain_directory_identity(parent, parent_identity)
            if _resolved(target) != expected_target:
                raise FullBookRehearsalError(
                    "created output directory escaped its prevalidated parent"
                )
        except FileExistsError as exc:
            raise FullBookRehearsalError("output directory must be new") from exc
        except OSError as exc:
            raise FullBookRehearsalError("output parent changed during directory creation") from exc
        except BaseException:
            # Failure cleanup is intentionally non-destructive.  The create-new
            # temp directory is retained as a forensic incomplete artifact; a
            # path-based unlink/rmdir cannot be made atomic against replacement.
            raise
        with _PinnedPlainDirectory(target):
            _assert_plain_directory_identity(parent, parent_identity)
            _assert_plain_directory_identity(target, created_identity)
            yield target


def _raw_row(
    *,
    spec: FullBookRehearsalSpec,
    sequence: int,
    record: FullBookTranscriptRecord,
    generation: int,
    previous_hash: str | None,
) -> dict[str, Any]:
    raw = record.raw_payload
    return _sealed(
        {
            "schema": RAW_SCHEMA,
            "rehearsal_id": spec.rehearsal_id,
            "contract_id": spec.contract_id,
            "record_sequence": sequence,
            "record_kind": record.kind,
            "transport_epoch": record.transport_epoch,
            "book_generation": generation,
            "received_ts": record.received_ts,
            "monotonic_ns": record.monotonic_ns,
            "close_reason": record.close_reason,
            "raw_payload_b64": base64.b64encode(raw).decode("ascii") if raw else None,
            "raw_payload_sha256": hashlib.sha256(raw).hexdigest() if raw else None,
            "request_path": (
                FULL_ORDERBOOK_REST_PATH if record.kind == "REST_ATTEMPT" else None
            ),
            "request_category": (
                FULL_ORDERBOOK_REST_CATEGORY if record.kind == "REST_ATTEMPT" else None
            ),
            "request_provenance": (
                REST_REQUEST_PROVENANCE if record.kind == "REST_ATTEMPT" else None
            ),
            "connection_path": (
                FULL_ORDERBOOK_WS_PATH
                if record.kind in {"OPEN", "WS_DELTA", "CLOSE"}
                else None
            ),
            "connection_category": (
                FULL_ORDERBOOK_WS_CATEGORY
                if record.kind in {"OPEN", "WS_DELTA", "CLOSE"}
                else None
            ),
            "connection_provenance": (
                WS_CONNECTION_PROVENANCE
                if record.kind in {"OPEN", "WS_DELTA", "CLOSE"}
                else None
            ),
            "previous_record_hash": previous_hash,
        },
        "record_hash",
    )


def _snapshot_dict(snapshot: Any) -> dict[str, Any]:
    return {
        "transport_epoch": snapshot.epoch,
        "book_generation": snapshot.generation,
        "sync_status": snapshot.status,
        "synchronized": snapshot.synchronized,
        "resync_required": snapshot.resync_required,
        "invalidation_reason": snapshot.invalidation_reason,
        "continuity_basis": snapshot.continuity_basis,
        "known_depth_limit_per_side": snapshot.known_depth_limit_per_side,
        "known_depth_limitation": snapshot.known_depth_limitation,
        "rpi_coverage": snapshot.rpi_coverage,
        "connection_path": snapshot.connection_path,
        "connection_category": snapshot.connection_category,
        "connection_provenance": snapshot.connection_provenance,
        "update_id": snapshot.update_id,
        "cross_sequence": snapshot.cross_sequence,
        "exchange_ts_ms": snapshot.exchange_ts_ms,
        "received_ts": snapshot.received_ts,
        "monotonic_ns": snapshot.monotonic_ns,
        "bids": [list(level) for level in snapshot.bids],
        "asks": [list(level) for level in snapshot.asks],
        "evidence_chain_sha256": snapshot.evidence_chain_sha256,
        "evidence_record_count": snapshot.evidence_record_count,
        "book_structurally_ready": snapshot.book_structurally_ready,
        "book_execution_ready": snapshot.execution_ready,
    }


def _decision_row(
    *,
    spec: FullBookRehearsalSpec,
    sequence: int,
    record: FullBookTranscriptRecord,
    decision: str,
    snapshot: Any,
    previous_hash: str | None,
    error: Exception | None = None,
) -> dict[str, Any]:
    return _sealed(
        {
            "schema": DECISION_SCHEMA,
            "rehearsal_id": spec.rehearsal_id,
            "contract_id": spec.contract_id,
            "decision_sequence": sequence,
            "source_record_sequence": sequence,
            "record_kind": record.kind,
            "decision": decision,
            **_snapshot_dict(snapshot),
            "error_class": type(error).__name__ if error is not None else None,
            "error_message": str(error) if error is not None else None,
            "previous_decision_hash": previous_hash,
        },
        "decision_hash",
    )


def _depth_row(
    *,
    spec: FullBookRehearsalSpec,
    sequence: int,
    source_record_hash: str,
    snapshot: Any,
    previous_hash: str | None,
) -> dict[str, Any]:
    return _sealed(
        {
            "schema": DEPTH_SCHEMA,
            "rehearsal_id": spec.rehearsal_id,
            "contract_id": spec.contract_id,
            "depth_sequence": sequence,
            "source_record_sequence": sequence,
            "source_record_hash": source_record_hash,
            **_snapshot_dict(snapshot),
            "previous_depth_hash": previous_hash,
        },
        "depth_hash",
    )


def run_unbound_bybit_full_book_rehearsal(
    spec: FullBookRehearsalSpec,
    *,
    output_dir: Path,
    transcript: StaticBybitFullBookTranscript,
) -> dict[str, Any]:
    """Rehearse exact Full OB synchronization from a static fixture transcript."""

    if type(spec) is not FullBookRehearsalSpec:
        raise FullBookRehearsalError("spec must be exact FullBookRehearsalSpec")
    if type(transcript) is not StaticBybitFullBookTranscript:
        raise FullBookRehearsalError("transcript must be exact StaticBybitFullBookTranscript")
    spec = FullBookRehearsalSpec(
        rehearsal_id=spec.rehearsal_id,
        contract_id=spec.contract_id,
        max_levels_per_side=spec.max_levels_per_side,
        max_buffered_deltas=spec.max_buffered_deltas,
    )
    with _new_external_temp_dir(Path(output_dir)) as target:
        return _run_unbound_bybit_full_book_rehearsal_in_target(
            spec,
            target=target,
            transcript=transcript,
        )


def _run_unbound_bybit_full_book_rehearsal_in_target(
    spec: FullBookRehearsalSpec,
    *,
    target: Path,
    transcript: StaticBybitFullBookTranscript,
) -> dict[str, Any]:
    target_identity = _plain_directory_identity(target)
    raw_path = target / "raw-ingress.jsonl"
    decision_path = target / "sync-decisions.jsonl"
    depth_path = target / "normalized-depth.jsonl"
    opened_handles: list[Any] = []
    owned_files: list[tuple[Path, tuple[int, int]]] = []
    try:
        _assert_plain_directory_identity(target, target_identity)
        raw_handle = raw_path.open("x+b")
        opened_handles.append(raw_handle)
        raw_identity = _handle_identity(raw_handle)
        owned_files.append((raw_path, raw_identity))
        _assert_plain_directory_identity(target, target_identity)
        decision_handle = decision_path.open("x+b")
        opened_handles.append(decision_handle)
        decision_identity = _handle_identity(decision_handle)
        owned_files.append((decision_path, decision_identity))
        _assert_plain_directory_identity(target, target_identity)
        depth_handle = depth_path.open("x+b")
        opened_handles.append(depth_handle)
        depth_identity = _handle_identity(depth_handle)
        owned_files.append((depth_path, depth_identity))
        _assert_plain_directory_identity(target, target_identity)
    except BaseException:
        for handle in reversed(opened_handles):
            try:
                handle.close()
            except OSError:
                pass
        # Retain all create-new temp artifacts.  Deleting by pathname after an
        # identity check would introduce a check/unlink replacement race.
        raise

    synchronizer = BybitFullBookSynchronizer(
        symbol=spec.contract_id,
        max_levels_per_side=spec.max_levels_per_side,
        max_buffered_deltas=spec.max_buffered_deltas,
    )
    record_sequence = 0
    depth_count = 0
    previous_raw_hash: str | None = None
    previous_decision_hash: str | None = None
    previous_depth_hash: str | None = None
    active_epoch: int | None = None
    last_epoch = 0
    reconnect_seen = False
    rest_attempt: BybitFullBookRestAttempt | None = None
    error: Exception | None = None
    termination_reason = "source_exhausted"

    with raw_handle, decision_handle, depth_handle:
        while True:
            record = transcript.next_record()
            if record is None:
                break
            record_sequence += 1
            try:
                generation_before = synchronizer.causal_snapshot().generation
                raw_generation = (
                    generation_before + 1 if record.kind == "OPEN" else generation_before
                )
                raw_row = _raw_row(
                    spec=spec,
                    sequence=record_sequence,
                    record=record,
                    generation=raw_generation,
                    previous_hash=previous_raw_hash,
                )
                _append_durable(raw_handle, raw_row)
                previous_raw_hash = raw_row["record_hash"]

                if record.kind == "OPEN":
                    if active_epoch is not None or record.transport_epoch <= last_epoch:
                        raise FullBookRehearsalError(
                            "OPEN requires no active epoch and a strictly advancing epoch"
                        )
                    if last_epoch == 0:
                        synchronizer.begin_epoch(record.transport_epoch)
                    else:
                        synchronizer.reconnect(record.transport_epoch)
                        reconnect_seen = True
                    active_epoch = record.transport_epoch
                    last_epoch = active_epoch

                decision = "OBSERVED"
                if record.kind == "OPEN":
                    decision = "EPOCH_OPENED"
                elif record.kind == "CLOSE":
                    if active_epoch != record.transport_epoch:
                        raise FullBookRehearsalError("CLOSE does not match the active epoch")
                    active_epoch = None
                    rest_attempt = None
                    decision = "EPOCH_CLOSED"
                elif active_epoch != record.transport_epoch:
                    raise FullBookRehearsalError("data record does not match the active epoch")
                elif record.kind == "REST_ATTEMPT":
                    rest_attempt = synchronizer.issue_rest_attempt(
                        epoch=record.transport_epoch,
                        issued_received_ts=float(record.received_ts),
                        issued_monotonic_ns=int(record.monotonic_ns),
                    )
                    decision = "REST_ATTEMPT_ISSUED"
                elif record.kind == "WS_DELTA":
                    delta = BybitFullBookDelta.from_raw(
                        record.raw_payload,
                        float(record.received_ts),
                        int(record.monotonic_ns),
                        spec.contract_id,
                        connection_path=FULL_ORDERBOOK_WS_PATH,
                        connection_category=FULL_ORDERBOOK_WS_CATEGORY,
                    )
                    decision = synchronizer.ingest_delta(
                        delta, epoch=record.transport_epoch
                    )
                elif record.kind == "REST_SNAPSHOT":
                    if rest_attempt is None:
                        raise FullBookRehearsalError(
                            "REST_SNAPSHOT requires the outstanding transcript attempt"
                        )
                    snapshot_input = BybitFullBookRestSnapshot.from_raw(
                        record.raw_payload,
                        float(record.received_ts),
                        int(record.monotonic_ns),
                        spec.contract_id,
                        request_path=rest_attempt.request_path,
                        request_category=rest_attempt.request_category,
                    )
                    anchored = synchronizer.ingest_rest_snapshot(
                        snapshot_input,
                        epoch=record.transport_epoch,
                        attempt=rest_attempt,
                    )
                    rest_attempt = None
                    decision = "SYNCHRONIZED" if anchored else "SNAPSHOT_PENDING_OR_RETRY"

                snapshot = synchronizer.causal_snapshot()
                decision_row = _decision_row(
                    spec=spec,
                    sequence=record_sequence,
                    record=record,
                    decision=decision,
                    snapshot=snapshot,
                    previous_hash=previous_decision_hash,
                )
                _append_durable(decision_handle, decision_row)
                previous_decision_hash = decision_row["decision_hash"]

                if snapshot.synchronized and decision in {"SYNCHRONIZED", "APPLIED"}:
                    depth_row = _depth_row(
                        spec=spec,
                        sequence=record_sequence,
                        source_record_hash=raw_row["record_hash"],
                        snapshot=snapshot,
                        previous_hash=previous_depth_hash,
                    )
                    _append_durable(depth_handle, depth_row)
                    previous_depth_hash = depth_row["depth_hash"]
                    depth_count += 1

                if decision == "INVALIDATED":
                    termination_reason = snapshot.invalidation_reason or "SYNC_INVALIDATED"
                    break
            except Exception as exc:
                error = exc
                termination_reason = "PARSE_OR_SYNC_ERROR"
                snapshot = synchronizer.causal_snapshot()
                error_row = _decision_row(
                    spec=spec,
                    sequence=record_sequence,
                    record=record,
                    decision="ERROR",
                    snapshot=snapshot,
                    previous_hash=previous_decision_hash,
                    error=exc,
                )
                _append_durable(decision_handle, error_row)
                previous_decision_hash = error_row["decision_hash"]
                break

        for handle in (raw_handle, decision_handle, depth_handle):
            handle.flush()
            os.fsync(handle.fileno())

        raw_bytes = _read_owned_handle(raw_handle, raw_path, raw_identity)
        decision_bytes = _read_owned_handle(
            decision_handle,
            decision_path,
            decision_identity,
        )
        depth_bytes = _read_owned_handle(depth_handle, depth_path, depth_identity)

    _assert_plain_directory_identity(target, target_identity)
    final_snapshot = synchronizer.causal_snapshot()
    success = bool(
        error is None
        and termination_reason == "source_exhausted"
        and active_epoch is None
        and depth_count > 0
        and final_snapshot.synchronized
    )
    if not success and termination_reason == "source_exhausted":
        if active_epoch is not None:
            termination_reason = "OPEN_EPOCH_AT_EOF"
        elif reconnect_seen and not final_snapshot.synchronized:
            termination_reason = "RECONNECT_REQUIRES_RESYNC"
        elif not final_snapshot.synchronized:
            termination_reason = final_snapshot.invalidation_reason or "NO_SYNCHRONIZED_BOOK"
    status = "FULL_BOOK_SYNC_ONLY" if success else "STOPPED_INCOMPLETE"

    _verify_jsonl_chain(
        raw_bytes,
        label="raw ingress",
        previous_field="previous_record_hash",
        hash_field="record_hash",
        expected_count=record_sequence,
        expected_head=previous_raw_hash,
    )
    _verify_jsonl_chain(
        decision_bytes,
        label="sync decisions",
        previous_field="previous_decision_hash",
        hash_field="decision_hash",
        expected_count=record_sequence,
        expected_head=previous_decision_hash,
    )
    _verify_jsonl_chain(
        depth_bytes,
        label="normalized depth",
        previous_field="previous_depth_hash",
        hash_field="depth_hash",
        expected_count=depth_count,
        expected_head=previous_depth_hash,
    )
    for path, identity in owned_files:
        _assert_plain_file_identity(path, identity)

    semantic_material = {
        "schema": "premarket_perp_bybit_full_book_semantic_result_v43",
        "rehearsal_id": spec.rehearsal_id,
        "contract_id": spec.contract_id,
        "status": status,
        "termination_reason": termination_reason,
        "records_written": record_sequence,
        "depth_rows_written": depth_count,
        "raw_chain_head": previous_raw_hash,
        "decision_chain_head": previous_decision_hash,
        "depth_chain_head": previous_depth_hash,
        "final_snapshot": _snapshot_dict(final_snapshot),
        "error_class": type(error).__name__ if error is not None else None,
        "error_message": str(error) if error is not None else None,
    }
    semantic_result_hash = hashlib.sha256(canonical_json_bytes(semantic_material)).hexdigest()
    manifest = _sealed(
        {
            "schema": MANIFEST_SCHEMA,
            "rehearsal_id": spec.rehearsal_id,
            "venue": "bybit",
            "contract_id": spec.contract_id,
            "status": status,
            "completion_scope": COMPLETION_SCOPE,
            "evidence_class": "SYNTHETIC_OFFLINE_ONLY" if success else "DESCRIPTIVE_ONLY",
            "termination_reason": termination_reason,
            "records_written": record_sequence,
            "depth_rows_written": depth_count,
            "final_transport_epoch": final_snapshot.epoch,
            "final_book_generation": final_snapshot.generation,
            "final_sync_status": final_snapshot.status,
            "resync_required": final_snapshot.resync_required,
            "invalidation_reason": final_snapshot.invalidation_reason,
            "raw_chain_head": previous_raw_hash,
            "decision_chain_head": previous_decision_hash,
            "depth_chain_head": previous_depth_hash,
            "semantic_result_hash": semantic_result_hash,
            "rest_request_path": FULL_ORDERBOOK_REST_PATH,
            "rest_request_category": FULL_ORDERBOOK_REST_CATEGORY,
            "rest_request_provenance": REST_REQUEST_PROVENANCE,
            "ws_connection_path": FULL_ORDERBOOK_WS_PATH,
            "ws_connection_category": FULL_ORDERBOOK_WS_CATEGORY,
            "ws_connection_provenance": WS_CONNECTION_PROVENANCE,
            "rpi_coverage": RPI_COVERAGE,
            "error_class": type(error).__name__ if error is not None else None,
            "error_message": str(error) if error is not None else None,
            "file_sha256": {
                "raw-ingress.jsonl": hashlib.sha256(raw_bytes).hexdigest(),
                "sync-decisions.jsonl": hashlib.sha256(decision_bytes).hexdigest(),
                "normalized-depth.jsonl": hashlib.sha256(depth_bytes).hexdigest(),
            },
            **_NO_AUTHORITY,
        },
        "manifest_hash",
    )
    readers: list[_PinnedEvidenceReader] = []
    manifest_reader: _PinnedEvidenceReader | None = None
    receipt_reader: _PinnedEvidenceReader | None = None
    cached_bytes = {
        "raw-ingress.jsonl": raw_bytes,
        "sync-decisions.jsonl": decision_bytes,
        "normalized-depth.jsonl": depth_bytes,
    }
    try:
        for path, identity in owned_files:
            readers.append(_PinnedEvidenceReader(path, identity))
        for reader in readers:
            if reader.read_bytes() != cached_bytes[reader.path.name]:
                raise FullBookRehearsalError(
                    f"owned evidence changed before manifest: {reader.path.name}"
                )

        manifest_path = target / "manifest.json"
        manifest_raw, manifest_identity = _write_exclusive(
            manifest_path,
            manifest,
            parent_identity=target_identity,
        )
        manifest_reader = _PinnedEvidenceReader(manifest_path, manifest_identity)
        if manifest_reader.read_bytes() != manifest_raw:
            raise FullBookRehearsalError("manifest changed before it was pinned")

        for name, expected in manifest["file_sha256"].items():
            _assert_plain_directory_identity(target, target_identity)
            reader = next((item for item in readers if item.path.name == name), None)
            if reader is None:
                raise FullBookRehearsalError(f"manifest names unknown evidence: {name}")
            actual_bytes = reader.read_bytes()
            if hashlib.sha256(actual_bytes).hexdigest() != expected:
                raise FullBookRehearsalError(
                    f"evidence changed before terminal receipt: {name}"
                )
        if manifest_reader.read_bytes() != manifest_raw:
            raise FullBookRehearsalError("manifest changed before terminal receipt")

        receipt = _sealed(
            {
                "schema": RECEIPT_SCHEMA,
                "rehearsal_id": spec.rehearsal_id,
                "contract_id": spec.contract_id,
                "status": status,
                "completion_scope": COMPLETION_SCOPE,
                "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                "manifest_hash": manifest["manifest_hash"],
                "semantic_result_hash": semantic_result_hash,
                "file_sha256": manifest["file_sha256"],
                **_NO_AUTHORITY,
            },
            "receipt_hash",
        )
        receipt_path = target / "terminal-receipt.json"
        receipt_raw, receipt_identity = _write_exclusive(
            receipt_path,
            receipt,
            parent_identity=target_identity,
        )
        receipt_reader = _PinnedEvidenceReader(receipt_path, receipt_identity)
        if receipt_reader.read_bytes() != receipt_raw:
            raise FullBookRehearsalError("terminal receipt exact readback failed")
        for reader in readers:
            if reader.read_bytes() != cached_bytes[reader.path.name]:
                raise FullBookRehearsalError(
                    f"owned evidence changed before completion: {reader.path.name}"
                )
        if manifest_reader.read_bytes() != manifest_raw:
            raise FullBookRehearsalError("manifest changed before completion")
        if receipt_reader.read_bytes() != receipt_raw:
            raise FullBookRehearsalError("terminal receipt changed before completion")
        _assert_plain_directory_identity(target, target_identity)
        return manifest
    finally:
        if receipt_reader is not None:
            receipt_reader.close()
        if manifest_reader is not None:
            manifest_reader.close()
        for reader in reversed(readers):
            reader.close()
