"""Pure venue-normalized L2 state for future event-bound capture.

This module deliberately knows no exchange protocol.  Adapters must provide complete
normalized frames with an immutable raw-frame hash.  The state machine then enforces
the causal invariants shared by every venue: a connection epoch starts with a snapshot,
deltas must link to the last accepted sequence, and any gap remains fail-closed until a
fresh snapshot arrives.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Iterable


ABSOLUTE_MAX_LEVELS = 1_000
MAX_DECISIONS_RETAINED = 1_024
_SHA256 = re.compile(r"[0-9a-f]{64}")


class L2BookError(ValueError):
    """A normalized frame or state transition violates the pure L2 contract."""


class FrameKind(str, Enum):
    SNAPSHOT = "snapshot"
    DELTA = "delta"


class MarketPhase(str, Enum):
    PREOPEN = "preopen"
    CONTINUOUS = "continuous"


class ApplyStatus(str, Enum):
    APPLIED_SNAPSHOT = "APPLIED_SNAPSHOT"
    APPLIED_DELTA = "APPLIED_DELTA"
    IGNORED_OLD_SEQUENCE = "IGNORED_OLD_SEQUENCE"
    IGNORED_SNAPSHOT_REQUIRED = "IGNORED_SNAPSHOT_REQUIRED"
    IGNORED_TAINTED_EPOCH = "IGNORED_TAINTED_EPOCH"
    IGNORED_STALE_EPOCH = "IGNORED_STALE_EPOCH"
    IGNORED_STALE_SNAPSHOT = "IGNORED_STALE_SNAPSHOT"
    TAINTED_SEQUENCE_GAP = "TAINTED_SEQUENCE_GAP"
    TAINTED_NONCAUSAL_CLOCK = "TAINTED_NONCAUSAL_CLOCK"
    TAINTED_CROSSED_BOOK = "TAINTED_CROSSED_BOOK"
    TAINTED_MAX_LEVELS = "TAINTED_MAX_LEVELS"


def _decimal(value: object, *, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise L2BookError(f"{field} must be a finite decimal") from exc
    if not number.is_finite():
        raise L2BookError(f"{field} must be a finite decimal")
    return number


def _positive_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise L2BookError(f"{field} must be a positive integer")
    return value


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        price = _decimal(self.price, field="price")
        size = _decimal(self.size, field="size")
        if price <= 0:
            raise L2BookError("price must be positive")
        if size < 0:
            raise L2BookError("size cannot be negative")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "size", size)


@dataclass(frozen=True)
class NormalizedL2Frame:
    venue: str
    symbol: str
    kind: FrameKind
    phase: MarketPhase
    connection_epoch: int
    sequence: int
    previous_sequence: int | None
    exchange_ts: float
    received_ts: float
    monotonic_ns: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    raw_sha256: str

    def __post_init__(self) -> None:
        venue = str(self.venue).strip().lower()
        symbol = str(self.symbol).strip()
        if not venue or not symbol:
            raise L2BookError("venue and symbol are required")
        try:
            kind = FrameKind(self.kind)
            phase = MarketPhase(self.phase)
        except ValueError as exc:
            raise L2BookError("kind and phase must be declared enum values") from exc
        epoch = _positive_int(self.connection_epoch, field="connection_epoch")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise L2BookError("sequence must be a non-negative integer")
        previous = self.previous_sequence
        if previous is not None and (
            isinstance(previous, bool) or not isinstance(previous, int) or previous < 0
        ):
            raise L2BookError("previous_sequence must be a non-negative integer or None")
        if kind is FrameKind.SNAPSHOT and previous is not None:
            raise L2BookError("snapshot previous_sequence must be None")
        for field, value in (("exchange_ts", self.exchange_ts), ("received_ts", self.received_ts)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise L2BookError(f"{field} must be finite and positive")
        if (
            isinstance(self.monotonic_ns, bool)
            or not isinstance(self.monotonic_ns, int)
            or self.monotonic_ns <= 0
        ):
            raise L2BookError("monotonic_ns must be a positive integer")
        bids = tuple(self.bids)
        asks = tuple(self.asks)
        if not all(isinstance(row, BookLevel) for row in bids + asks):
            raise L2BookError("bids and asks must contain BookLevel rows")
        raw_sha256 = str(self.raw_sha256)
        if _SHA256.fullmatch(raw_sha256) is None:
            raise L2BookError("raw_sha256 must be a lowercase SHA-256 hex digest")
        object.__setattr__(self, "venue", venue)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "connection_epoch", epoch)
        object.__setattr__(self, "exchange_ts", float(self.exchange_ts))
        object.__setattr__(self, "received_ts", float(self.received_ts))
        object.__setattr__(self, "bids", bids)
        object.__setattr__(self, "asks", asks)
        object.__setattr__(self, "raw_sha256", raw_sha256)


@dataclass(frozen=True)
class FrameDecision:
    status: ApplyStatus
    reason: str
    connection_epoch: int
    sequence: int
    previous_sequence: int | None
    raw_sha256: str


@dataclass(frozen=True)
class CausalDepthSnapshot:
    venue: str
    symbol: str
    phase: MarketPhase
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    exchange_ts: float
    received_ts: float
    monotonic_ns: int
    connection_epoch: int
    sequence: int
    gap_free: bool
    execution_ready: bool
    raw_sha256: str
    frame_chain_sha256: str


class L2Book:
    """One bounded, sequence-aware book for one venue instrument."""

    def __init__(self, *, venue: str, symbol: str, max_levels: int) -> None:
        normalized_venue = str(venue).strip().lower()
        normalized_symbol = str(symbol).strip()
        if not normalized_venue or not normalized_symbol:
            raise L2BookError("venue and symbol are required")
        max_levels = _positive_int(max_levels, field="max_levels")
        if max_levels > ABSOLUTE_MAX_LEVELS:
            raise L2BookError(
                f"max_levels cannot exceed the hard ceiling {ABSOLUTE_MAX_LEVELS}"
            )
        self.venue = normalized_venue
        self.symbol = normalized_symbol
        self.max_levels = max_levels
        self._connection_epoch: int | None = None
        self._decisions: list[FrameDecision] = []
        self._reset_epoch_state()

    @property
    def decisions(self) -> tuple[FrameDecision, ...]:
        return tuple(self._decisions)

    def begin_connection(self, connection_epoch: int) -> None:
        """Begin a strictly newer epoch and require a new authoritative snapshot."""
        epoch = _positive_int(connection_epoch, field="connection_epoch")
        if self._connection_epoch is not None and epoch <= self._connection_epoch:
            raise L2BookError("connection_epoch must strictly increase on reconnect")
        self._connection_epoch = epoch
        self._reset_epoch_state()

    def apply(self, frame: NormalizedL2Frame) -> FrameDecision:
        if not isinstance(frame, NormalizedL2Frame):
            raise L2BookError("frame must be NormalizedL2Frame")
        if frame.venue != self.venue or frame.symbol != self.symbol:
            raise L2BookError("frame venue/symbol does not match this book")
        if self._connection_epoch is None:
            raise L2BookError("begin_connection must be called before applying frames")
        if frame.connection_epoch < self._connection_epoch:
            return self._record(
                frame,
                ApplyStatus.IGNORED_STALE_EPOCH,
                "frame belongs to an earlier connection epoch",
            )
        if frame.connection_epoch > self._connection_epoch:
            raise L2BookError("future connection epoch must be begun explicitly")
        highest_seen_before = self._highest_seen_sequence
        if highest_seen_before is None or frame.sequence > highest_seen_before:
            self._highest_seen_sequence = frame.sequence
        if frame.kind is FrameKind.SNAPSHOT:
            return self._apply_snapshot(frame, highest_seen_before=highest_seen_before)
        return self._apply_delta(frame)

    def causal_snapshot(self) -> CausalDepthSnapshot | None:
        if not self._has_snapshot or self._sequence is None or self._phase is None:
            return None
        bids = self._sorted_bids(self._bids)
        asks = self._sorted_asks(self._asks)
        gap_free = not self._tainted
        execution_ready = bool(
            gap_free
            and self._phase is MarketPhase.CONTINUOUS
            and bids
            and asks
            and bids[0].price < asks[0].price
        )
        return CausalDepthSnapshot(
            venue=self.venue,
            symbol=self.symbol,
            phase=self._phase,
            bids=bids,
            asks=asks,
            exchange_ts=self._exchange_ts,
            received_ts=self._received_ts,
            monotonic_ns=self._monotonic_ns,
            connection_epoch=self._connection_epoch,
            sequence=self._sequence,
            gap_free=gap_free,
            execution_ready=execution_ready,
            raw_sha256=self._raw_sha256,
            frame_chain_sha256=self._frame_chain_sha256,
        )

    def _reset_epoch_state(self) -> None:
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._has_snapshot = False
        self._tainted = False
        self._sequence: int | None = None
        self._phase: MarketPhase | None = None
        self._exchange_ts = 0.0
        self._received_ts = 0.0
        self._monotonic_ns = 0
        self._raw_sha256 = ""
        self._frame_chain_sha256 = ""
        self._highest_seen_sequence: int | None = None

    def _apply_snapshot(
        self,
        frame: NormalizedL2Frame,
        *,
        highest_seen_before: int | None,
    ) -> FrameDecision:
        if highest_seen_before is not None and frame.sequence <= highest_seen_before:
            return self._record(
                frame,
                ApplyStatus.IGNORED_STALE_SNAPSHOT,
                "same-epoch snapshot does not advance past the highest seen sequence",
            )
        if self._has_snapshot and self._sequence is not None:
            if self._clock_regresses(frame):
                self._tainted = True
                return self._record(
                    frame,
                    ApplyStatus.TAINTED_NONCAUSAL_CLOCK,
                    "same-epoch snapshot clocks regress from current state",
                )
        if len(frame.bids) > self.max_levels or len(frame.asks) > self.max_levels:
            self._tainted = True
            return self._record(
                frame,
                ApplyStatus.TAINTED_MAX_LEVELS,
                "snapshot raw rows exceed the hard per-side update limit",
            )
        bids = self._levels(frame.bids)
        asks = self._levels(frame.asks)
        if len(bids) > self.max_levels or len(asks) > self.max_levels:
            self._tainted = True
            return self._record(
                frame,
                ApplyStatus.TAINTED_MAX_LEVELS,
                "snapshot exceeds the hard per-side level limit",
            )
        if frame.phase is MarketPhase.CONTINUOUS and self._crossed(bids, asks):
            self._tainted = True
            return self._record(
                frame,
                ApplyStatus.TAINTED_CROSSED_BOOK,
                "continuous snapshot is locked or crossed",
            )
        self._bids = bids
        self._asks = asks
        self._has_snapshot = True
        self._tainted = False
        self._accept_metadata(frame)
        return self._record(frame, ApplyStatus.APPLIED_SNAPSHOT, "fresh snapshot accepted")

    def _apply_delta(self, frame: NormalizedL2Frame) -> FrameDecision:
        if not self._has_snapshot or self._sequence is None:
            return self._record(
                frame,
                ApplyStatus.IGNORED_SNAPSHOT_REQUIRED,
                "connection epoch has no accepted snapshot",
            )
        if frame.sequence <= self._sequence:
            return self._record(
                frame,
                ApplyStatus.IGNORED_OLD_SEQUENCE,
                "delta sequence is duplicate or older than current state",
            )
        if self._tainted:
            return self._record(
                frame,
                ApplyStatus.IGNORED_TAINTED_EPOCH,
                "fresh snapshot is required after an earlier gap or invalid book",
            )
        if self._clock_regresses(frame):
            self._tainted = True
            return self._record(
                frame,
                ApplyStatus.TAINTED_NONCAUSAL_CLOCK,
                "delta clocks regress from current state",
            )
        if frame.previous_sequence != self._sequence:
            self._tainted = True
            return self._record(
                frame,
                ApplyStatus.TAINTED_SEQUENCE_GAP,
                "delta previous_sequence does not link to current sequence",
            )
        if len(frame.bids) > self.max_levels or len(frame.asks) > self.max_levels:
            self._tainted = True
            return self._record(
                frame,
                ApplyStatus.TAINTED_MAX_LEVELS,
                "delta raw rows exceed the hard per-side update limit",
            )

        bids = dict(self._bids)
        asks = dict(self._asks)
        self._update_side(bids, frame.bids)
        self._update_side(asks, frame.asks)
        if len(bids) > self.max_levels or len(asks) > self.max_levels:
            self._tainted = True
            return self._record(
                frame,
                ApplyStatus.TAINTED_MAX_LEVELS,
                "delta would exceed the hard per-side level limit",
            )
        if frame.phase is MarketPhase.CONTINUOUS and self._crossed(bids, asks):
            self._tainted = True
            return self._record(
                frame,
                ApplyStatus.TAINTED_CROSSED_BOOK,
                "continuous delta would lock or cross the book",
            )
        self._bids = bids
        self._asks = asks
        self._accept_metadata(frame)
        return self._record(frame, ApplyStatus.APPLIED_DELTA, "contiguous delta accepted")

    def _accept_metadata(self, frame: NormalizedL2Frame) -> None:
        self._sequence = frame.sequence
        self._phase = frame.phase
        self._exchange_ts = frame.exchange_ts
        self._received_ts = frame.received_ts
        self._monotonic_ns = frame.monotonic_ns
        self._raw_sha256 = frame.raw_sha256
        self._frame_chain_sha256 = self._extend_frame_chain(frame)

    def _clock_regresses(self, frame: NormalizedL2Frame) -> bool:
        return bool(
            frame.exchange_ts < self._exchange_ts
            or frame.received_ts < self._received_ts
            or frame.monotonic_ns < self._monotonic_ns
        )

    def _extend_frame_chain(self, frame: NormalizedL2Frame) -> str:
        digest = hashlib.sha256()
        digest.update(b"premarket-l2-frame-chain-v1\0")
        if self._frame_chain_sha256:
            digest.update(bytes.fromhex(self._frame_chain_sha256))

        def bind_text(value: str) -> None:
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)

        for value in (
            frame.venue,
            frame.symbol,
            frame.kind.value,
            frame.phase.value,
            str(frame.connection_epoch),
            str(frame.sequence),
            "none" if frame.previous_sequence is None else str(frame.previous_sequence),
            frame.exchange_ts.hex(),
            frame.received_ts.hex(),
            str(frame.monotonic_ns),
        ):
            bind_text(value)
        digest.update(bytes.fromhex(frame.raw_sha256))
        for side_name, side in (("bids", self._bids), ("asks", self._asks)):
            bind_text(side_name)
            bind_text(str(len(side)))
            for price in sorted(side, reverse=side_name == "bids"):
                size = side[price]
                bind_text("0" if price == 0 else str(price.normalize()))
                bind_text("0" if size == 0 else str(size.normalize()))
        return digest.hexdigest()

    def _record(
        self,
        frame: NormalizedL2Frame,
        status: ApplyStatus,
        reason: str,
    ) -> FrameDecision:
        decision = FrameDecision(
            status=status,
            reason=reason,
            connection_epoch=frame.connection_epoch,
            sequence=frame.sequence,
            previous_sequence=frame.previous_sequence,
            raw_sha256=frame.raw_sha256,
        )
        self._decisions.append(decision)
        if len(self._decisions) > MAX_DECISIONS_RETAINED:
            del self._decisions[: len(self._decisions) - MAX_DECISIONS_RETAINED]
        return decision

    @staticmethod
    def _levels(rows: Iterable[BookLevel]) -> dict[Decimal, Decimal]:
        return {row.price: row.size for row in rows if row.size > 0}

    @staticmethod
    def _update_side(side: dict[Decimal, Decimal], rows: Iterable[BookLevel]) -> None:
        for row in rows:
            if row.size == 0:
                side.pop(row.price, None)
            else:
                side[row.price] = row.size

    @staticmethod
    def _crossed(
        bids: dict[Decimal, Decimal], asks: dict[Decimal, Decimal]
    ) -> bool:
        return bool(bids and asks and max(bids) >= min(asks))

    @staticmethod
    def _sorted_bids(rows: dict[Decimal, Decimal]) -> tuple[BookLevel, ...]:
        return tuple(
            BookLevel(price=price, size=rows[price]) for price in sorted(rows, reverse=True)
        )

    @staticmethod
    def _sorted_asks(rows: dict[Decimal, Decimal]) -> tuple[BookLevel, ...]:
        return tuple(BookLevel(price=price, size=rows[price]) for price in sorted(rows))
