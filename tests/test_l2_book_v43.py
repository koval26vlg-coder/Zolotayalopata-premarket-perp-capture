"""Pure, venue-normalized L2 state required before an event-bound v43 capture.

These tests deliberately describe an adapter-independent boundary.  Venue adapters may
translate different sequence fields and phases, but they must hand this module complete,
hash-bound frames; this module decides only whether a causal book remains usable.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from decimal import Decimal
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from l2_book import (  # noqa: E402
    ApplyStatus,
    BookLevel,
    FrameKind,
    L2Book,
    MarketPhase,
    NormalizedL2Frame,
)


def raw_hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def level(price: str, size: str) -> BookLevel:
    return BookLevel(price=price, size=size)


def frame(
    *,
    epoch: int,
    sequence: int,
    kind: FrameKind = FrameKind.DELTA,
    previous_sequence: int | None = None,
    phase: MarketPhase = MarketPhase.CONTINUOUS,
    bids: tuple[BookLevel, ...] = (),
    asks: tuple[BookLevel, ...] = (),
    label: str | None = None,
    venue: str = "bybit",
    symbol: str = "NEWUSDT",
) -> NormalizedL2Frame:
    return NormalizedL2Frame(
        venue=venue,
        symbol=symbol,
        kind=kind,
        phase=phase,
        connection_epoch=epoch,
        sequence=sequence,
        previous_sequence=previous_sequence,
        exchange_ts=1_800_000_000.100 + sequence / 1000,
        received_ts=1_800_000_000.200 + sequence / 1000,
        monotonic_ns=9_000_000_000 + sequence,
        bids=bids,
        asks=asks,
        raw_sha256=raw_hash(label or f"{epoch}:{sequence}:{kind.value}"),
    )


def snapshot(
    *,
    epoch: int,
    sequence: int,
    bids: tuple[BookLevel, ...],
    asks: tuple[BookLevel, ...],
    phase: MarketPhase = MarketPhase.CONTINUOUS,
    label: str | None = None,
) -> NormalizedL2Frame:
    return frame(
        epoch=epoch,
        sequence=sequence,
        kind=FrameKind.SNAPSHOT,
        phase=phase,
        bids=bids,
        asks=asks,
        label=label,
    )


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_sorted_and_ready_only_after_connection_epoch_begins(self):
        for venue in ("bybit", "okx", "gate"):
            with self.subTest(venue=venue):
                book = L2Book(venue=venue, symbol="NEWUSDT", max_levels=4)
                book.begin_connection(1)
                source = frame(
                    venue=venue,
                    epoch=1,
                    sequence=10,
                    kind=FrameKind.SNAPSHOT,
                    bids=(level("98", "2"), level("99", "1")),
                    asks=(level("102", "2"), level("101", "1")),
                )

                decision = book.apply(source)
                causal = book.causal_snapshot()

                self.assertEqual(decision.status, ApplyStatus.APPLIED_SNAPSHOT)
                self.assertIsNotNone(causal)
                assert causal is not None
                self.assertEqual([row.price for row in causal.bids], [Decimal("99"), Decimal("98")])
                self.assertEqual([row.price for row in causal.asks], [Decimal("101"), Decimal("102")])
                self.assertTrue(causal.gap_free)
                self.assertTrue(causal.execution_ready)

    def test_causal_snapshot_carries_all_frame_provenance(self):
        book = L2Book(venue="bybit", symbol="NEWUSDT", max_levels=2)
        book.begin_connection(7)
        source = snapshot(
            epoch=7,
            sequence=41,
            bids=(level("99", "1"),),
            asks=(level("101", "1"),),
            label="provenance",
        )

        book.apply(source)
        causal = book.causal_snapshot()

        self.assertIsNotNone(causal)
        assert causal is not None
        self.assertEqual(causal.exchange_ts, source.exchange_ts)
        self.assertEqual(causal.received_ts, source.received_ts)
        self.assertEqual(causal.monotonic_ns, source.monotonic_ns)
        self.assertEqual(causal.connection_epoch, 7)
        self.assertEqual(causal.sequence, 41)
        self.assertTrue(causal.gap_free)
        self.assertEqual(causal.raw_sha256, source.raw_sha256)


class DeltaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = L2Book(venue="bybit", symbol="NEWUSDT", max_levels=4)
        self.book.begin_connection(1)
        self.book.apply(snapshot(
            epoch=1,
            sequence=10,
            bids=(level("99", "2"), level("98", "1")),
            asks=(level("101", "2"), level("102", "1")),
        ))

    def test_contiguous_delta_updates_and_zero_size_deletes(self):
        decision = self.book.apply(frame(
            epoch=1,
            previous_sequence=10,
            sequence=11,
            bids=(level("99", "0"), level("100", "3")),
            asks=(level("101", "4"), level("102", "0")),
        ))

        causal = self.book.causal_snapshot()
        self.assertEqual(decision.status, ApplyStatus.APPLIED_DELTA)
        self.assertIsNotNone(causal)
        assert causal is not None
        self.assertEqual([(row.price, row.size) for row in causal.bids], [
            (Decimal("100"), Decimal("3")),
            (Decimal("98"), Decimal("1")),
        ])
        self.assertEqual([(row.price, row.size) for row in causal.asks], [
            (Decimal("101"), Decimal("4")),
        ])
        self.assertEqual(causal.sequence, 11)
        self.assertTrue(causal.execution_ready)

    def test_duplicate_or_old_sequence_is_ignored_and_recorded(self):
        duplicate = self.book.apply(frame(
            epoch=1,
            previous_sequence=9,
            sequence=10,
            bids=(level("120", "5"),),
            label="duplicate",
        ))

        causal = self.book.causal_snapshot()
        self.assertEqual(duplicate.status, ApplyStatus.IGNORED_OLD_SEQUENCE)
        self.assertEqual(duplicate.raw_sha256, raw_hash("duplicate"))
        self.assertEqual(self.book.decisions[-1], duplicate)
        self.assertIsNotNone(causal)
        assert causal is not None
        self.assertEqual(causal.sequence, 10)
        self.assertEqual(causal.bids[0].price, Decimal("99"))

    def test_sequence_gap_taints_epoch_until_a_fresh_snapshot(self):
        gap = self.book.apply(frame(
            epoch=1,
            previous_sequence=8,
            sequence=12,
            bids=(level("100", "1"),),
            label="gap",
        ))
        ignored = self.book.apply(frame(
            epoch=1,
            previous_sequence=10,
            sequence=13,
            bids=(level("100", "2"),),
            label="after-gap",
        ))

        tainted = self.book.causal_snapshot()
        self.assertEqual(gap.status, ApplyStatus.TAINTED_SEQUENCE_GAP)
        self.assertEqual(ignored.status, ApplyStatus.IGNORED_TAINTED_EPOCH)
        self.assertIsNotNone(tainted)
        assert tainted is not None
        self.assertFalse(tainted.gap_free)
        self.assertFalse(tainted.execution_ready)
        self.assertEqual(tainted.sequence, 10)

        reset = self.book.apply(snapshot(
            epoch=1,
            sequence=20,
            bids=(level("100", "1"),),
            asks=(level("101", "1"),),
            label="fresh",
        ))
        causal = self.book.causal_snapshot()
        self.assertEqual(reset.status, ApplyStatus.APPLIED_SNAPSHOT)
        self.assertIsNotNone(causal)
        assert causal is not None
        self.assertTrue(causal.gap_free)
        self.assertTrue(causal.execution_ready)
        self.assertEqual(causal.sequence, 20)


class EpochTests(unittest.TestCase):
    def test_reconnect_starts_new_epoch_and_requires_a_new_snapshot(self):
        book = L2Book(venue="bybit", symbol="NEWUSDT", max_levels=2)
        book.begin_connection(1)
        book.apply(snapshot(
            epoch=1,
            sequence=10,
            bids=(level("99", "1"),),
            asks=(level("101", "1"),),
        ))

        book.begin_connection(2)
        before_snapshot = book.apply(frame(
            epoch=2,
            previous_sequence=10,
            sequence=11,
            bids=(level("100", "1"),),
        ))
        stale_epoch = book.apply(frame(
            epoch=1,
            previous_sequence=10,
            sequence=11,
            bids=(level("100", "1"),),
            label="stale-epoch",
        ))

        self.assertEqual(before_snapshot.status, ApplyStatus.IGNORED_SNAPSHOT_REQUIRED)
        self.assertEqual(stale_epoch.status, ApplyStatus.IGNORED_STALE_EPOCH)
        self.assertIsNone(book.causal_snapshot())

        book.apply(snapshot(
            epoch=2,
            sequence=12,
            bids=(level("100", "1"),),
            asks=(level("101", "1"),),
        ))
        causal = book.causal_snapshot()
        self.assertIsNotNone(causal)
        assert causal is not None
        self.assertEqual(causal.connection_epoch, 2)
        self.assertTrue(causal.execution_ready)


class PhaseAndBoundsTests(unittest.TestCase):
    def test_crossed_preopen_book_is_preserved_but_never_execution_ready(self):
        book = L2Book(venue="okx", symbol="NEW-USDT-SWAP", max_levels=2)
        book.begin_connection(1)

        decision = book.apply(frame(
            venue="okx",
            symbol="NEW-USDT-SWAP",
            epoch=1,
            sequence=1,
            kind=FrameKind.SNAPSHOT,
            phase=MarketPhase.PREOPEN,
            bids=(level("101", "1"),),
            asks=(level("100", "1"),),
        ))

        causal = book.causal_snapshot()
        self.assertEqual(decision.status, ApplyStatus.APPLIED_SNAPSHOT)
        self.assertIsNotNone(causal)
        assert causal is not None
        self.assertTrue(causal.gap_free)
        self.assertFalse(causal.execution_ready)
        self.assertGreaterEqual(causal.bids[0].price, causal.asks[0].price)

    def test_crossed_continuous_book_taints_epoch_until_fresh_snapshot(self):
        book = L2Book(venue="okx", symbol="NEW-USDT-SWAP", max_levels=2)
        book.begin_connection(1)

        decision = book.apply(frame(
            venue="okx",
            symbol="NEW-USDT-SWAP",
            epoch=1,
            sequence=1,
            kind=FrameKind.SNAPSHOT,
            bids=(level("101", "1"),),
            asks=(level("100", "1"),),
        ))

        self.assertEqual(decision.status, ApplyStatus.TAINTED_CROSSED_BOOK)
        self.assertIsNone(book.causal_snapshot())

    def test_snapshot_exceeding_hard_level_limit_is_rejected(self):
        book = L2Book(venue="gate", symbol="NEW_USDT", max_levels=2)
        book.begin_connection(1)

        decision = book.apply(frame(
            venue="gate",
            symbol="NEW_USDT",
            epoch=1,
            sequence=1,
            kind=FrameKind.SNAPSHOT,
            bids=(level("99", "1"), level("98", "1"), level("97", "1")),
            asks=(level("101", "1"),),
        ))

        self.assertEqual(decision.status, ApplyStatus.TAINTED_MAX_LEVELS)
        self.assertIsNone(book.causal_snapshot())

    def test_delta_cannot_expand_stored_book_beyond_hard_limit(self):
        book = L2Book(venue="gate", symbol="NEW_USDT", max_levels=2)
        book.begin_connection(1)
        book.apply(frame(
            venue="gate",
            symbol="NEW_USDT",
            epoch=1,
            sequence=1,
            kind=FrameKind.SNAPSHOT,
            bids=(level("99", "1"), level("98", "1")),
            asks=(level("101", "1"),),
        ))

        decision = book.apply(frame(
            venue="gate",
            symbol="NEW_USDT",
            epoch=1,
            previous_sequence=1,
            sequence=2,
            bids=(level("97", "1"),),
        ))

        causal = book.causal_snapshot()
        self.assertEqual(decision.status, ApplyStatus.TAINTED_MAX_LEVELS)
        self.assertIsNotNone(causal)
        assert causal is not None
        self.assertFalse(causal.gap_free)
        self.assertFalse(causal.execution_ready)
        self.assertEqual(len(causal.bids), 2)


if __name__ == "__main__":
    unittest.main()
