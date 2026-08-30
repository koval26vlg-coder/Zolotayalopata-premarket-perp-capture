"""Pure offline Bybit full-order-book REST/WS synchronizer.

The module accepts already-received public payload bytes.  It deliberately has no
transport or persistence surface.  A synchronized view exists only after a REST
snapshot is bridged to the exact ``(seq, u)`` pair from the buffered full-book
stream, followed by consecutive update ids and nondecreasing cross sequences.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


MAX_LEVELS_PER_SIDE = 10_000
MAX_RAW_MESSAGE_BYTES = 32_000_000
FULL_ORDERBOOK_REST_PATH = "/v5/market/full_orderbook"
FULL_ORDERBOOK_REST_CATEGORY = "linear"
REST_REQUEST_PROVENANCE = "UNBOUND_EXACT_REQUEST_DECLARATION"
FULL_ORDERBOOK_WS_PATH = "/v5/public/linear"
FULL_ORDERBOOK_WS_CATEGORY = "linear"
WS_CONNECTION_PROVENANCE = "UNBOUND_EXACT_CONNECTION_DECLARATION"
CONTINUITY_BASIS = "REST_EXACT_SEQ_U_BRIDGE_THEN_WS_U_PLUS_ONE_SEQ_NONDECREASING"
KNOWN_DEPTH_LIMITATION = "BOUNDED_LEVELS_PER_SIDE_NOT_COMPLETE_MARKET_DEPTH"
RPI_COVERAGE = "RPI_EXCLUDED_BY_BYBIT_PUBLIC_API"

_SYMBOL = re.compile(r"[A-Z0-9]{2,40}")


class BybitFullBookError(RuntimeError):
    """Base error for the offline synchronizer."""


class BybitFullBookSchemaError(BybitFullBookError):
    """A raw public payload is outside the pinned schema."""


class BybitFullBookCausalityError(BybitFullBookError):
    """A receive clock predates a timestamp carried by the payload."""


class BybitFullBookStateError(BybitFullBookError):
    """The operation is not allowed in the current synchronization state."""


class BybitFullBookEpochError(BybitFullBookStateError):
    """Evidence belongs to an inactive or non-advancing connection epoch."""


class BybitFullBookRetryExhausted(BybitFullBookStateError):
    """A configured deterministic retry bound has been reached."""


Level = tuple[str, str]


def _frozen_record(record_type: type[Any], **values: object) -> Any:
    expected = set(record_type.__dataclass_fields__)
    if set(values) != expected:
        raise BybitFullBookSchemaError("internal frozen-record field mismatch")
    record = object.__new__(record_type)
    for name, value in values.items():
        object.__setattr__(record, name, value)
    return record


@dataclass(frozen=True, init=False)
class BybitFullBookRestSnapshot:
    contract: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    response_ts_ms: int
    gateway_ts_ms: int
    exchange_ts_ms: int
    update_id: int
    cross_sequence: int
    received_ts: float
    monotonic_ns: int
    source_clock_skew_ms: str
    request_path: str
    request_category: str
    request_provenance: str
    raw_bytes: bytes
    raw_sha256: str

    @classmethod
    def from_raw(
        cls,
        raw: bytes,
        received_ts: float,
        monotonic_ns: int,
        contract: str,
        *,
        request_path: str,
        request_category: str,
    ) -> "BybitFullBookRestSnapshot":
        symbol = _contract(contract)
        path, category = _full_orderbook_request(request_path, request_category)
        received = _received_ts(received_ts)
        monotonic = _monotonic_ns(monotonic_ns)
        payload, exact_raw, raw_sha = _raw_object(raw)
        if _integer(_required(payload, "retCode", "REST response"), "retCode") != 0:
            raise BybitFullBookSchemaError("REST retCode must be zero")
        if _required(payload, "retMsg", "REST response") != "OK":
            raise BybitFullBookSchemaError("REST retMsg must be exactly OK")
        result = _mapping(_required(payload, "result", "REST response"), "REST result")
        if _required(result, "s", "REST result") != symbol:
            raise BybitFullBookSchemaError("REST result symbol does not match contract")
        response_ts = _integer(_required(payload, "time", "REST response"), "REST time")
        gateway_ts = _integer(_required(result, "ts", "REST result"), "REST result ts")
        exchange_ts = _integer(_required(result, "cts", "REST result"), "REST result cts")
        if gateway_ts < exchange_ts or response_ts < gateway_ts:
            raise BybitFullBookCausalityError("REST timestamps are not causally ordered")
        return _frozen_record(
            cls,
            contract=symbol,
            bids=_levels(_required(result, "b", "REST result"), "REST bids", allow_zero=False),
            asks=_levels(_required(result, "a", "REST result"), "REST asks", allow_zero=False),
            response_ts_ms=response_ts,
            gateway_ts_ms=gateway_ts,
            exchange_ts_ms=exchange_ts,
            update_id=_integer(_required(result, "u", "REST result"), "REST u"),
            cross_sequence=_integer(_required(result, "seq", "REST result"), "REST seq"),
            received_ts=received,
            monotonic_ns=monotonic,
            source_clock_skew_ms=_clock_skew_ms(received, response_ts),
            request_path=path,
            request_category=category,
            request_provenance=REST_REQUEST_PROVENANCE,
            raw_bytes=exact_raw,
            raw_sha256=raw_sha,
        )


@dataclass(frozen=True, init=False)
class BybitFullBookDelta:
    contract: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    gateway_ts_ms: int
    exchange_ts_ms: int
    update_id: int
    cross_sequence: int
    received_ts: float
    monotonic_ns: int
    source_clock_skew_ms: str
    connection_path: str
    connection_category: str
    connection_provenance: str
    raw_bytes: bytes
    raw_sha256: str

    @classmethod
    def from_raw(
        cls,
        raw: bytes,
        received_ts: float,
        monotonic_ns: int,
        contract: str,
        *,
        connection_path: str,
        connection_category: str,
    ) -> "BybitFullBookDelta":
        symbol = _contract(contract)
        path, category = _full_orderbook_connection(
            connection_path,
            connection_category,
        )
        received = _received_ts(received_ts)
        monotonic = _monotonic_ns(monotonic_ns)
        payload, exact_raw, raw_sha = _raw_object(raw)
        if _required(payload, "topic", "WS message") != f"orderbook.full.{symbol}":
            raise BybitFullBookSchemaError("WS topic is not the exact full-book contract topic")
        if _required(payload, "type", "WS message") != "delta":
            raise BybitFullBookSchemaError("full-book WS message must be delta")
        data = _mapping(_required(payload, "data", "WS message"), "WS data")
        if _required(data, "s", "WS data") != symbol:
            raise BybitFullBookSchemaError("WS data symbol does not match contract")
        gateway_ts = _integer(_required(payload, "ts", "WS message"), "WS ts")
        exchange_ts = _integer(_required(payload, "cts", "WS message"), "WS cts")
        if gateway_ts < exchange_ts:
            raise BybitFullBookCausalityError("WS timestamps are not causally ordered")
        return _frozen_record(
            cls,
            contract=symbol,
            bids=_levels(_required(data, "b", "WS data"), "WS bids", allow_zero=True),
            asks=_levels(_required(data, "a", "WS data"), "WS asks", allow_zero=True),
            gateway_ts_ms=gateway_ts,
            exchange_ts_ms=exchange_ts,
            update_id=_integer(_required(data, "u", "WS data"), "WS u"),
            cross_sequence=_integer(_required(data, "seq", "WS data"), "WS seq"),
            received_ts=received,
            monotonic_ns=monotonic,
            source_clock_skew_ms=_clock_skew_ms(received, gateway_ts),
            connection_path=path,
            connection_category=category,
            connection_provenance=WS_CONNECTION_PROVENANCE,
            raw_bytes=exact_raw,
            raw_sha256=raw_sha,
        )


@dataclass(frozen=True)
class BybitFullBookCausalSnapshot:
    symbol: str
    epoch: int | None
    generation: int
    status: str
    synchronized: bool
    resync_required: bool
    invalidation_reason: str | None
    continuity_basis: str
    known_depth_limit_per_side: int
    known_depth_limitation: str
    rpi_coverage: str
    connection_path: str | None
    connection_category: str | None
    connection_provenance: str | None
    update_id: int | None
    cross_sequence: int | None
    exchange_ts_ms: int | None
    received_ts: float | None
    monotonic_ns: int | None
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    evidence_chain_sha256: str | None
    evidence_record_count: int
    book_structurally_ready: bool
    execution_ready: bool


@dataclass(frozen=True)
class BybitFullBookRestAttempt:
    epoch: int
    generation: int
    attempt: int
    issued_received_ts: float
    issued_monotonic_ns: int
    request_path: str
    request_category: str
    request_symbol: str
    request_provenance: str


class BybitFullBookSynchronizer:
    """One deterministic, bounded synchronization state for one Bybit symbol."""

    def __init__(
        self,
        *,
        symbol: str,
        max_levels_per_side: int,
        max_buffered_deltas: int,
        max_buffered_bytes: int = 16_000_000,
        max_buffer_age_ms: int = 10_000,
        max_snapshot_attempts: int = 3,
        max_resync_attempts: int = 3,
    ) -> None:
        self.symbol = _contract(symbol)
        self.max_levels_per_side = _bounded_integer(
            max_levels_per_side, "max_levels_per_side", maximum=MAX_LEVELS_PER_SIDE
        )
        self.max_buffered_deltas = _bounded_integer(
            max_buffered_deltas, "max_buffered_deltas", maximum=1_000_000
        )
        self.max_buffered_bytes = _bounded_integer(
            max_buffered_bytes, "max_buffered_bytes", maximum=1_000_000_000
        )
        self.max_buffer_age_ms = _bounded_integer(
            max_buffer_age_ms, "max_buffer_age_ms", maximum=3_600_000
        )
        self.max_snapshot_attempts = _bounded_integer(
            max_snapshot_attempts, "max_snapshot_attempts", maximum=1_000
        )
        self.max_resync_attempts = _bounded_integer(
            max_resync_attempts, "max_resync_attempts", maximum=1_000
        )
        self._epoch: int | None = None
        self._connection_path: str | None = None
        self._connection_category: str | None = None
        self._connection_provenance: str | None = None
        self._generation = 0
        self._resync_attempts = 0
        self._status = "WAITING_FOR_EPOCH"
        self._invalidation_reason: str | None = None
        self._reset_epoch_state()

    def begin_epoch(
        self,
        epoch: int,
        *,
        connection_path: str = FULL_ORDERBOOK_WS_PATH,
        connection_category: str = FULL_ORDERBOOK_WS_CATEGORY,
    ) -> None:
        if self._epoch is not None:
            raise BybitFullBookStateError("an epoch is already active")
        self._start_new_epoch(epoch, connection_path, connection_category)

    def reconnect(
        self,
        epoch: int,
        *,
        connection_path: str = FULL_ORDERBOOK_WS_PATH,
        connection_category: str = FULL_ORDERBOOK_WS_CATEGORY,
    ) -> None:
        candidate = _epoch(epoch)
        if self._epoch is None:
            raise BybitFullBookStateError("begin_epoch is required before reconnect")
        if candidate <= self._epoch:
            raise BybitFullBookEpochError("reconnect epoch must strictly advance")
        self._start_new_epoch(candidate, connection_path, connection_category)

    def issue_rest_attempt(
        self,
        *,
        epoch: int,
        issued_received_ts: float,
        issued_monotonic_ns: int,
    ) -> BybitFullBookRestAttempt:
        self._require_epoch(epoch)
        if self._status not in {"BUFFERING", "WAITING_FOR_BRIDGE"}:
            raise BybitFullBookStateError("REST attempt is not allowed in current state")
        if self._outstanding_attempt is not None:
            raise BybitFullBookStateError("a REST attempt is already outstanding")
        if self._snapshot_attempts >= self.max_snapshot_attempts:
            self._invalidate("SNAPSHOT_RETRY_EXHAUSTED", retry_exhausted=True)
            raise BybitFullBookRetryExhausted("snapshot retry bound reached")
        self._snapshot_attempts += 1
        attempt = BybitFullBookRestAttempt(
            epoch=self._epoch or 0,
            generation=self._generation,
            attempt=self._snapshot_attempts,
            issued_received_ts=_received_ts(issued_received_ts),
            issued_monotonic_ns=_monotonic_ns(issued_monotonic_ns),
            request_path=FULL_ORDERBOOK_REST_PATH,
            request_category=FULL_ORDERBOOK_REST_CATEGORY,
            request_symbol=self.symbol,
            request_provenance=REST_REQUEST_PROVENANCE,
        )
        self._outstanding_attempt = BybitFullBookRestAttempt(
            epoch=attempt.epoch,
            generation=attempt.generation,
            attempt=attempt.attempt,
            issued_received_ts=attempt.issued_received_ts,
            issued_monotonic_ns=attempt.issued_monotonic_ns,
            request_path=attempt.request_path,
            request_category=attempt.request_category,
            request_symbol=attempt.request_symbol,
            request_provenance=attempt.request_provenance,
        )
        self._outstanding_attempt_handle = attempt
        return attempt

    def ingest_delta(self, delta: BybitFullBookDelta, *, epoch: int) -> str:
        self._require_epoch(epoch)
        if not isinstance(delta, BybitFullBookDelta) or delta.contract != self.symbol:
            raise BybitFullBookSchemaError("delta does not match synchronizer symbol")
        reparsed_delta = BybitFullBookDelta.from_raw(
            delta.raw_bytes,
            delta.received_ts,
            delta.monotonic_ns,
            delta.contract,
            connection_path=delta.connection_path,
            connection_category=delta.connection_category,
        )
        if reparsed_delta != delta:
            raise BybitFullBookSchemaError("typed delta does not match its raw evidence")
        delta = reparsed_delta
        if (
            delta.connection_path,
            delta.connection_category,
            delta.connection_provenance,
        ) != (
            self._connection_path,
            self._connection_category,
            self._connection_provenance,
        ):
            raise BybitFullBookSchemaError("WS delta connection provenance mismatch")
        if self._status in {"RESYNC_REQUIRED", "RETRY_EXHAUSTED"}:
            raise BybitFullBookStateError("a new epoch is required before more evidence")

        if self._status == "SYNCHRONIZED" and delta.update_id == 1:
            self._invalidate("WS_U_ONE_RESET")
            return "INVALIDATED"

        existing = self._seen_by_update_id.get(delta.update_id)
        if existing is not None:
            if (
                existing.cross_sequence == delta.cross_sequence
                and existing.raw_sha256 == delta.raw_sha256
            ):
                return "DUPLICATE"
            self._invalidate("CONFLICTING_DUPLICATE")
            return "INVALIDATED"

        if self._last_observed_update_id is not None:
            if delta.update_id != self._last_observed_update_id + 1:
                self._invalidate("WS_U_GAP")
                return "INVALIDATED"
        if (
            self._last_observed_cross_sequence is not None
            and delta.cross_sequence < self._last_observed_cross_sequence
        ):
            self._invalidate("WS_SEQ_REGRESSION")
            return "INVALIDATED"
        if self._last_observed_monotonic_ns is not None and delta.monotonic_ns <= self._last_observed_monotonic_ns:
            self._invalidate("WS_RECEIVE_ORDER_REGRESSION")
            return "INVALIDATED"

        self._seen_by_update_id[delta.update_id] = delta
        self._last_observed_update_id = delta.update_id
        self._last_observed_cross_sequence = delta.cross_sequence
        self._last_observed_monotonic_ns = delta.monotonic_ns

        if self._status == "SYNCHRONIZED":
            if not self._apply_delta(delta):
                return "INVALIDATED"
            return "APPLIED"

        self._buffer.append(delta)
        self._buffered_bytes += len(delta.raw_bytes)
        if len(self._buffer) > self.max_buffered_deltas:
            self._invalidate("BUFFER_COUNT_EXCEEDED")
            return "INVALIDATED"
        if self._buffered_bytes > self.max_buffered_bytes:
            self._invalidate("BUFFER_BYTES_EXCEEDED")
            return "INVALIDATED"
        if self._buffer_age_ms() > Decimal(self.max_buffer_age_ms):
            self._invalidate("BUFFER_AGE_EXCEEDED")
            return "INVALIDATED"

        if self._pending_snapshot is not None:
            pending = self._pending_snapshot
            if (delta.update_id, delta.cross_sequence) == (
                pending.update_id,
                pending.cross_sequence,
            ):
                if delta.monotonic_ns <= pending.monotonic_ns:
                    self._invalidate("REST_WS_RECEIVE_ORDER_INVALID")
                    return "INVALIDATED"
                pending_attempt = self._pending_snapshot_attempt
                if pending_attempt is None:
                    self._invalidate("INTERNAL_REST_ATTEMPT_MISSING")
                    return "INVALIDATED"
                return (
                    "SYNCHRONIZED"
                    if self._anchor(pending, pending_attempt)
                    else "INVALIDATED"
                )
            if delta.cross_sequence > pending.cross_sequence:
                self._pending_snapshot = None
                self._pending_snapshot_attempt = None
                if self._snapshot_attempts >= self.max_snapshot_attempts:
                    self._invalidate("SNAPSHOT_RETRY_EXHAUSTED", retry_exhausted=True)
                    return "INVALIDATED"
                self._status = "BUFFERING"
        return "BUFFERED"

    def ingest_rest_snapshot(
        self,
        snapshot: BybitFullBookRestSnapshot,
        *,
        epoch: int,
        attempt: BybitFullBookRestAttempt,
    ) -> bool:
        self._require_epoch(epoch)
        if not isinstance(snapshot, BybitFullBookRestSnapshot) or snapshot.contract != self.symbol:
            raise BybitFullBookSchemaError("REST snapshot does not match synchronizer symbol")
        reparsed_snapshot = BybitFullBookRestSnapshot.from_raw(
            snapshot.raw_bytes,
            snapshot.received_ts,
            snapshot.monotonic_ns,
            snapshot.contract,
            request_path=snapshot.request_path,
            request_category=snapshot.request_category,
        )
        if reparsed_snapshot != snapshot:
            raise BybitFullBookSchemaError("typed REST snapshot does not match its raw evidence")
        snapshot = reparsed_snapshot
        if self._status in {"SYNCHRONIZED", "RESYNC_REQUIRED", "RETRY_EXHAUSTED"}:
            raise BybitFullBookStateError("REST snapshot is not accepted in current state")
        canonical_attempt = self._consume_rest_attempt(attempt, snapshot)

        match = self._matching_buffer_index(snapshot)
        if match is not None:
            if snapshot.monotonic_ns < self._buffer[match].monotonic_ns:
                return self._snapshot_mismatch()
            return self._anchor(snapshot, canonical_attempt)

        if self._snapshot_is_ahead(snapshot):
            self._pending_snapshot = snapshot
            self._pending_snapshot_attempt = canonical_attempt
            self._status = "WAITING_FOR_BRIDGE"
            return False
        return self._snapshot_mismatch()

    def causal_snapshot(self) -> BybitFullBookCausalSnapshot:
        bids = self._render_side(self._bids, reverse=True) if self._status == "SYNCHRONIZED" else ()
        asks = self._render_side(self._asks, reverse=False) if self._status == "SYNCHRONIZED" else ()
        noncrossed = bool(bids and asks) and Decimal(bids[0][0]) < Decimal(asks[0][0])
        ready = self._status == "SYNCHRONIZED" and noncrossed
        return BybitFullBookCausalSnapshot(
            symbol=self.symbol,
            epoch=self._epoch,
            generation=self._generation,
            status=self._status,
            synchronized=self._status == "SYNCHRONIZED",
            resync_required=self._status in {"RESYNC_REQUIRED", "RETRY_EXHAUSTED"},
            invalidation_reason=self._invalidation_reason,
            continuity_basis=CONTINUITY_BASIS,
            known_depth_limit_per_side=self.max_levels_per_side,
            known_depth_limitation=KNOWN_DEPTH_LIMITATION,
            rpi_coverage=RPI_COVERAGE,
            connection_path=self._connection_path,
            connection_category=self._connection_category,
            connection_provenance=self._connection_provenance,
            update_id=self._current_update_id if self._status == "SYNCHRONIZED" else None,
            cross_sequence=self._current_cross_sequence if self._status == "SYNCHRONIZED" else None,
            exchange_ts_ms=self._exchange_ts_ms if self._status == "SYNCHRONIZED" else None,
            received_ts=self._causal_received_ts if self._status == "SYNCHRONIZED" else None,
            monotonic_ns=self._causal_monotonic_ns if self._status == "SYNCHRONIZED" else None,
            bids=bids,
            asks=asks,
            evidence_chain_sha256=self._evidence_chain if self._status == "SYNCHRONIZED" else None,
            evidence_record_count=self._evidence_record_count if self._status == "SYNCHRONIZED" else 0,
            book_structurally_ready=ready,
            execution_ready=False,
        )

    def _start_new_epoch(
        self,
        epoch: int,
        connection_path: object,
        connection_category: object,
    ) -> None:
        candidate = _epoch(epoch)
        path, category = _full_orderbook_connection(
            connection_path,
            connection_category,
        )
        if self._resync_attempts >= self.max_resync_attempts:
            self._reset_epoch_state()
            self._status = "RETRY_EXHAUSTED"
            self._invalidation_reason = "RESYNC_RETRY_EXHAUSTED"
            raise BybitFullBookRetryExhausted("resync retry bound reached")
        self._resync_attempts += 1
        self._generation += 1
        self._epoch = candidate
        self._connection_path = path
        self._connection_category = category
        self._connection_provenance = WS_CONNECTION_PROVENANCE
        self._reset_epoch_state()
        self._status = "BUFFERING"

    def _reset_epoch_state(self) -> None:
        self._buffer: list[BybitFullBookDelta] = []
        self._buffered_bytes = 0
        self._pending_snapshot: BybitFullBookRestSnapshot | None = None
        self._pending_snapshot_attempt: BybitFullBookRestAttempt | None = None
        self._seen_by_update_id: dict[int, BybitFullBookDelta] = {}
        self._last_observed_update_id: int | None = None
        self._last_observed_cross_sequence: int | None = None
        self._last_observed_monotonic_ns: int | None = None
        self._snapshot_attempts = 0
        self._outstanding_attempt: BybitFullBookRestAttempt | None = None
        self._outstanding_attempt_handle: BybitFullBookRestAttempt | None = None
        self._bids: dict[Decimal, Level] = {}
        self._asks: dict[Decimal, Level] = {}
        self._current_update_id: int | None = None
        self._current_cross_sequence: int | None = None
        self._exchange_ts_ms: int | None = None
        self._causal_received_ts: float | None = None
        self._causal_monotonic_ns: int | None = None
        self._evidence_chain: str | None = None
        self._evidence_record_count = 0
        self._invalidation_reason = None

    def _consume_rest_attempt(
        self,
        attempt: BybitFullBookRestAttempt,
        snapshot: BybitFullBookRestSnapshot,
    ) -> BybitFullBookRestAttempt:
        if not isinstance(attempt, BybitFullBookRestAttempt):
            raise BybitFullBookStateError("REST attempt record is required")
        is_current_handle = self._outstanding_attempt_handle is attempt
        if not is_current_handle and (
            attempt.epoch != self._epoch or attempt.generation != self._generation
        ):
            raise BybitFullBookEpochError("REST response belongs to a stale generation")
        canonical = self._outstanding_attempt
        if not is_current_handle or canonical is None:
            raise BybitFullBookStateError("REST attempt is stale, forged, or already consumed")
        if attempt != canonical:
            self._invalidate("REST_ATTEMPT_MUTATED")
            raise BybitFullBookStateError(
                "REST attempt handle mutated after canonical issuance"
            )
        if snapshot.monotonic_ns < canonical.issued_monotonic_ns:
            raise BybitFullBookCausalityError("REST response predates its issued attempt")
        if snapshot.received_ts < canonical.issued_received_ts:
            raise BybitFullBookCausalityError("REST receive time predates its issued attempt")
        if (
            snapshot.request_path,
            snapshot.request_category,
            snapshot.request_provenance,
            snapshot.contract,
        ) != (
            canonical.request_path,
            canonical.request_category,
            canonical.request_provenance,
            canonical.request_symbol,
        ):
            raise BybitFullBookSchemaError("REST response request provenance mismatch")
        self._outstanding_attempt = None
        self._outstanding_attempt_handle = None
        return canonical

    def _require_epoch(self, epoch: int) -> None:
        candidate = _epoch(epoch)
        if self._epoch is None:
            raise BybitFullBookEpochError("no active epoch")
        if candidate != self._epoch:
            raise BybitFullBookEpochError("evidence epoch does not match active epoch")

    def _matching_buffer_index(self, snapshot: BybitFullBookRestSnapshot) -> int | None:
        matches = [
            index
            for index, delta in enumerate(self._buffer)
            if (delta.update_id, delta.cross_sequence)
            == (snapshot.update_id, snapshot.cross_sequence)
        ]
        return matches[0] if len(matches) == 1 else None

    def _snapshot_is_ahead(self, snapshot: BybitFullBookRestSnapshot) -> bool:
        if not self._buffer:
            return True
        if any(delta.cross_sequence == snapshot.cross_sequence for delta in self._buffer):
            return False
        last = self._buffer[-1]
        return (
            snapshot.cross_sequence > last.cross_sequence
            and snapshot.update_id > last.update_id
        )

    def _snapshot_mismatch(self) -> bool:
        self._pending_snapshot = None
        self._pending_snapshot_attempt = None
        if self._snapshot_attempts >= self.max_snapshot_attempts:
            self._invalidate("SNAPSHOT_RETRY_EXHAUSTED", retry_exhausted=True)
        else:
            self._status = "BUFFERING"
        return False

    def _anchor(
        self,
        snapshot: BybitFullBookRestSnapshot,
        attempt: BybitFullBookRestAttempt,
    ) -> bool:
        index = self._matching_buffer_index(snapshot)
        if index is None:
            return self._snapshot_mismatch()
        bridge = self._buffer[index]

        self._bids = _book_side(snapshot.bids)
        self._asks = _book_side(snapshot.asks)
        self._trim_book()
        if self._is_crossed():
            self._invalidate("CROSSED_BOOK")
            return False

        self._current_update_id = snapshot.update_id
        self._current_cross_sequence = snapshot.cross_sequence
        self._exchange_ts_ms = max(snapshot.exchange_ts_ms, bridge.exchange_ts_ms)
        self._causal_received_ts = max(snapshot.received_ts, bridge.received_ts)
        if snapshot.monotonic_ns >= bridge.monotonic_ns:
            self._causal_received_ts = snapshot.received_ts
            self._causal_monotonic_ns = snapshot.monotonic_ns
        else:
            self._causal_received_ts = bridge.received_ts
            self._causal_monotonic_ns = bridge.monotonic_ns
        self._evidence_chain = _initial_chain(
            self.symbol,
            self._epoch or 0,
            snapshot,
            bridge,
            attempt,
        )
        self._evidence_record_count = 2
        self._status = "SYNCHRONIZED"
        self._pending_snapshot = None
        self._pending_snapshot_attempt = None

        for delta in self._buffer[index + 1 :]:
            if not self._apply_delta(delta):
                return False
        self._buffer = []
        self._buffered_bytes = 0
        self._last_observed_monotonic_ns = max(
            self._last_observed_monotonic_ns or 0,
            self._causal_monotonic_ns or 0,
        )
        return True

    def _apply_delta(self, delta: BybitFullBookDelta) -> bool:
        if self._current_update_id is None or self._current_cross_sequence is None:
            self._invalidate("INTERNAL_BASE_MISSING")
            return False
        if delta.update_id != self._current_update_id + 1:
            self._invalidate("WS_U_GAP")
            return False
        if delta.cross_sequence < self._current_cross_sequence:
            self._invalidate("WS_SEQ_REGRESSION")
            return False
        _apply_levels(self._bids, delta.bids)
        _apply_levels(self._asks, delta.asks)
        self._trim_book()
        if self._is_crossed():
            self._invalidate("CROSSED_BOOK")
            return False
        self._current_update_id = delta.update_id
        self._current_cross_sequence = delta.cross_sequence
        self._exchange_ts_ms = max(self._exchange_ts_ms or 0, delta.exchange_ts_ms)
        if delta.monotonic_ns > (self._causal_monotonic_ns or -1):
            self._causal_received_ts = delta.received_ts
            self._causal_monotonic_ns = delta.monotonic_ns
        self._evidence_chain = _next_chain(self._evidence_chain or "", delta)
        self._evidence_record_count += 1
        return True

    def _trim_book(self) -> None:
        self._bids = _trim_side(self._bids, self.max_levels_per_side, reverse=True)
        self._asks = _trim_side(self._asks, self.max_levels_per_side, reverse=False)

    def _render_side(self, side: Mapping[Decimal, Level], *, reverse: bool) -> tuple[Level, ...]:
        return tuple(side[price] for price in sorted(side, reverse=reverse))

    def _is_crossed(self) -> bool:
        return bool(self._bids and self._asks) and max(self._bids) >= min(self._asks)

    def _buffer_age_ms(self) -> Decimal:
        if len(self._buffer) < 2:
            return Decimal(0)
        elapsed_ns = self._buffer[-1].monotonic_ns - self._buffer[0].monotonic_ns
        return Decimal(elapsed_ns) / Decimal(1_000_000)

    def _invalidate(self, reason: str, *, retry_exhausted: bool = False) -> None:
        self._buffer = []
        self._buffered_bytes = 0
        self._pending_snapshot = None
        self._pending_snapshot_attempt = None
        self._outstanding_attempt = None
        self._outstanding_attempt_handle = None
        self._bids = {}
        self._asks = {}
        self._current_update_id = None
        self._current_cross_sequence = None
        self._exchange_ts_ms = None
        self._causal_received_ts = None
        self._causal_monotonic_ns = None
        self._evidence_chain = None
        self._evidence_record_count = 0
        self._invalidation_reason = reason
        self._status = "RETRY_EXHAUSTED" if retry_exhausted else "RESYNC_REQUIRED"


def _contract(value: object) -> str:
    if not isinstance(value, str) or _SYMBOL.fullmatch(value) is None:
        raise BybitFullBookSchemaError("contract is not canonical")
    return value


def _full_orderbook_request(path: object, category: object) -> tuple[str, str]:
    if path != FULL_ORDERBOOK_REST_PATH:
        raise BybitFullBookSchemaError("REST source path is not the exact full orderbook endpoint")
    if category != FULL_ORDERBOOK_REST_CATEGORY:
        raise BybitFullBookSchemaError("REST source category must be exact linear")
    return FULL_ORDERBOOK_REST_PATH, FULL_ORDERBOOK_REST_CATEGORY


def _full_orderbook_connection(path: object, category: object) -> tuple[str, str]:
    if path != FULL_ORDERBOOK_WS_PATH:
        raise BybitFullBookSchemaError("WS source path is not the exact linear public endpoint")
    if category != FULL_ORDERBOOK_WS_CATEGORY:
        raise BybitFullBookSchemaError("WS source category must be exact linear")
    return FULL_ORDERBOOK_WS_PATH, FULL_ORDERBOOK_WS_CATEGORY


def _epoch(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BybitFullBookEpochError("epoch must be a positive integer")
    return value


def _bounded_integer(value: object, label: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")
    return value


def _received_ts(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BybitFullBookSchemaError("received_ts must be a finite non-negative number")
    rendered = Decimal(str(value))
    if not rendered.is_finite() or rendered < 0:
        raise BybitFullBookSchemaError("received_ts must be a finite non-negative number")
    return float(value)


def _monotonic_ns(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BybitFullBookSchemaError("monotonic_ns must be a non-negative integer")
    return value


def _clock_skew_ms(received_ts: float, source_ts_ms: int) -> str:
    return str(Decimal(str(received_ts)) * 1000 - Decimal(source_ts_ms))


def _raw_object(raw: object) -> tuple[Mapping[str, Any], bytes, str]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_RAW_MESSAGE_BYTES:
        raise BybitFullBookSchemaError("raw payload must be bounded non-empty bytes")
    exact = raw
    try:
        text = exact.decode("utf-8", errors="strict")
        decoded = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                BybitFullBookSchemaError(f"non-finite JSON constant: {value}")
            ),
        )
    except BybitFullBookSchemaError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BybitFullBookSchemaError("raw payload is not strict UTF-8 JSON") from exc
    return _mapping(decoded, "raw payload"), exact, hashlib.sha256(exact).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BybitFullBookSchemaError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BybitFullBookSchemaError(f"{label} must be an object")
    return value


def _required(row: Mapping[str, Any], key: str, label: str) -> Any:
    if key not in row:
        raise BybitFullBookSchemaError(f"{label} is missing {key}")
    return row[key]


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BybitFullBookSchemaError(f"{label} must be a non-negative integer")
    return value


def _levels(value: object, label: str, *, allow_zero: bool) -> tuple[Level, ...]:
    if not isinstance(value, list) or len(value) > MAX_LEVELS_PER_SIDE:
        raise BybitFullBookSchemaError(f"{label} must be an array with at most 10000 rows")
    result: list[Level] = []
    seen: set[Decimal] = set()
    for index, raw_level in enumerate(value):
        if not isinstance(raw_level, list) or len(raw_level) != 2:
            raise BybitFullBookSchemaError(f"{label}[{index}] must contain price and size")
        price_text, price = _decimal(raw_level[0], f"{label}[{index}].price", positive=True)
        size_text, size = _decimal(raw_level[1], f"{label}[{index}].size", positive=False)
        if (not allow_zero and size <= 0) or (allow_zero and size < 0):
            raise BybitFullBookSchemaError(f"{label}[{index}].size has invalid sign")
        if price in seen:
            raise BybitFullBookSchemaError(f"{label} contains a duplicate numeric price")
        seen.add(price)
        result.append((price_text, size_text))
    return tuple(result)


def _decimal(value: object, label: str, *, positive: bool) -> tuple[str, Decimal]:
    if not isinstance(value, str) or not value:
        raise BybitFullBookSchemaError(f"{label} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise BybitFullBookSchemaError(f"{label} is not decimal") from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise BybitFullBookSchemaError(f"{label} has invalid magnitude")
    return value, parsed


def _book_side(levels: tuple[Level, ...]) -> dict[Decimal, Level]:
    return {Decimal(price): (price, size) for price, size in levels}


def _apply_levels(book: dict[Decimal, Level], levels: tuple[Level, ...]) -> None:
    for price_text, size_text in levels:
        price = Decimal(price_text)
        if Decimal(size_text) == 0:
            book.pop(price, None)
        else:
            book[price] = (price_text, size_text)


def _trim_side(
    side: dict[Decimal, Level],
    limit: int,
    *,
    reverse: bool,
) -> dict[Decimal, Level]:
    selected = sorted(side, reverse=reverse)[:limit]
    return {price: side[price] for price in selected}


def _receive_clock_material(received_ts: float, monotonic_ns: int) -> tuple[str, str]:
    return (str(Decimal(str(received_ts))), str(monotonic_ns))


def _initial_chain(
    symbol: str,
    epoch: int,
    snapshot: BybitFullBookRestSnapshot,
    bridge: BybitFullBookDelta,
    attempt: BybitFullBookRestAttempt,
) -> str:
    snapshot_clock = _receive_clock_material(snapshot.received_ts, snapshot.monotonic_ns)
    bridge_clock = _receive_clock_material(bridge.received_ts, bridge.monotonic_ns)
    material = "\0".join(
        (
            "BYBIT_FULL_BOOK_V43",
            RPI_COVERAGE,
            symbol,
            str(epoch),
            "REST_ATTEMPT",
            str(attempt.generation),
            str(attempt.attempt),
            str(Decimal(str(attempt.issued_received_ts))),
            str(attempt.issued_monotonic_ns),
            attempt.request_path,
            attempt.request_category,
            attempt.request_symbol,
            attempt.request_provenance,
            "REST",
            snapshot.request_path,
            snapshot.request_category,
            snapshot.request_provenance,
            snapshot.raw_sha256,
            *snapshot_clock,
            "WS",
            bridge.connection_path,
            bridge.connection_category,
            bridge.connection_provenance,
            bridge.raw_sha256,
            *bridge_clock,
        )
    ).encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _next_chain(previous: str, delta: BybitFullBookDelta) -> str:
    received_ts, monotonic_ns = _receive_clock_material(
        delta.received_ts,
        delta.monotonic_ns,
    )
    material = "\0".join(
        (
            previous,
            "WS",
            delta.connection_path,
            delta.connection_category,
            delta.connection_provenance,
            delta.raw_sha256,
            received_ts,
            monotonic_ns,
        )
    ).encode("ascii")
    return hashlib.sha256(material).hexdigest()
