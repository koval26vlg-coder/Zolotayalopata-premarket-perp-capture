"""Contract tests for the pure offline Bybit REST/WS full-book synchronizer."""

from __future__ import annotations

import ast
import hashlib
import json
import sys
import unittest
from dataclasses import FrozenInstanceError, fields
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import bybit_full_book_v43 as full_book  # noqa: E402


CONTRACT = "ABCUSDT"
BASE_MS = 1_800_000_000_000


def encode(value: object, *, spaced: bool = False) -> bytes:
    separators = None if spaced else (",", ":")
    return json.dumps(value, separators=separators).encode("utf-8")


def rest_raw(
    *,
    update_id: int = 100,
    cross_sequence: int = 900,
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
    response_ts_ms: int = BASE_MS + 100,
) -> bytes:
    return encode(
        {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "s": CONTRACT,
                "b": bids if bids is not None else [["10", "2"], ["9", "3"]],
                "a": asks if asks is not None else [["11", "4"], ["12", "5"]],
                "ts": BASE_MS + 90,
                "cts": BASE_MS + 80,
                "u": update_id,
                "seq": cross_sequence,
            },
            "retExtInfo": {},
            "time": response_ts_ms,
        }
    )


def delta_raw(
    *,
    update_id: int,
    cross_sequence: int,
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
    gateway_ts_ms: int | None = None,
) -> bytes:
    gateway = gateway_ts_ms if gateway_ts_ms is not None else BASE_MS + update_id
    return encode(
        {
            "topic": f"orderbook.full.{CONTRACT}",
            "type": "delta",
            "ts": gateway,
            "data": {
                "s": CONTRACT,
                "b": bids if bids is not None else [],
                "a": asks if asks is not None else [],
                "u": update_id,
                "seq": cross_sequence,
            },
            "cts": gateway - 1,
        }
    )


def parse_rest(raw: bytes | None = None, *, monotonic_ns: int = 2_000) -> full_book.BybitFullBookRestSnapshot:
    return full_book.BybitFullBookRestSnapshot.from_raw(
        rest_raw() if raw is None else raw,
        received_ts=(BASE_MS + 700) / 1000,
        monotonic_ns=monotonic_ns,
        contract=CONTRACT,
        request_path=full_book.FULL_ORDERBOOK_REST_PATH,
        request_category=full_book.FULL_ORDERBOOK_REST_CATEGORY,
    )


def parse_delta(
    *,
    update_id: int,
    cross_sequence: int,
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
    monotonic_ns: int | None = None,
    raw: bytes | None = None,
) -> full_book.BybitFullBookDelta:
    payload = raw if raw is not None else delta_raw(
        update_id=update_id,
        cross_sequence=cross_sequence,
        bids=bids,
        asks=asks,
    )
    return full_book.BybitFullBookDelta.from_raw(
        payload,
        received_ts=(BASE_MS + 500 + update_id) / 1000,
        monotonic_ns=monotonic_ns if monotonic_ns is not None else 1_000 + update_id,
        contract=CONTRACT,
        connection_path=full_book.FULL_ORDERBOOK_WS_PATH,
        connection_category=full_book.FULL_ORDERBOOK_WS_CATEGORY,
    )


def synchronizer(**overrides: object) -> full_book.BybitFullBookSynchronizer:
    options: dict[str, object] = {
        "symbol": CONTRACT,
        "max_levels_per_side": 1_000,
        "max_buffered_deltas": 20,
        "max_buffered_bytes": 1_000_000,
        "max_buffer_age_ms": 10_000,
        "max_snapshot_attempts": 3,
        "max_resync_attempts": 3,
    }
    options.update(overrides)
    return full_book.BybitFullBookSynchronizer(**options)


def issue_and_ingest(
    sync: full_book.BybitFullBookSynchronizer,
    snapshot: full_book.BybitFullBookRestSnapshot,
    *,
    epoch: int,
) -> bool:
    attempt = sync.issue_rest_attempt(
        epoch=epoch,
        issued_received_ts=snapshot.received_ts - 0.001,
        issued_monotonic_ns=snapshot.monotonic_ns - 1,
    )
    return sync.ingest_rest_snapshot(snapshot, epoch=epoch, attempt=attempt)


class RawSchemaTests(unittest.TestCase):
    def test_rest_and_delta_are_frozen_and_preserve_exact_raw_hash(self) -> None:
        raw_snapshot = rest_raw()
        raw_delta = delta_raw(update_id=100, cross_sequence=900)
        snapshot = parse_rest(raw_snapshot)
        delta = parse_delta(update_id=100, cross_sequence=900, raw=raw_delta)

        self.assertEqual(snapshot.raw_bytes, raw_snapshot)
        self.assertEqual(snapshot.raw_sha256, hashlib.sha256(raw_snapshot).hexdigest())
        self.assertEqual((snapshot.update_id, snapshot.cross_sequence), (100, 900))
        self.assertEqual(snapshot.exchange_ts_ms, BASE_MS + 80)
        self.assertEqual(delta.raw_bytes, raw_delta)
        self.assertEqual(delta.raw_sha256, hashlib.sha256(raw_delta).hexdigest())
        self.assertEqual((delta.update_id, delta.cross_sequence), (100, 900))
        with self.assertRaises(FrozenInstanceError):
            snapshot.update_id = 101  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            delta.update_id = 101  # type: ignore[misc]

    def test_raw_input_is_strict_utf8_json_with_exact_contract_and_channel(self) -> None:
        malformed_inputs = (
            b"\xff",
            b'{"retCode":0} trailing',
            b'{"retCode":0,"retCode":0}',
        )
        for raw in malformed_inputs:
            with self.subTest(raw=raw), self.assertRaises(full_book.BybitFullBookSchemaError):
                full_book.BybitFullBookRestSnapshot.from_raw(
                    raw,
                    received_ts=(BASE_MS + 200) / 1000,
                    monotonic_ns=2_000,
                    contract=CONTRACT,
                    request_path=full_book.FULL_ORDERBOOK_REST_PATH,
                    request_category=full_book.FULL_ORDERBOOK_REST_CATEGORY,
                )

        wrong_symbol = json.loads(rest_raw())
        wrong_symbol["result"]["s"] = "WRONGUSDT"
        with self.assertRaises(full_book.BybitFullBookSchemaError):
            parse_rest(encode(wrong_symbol))

        wrong_topic = json.loads(delta_raw(update_id=100, cross_sequence=900))
        wrong_topic["topic"] = f"orderbook.1000.{CONTRACT}"
        with self.assertRaises(full_book.BybitFullBookSchemaError):
            parse_delta(update_id=100, cross_sequence=900, raw=encode(wrong_topic))

        with self.assertRaises(full_book.BybitFullBookSchemaError):
            full_book.BybitFullBookDelta.from_raw(
                "not-bytes",  # type: ignore[arg-type]
                received_ts=(BASE_MS + 500) / 1000,
                monotonic_ns=1,
                contract=CONTRACT,
                connection_path=full_book.FULL_ORDERBOOK_WS_PATH,
                connection_category=full_book.FULL_ORDERBOOK_WS_CATEGORY,
            )

        with self.assertRaises(full_book.BybitFullBookSchemaError):
            full_book.BybitFullBookDelta.from_raw(
                delta_raw(update_id=100, cross_sequence=900),
                received_ts=(BASE_MS + 500) / 1000,
                monotonic_ns=1,
                contract=CONTRACT,
                connection_path="/v5/public/spot",
                connection_category="spot",
            )

    def test_exchange_clock_skew_is_retained_but_not_used_as_local_causal_order(self) -> None:
        snapshot = full_book.BybitFullBookRestSnapshot.from_raw(
            rest_raw(response_ts_ms=BASE_MS + 100),
            received_ts=(BASE_MS + 99) / 1000,
            monotonic_ns=2_000,
            contract=CONTRACT,
            request_path=full_book.FULL_ORDERBOOK_REST_PATH,
            request_category=full_book.FULL_ORDERBOOK_REST_CATEGORY,
        )
        self.assertLess(Decimal(snapshot.source_clock_skew_ms), 0)
        raw = delta_raw(
            update_id=100,
            cross_sequence=900,
            gateway_ts_ms=BASE_MS + 100,
        )
        delta = full_book.BybitFullBookDelta.from_raw(
            raw,
            received_ts=(BASE_MS + 99) / 1000,
            monotonic_ns=1_000,
            contract=CONTRACT,
            connection_path=full_book.FULL_ORDERBOOK_WS_PATH,
            connection_category=full_book.FULL_ORDERBOOK_WS_CATEGORY,
        )
        self.assertLess(Decimal(delta.source_clock_skew_ms), 0)

    def test_levels_are_decimal_strings_unique_and_side_appropriate(self) -> None:
        bad_levels = (
            ([['0', '1']], [['11', '1']]),
            ([['10', '-1']], [['11', '1']]),
            ([['10', '1'], ['10.0', '2']], [['11', '1']]),
            ([['10', '1']], [['nan', '1']]),
        )
        for bids, asks in bad_levels:
            with self.subTest(bids=bids, asks=asks), self.assertRaises(
                full_book.BybitFullBookSchemaError
            ):
                parse_rest(rest_raw(bids=bids, asks=asks))


class SynchronizationTests(unittest.TestCase):
    @staticmethod
    def _forged_copy(value: object, **overrides: object) -> object:
        forged = object.__new__(type(value))
        for field in fields(value):
            object.__setattr__(
                forged,
                field.name,
                overrides.get(field.name, getattr(value, field.name)),
            )
        return forged

    def test_rest_attempt_is_bound_to_exact_epoch_generation_and_one_response(self) -> None:
        sync = synchronizer()
        sync.begin_epoch(1)
        stale = sync.issue_rest_attempt(
            epoch=1,
            issued_received_ts=(BASE_MS + 100) / 1000,
            issued_monotonic_ns=1_000,
        )
        self.assertEqual((stale.epoch, stale.generation, stale.attempt), (1, 1, 1))
        self.assertEqual(stale.request_path, full_book.FULL_ORDERBOOK_REST_PATH)
        self.assertEqual(stale.request_category, full_book.FULL_ORDERBOOK_REST_CATEGORY)
        self.assertEqual(stale.request_symbol, CONTRACT)
        sync.reconnect(2)
        sync.ingest_delta(parse_delta(update_id=100, cross_sequence=900), epoch=2)
        with self.assertRaises(full_book.BybitFullBookEpochError):
            sync.ingest_rest_snapshot(parse_rest(), epoch=2, attempt=stale)

        current = sync.issue_rest_attempt(
            epoch=2,
            issued_received_ts=(BASE_MS + 100) / 1000,
            issued_monotonic_ns=1_500,
        )
        self.assertEqual((current.epoch, current.generation, current.attempt), (2, 2, 1))
        self.assertTrue(sync.ingest_rest_snapshot(parse_rest(), epoch=2, attempt=current))
        with self.assertRaises(full_book.BybitFullBookStateError):
            sync.ingest_rest_snapshot(parse_rest(), epoch=2, attempt=current)

    def test_rest_origin_is_exact_and_forged_typed_evidence_is_reparsed(self) -> None:
        wrong_connection = synchronizer()
        with self.assertRaises(full_book.BybitFullBookSchemaError):
            wrong_connection.begin_epoch(
                1,
                connection_path="/v5/public/spot",
                connection_category="spot",
            )

        with self.assertRaises(full_book.BybitFullBookSchemaError):
            full_book.BybitFullBookRestSnapshot.from_raw(
                rest_raw(),
                received_ts=(BASE_MS + 700) / 1000,
                monotonic_ns=2_000,
                contract=CONTRACT,
                request_path="/v5/market/orderbook",
                request_category="linear",
            )
        with self.assertRaises(full_book.BybitFullBookSchemaError):
            full_book.BybitFullBookRestSnapshot.from_raw(
                rest_raw(),
                received_ts=(BASE_MS + 700) / 1000,
                monotonic_ns=2_000,
                contract=CONTRACT,
                request_path=full_book.FULL_ORDERBOOK_REST_PATH,
                request_category="spot",
            )

        valid_delta = parse_delta(update_id=100, cross_sequence=900)
        forged_delta = self._forged_copy(
            valid_delta,
            raw_bytes=b"NOT JSON",
            raw_sha256=hashlib.sha256(b"NOT JSON").hexdigest(),
        )
        sync = synchronizer()
        sync.begin_epoch(1)
        with self.assertRaises(full_book.BybitFullBookSchemaError):
            sync.ingest_delta(forged_delta, epoch=1)

        sync = synchronizer()
        sync.begin_epoch(1)
        sync.ingest_delta(valid_delta, epoch=1)
        attempt = sync.issue_rest_attempt(
            epoch=1,
            issued_received_ts=(BASE_MS + 100) / 1000,
            issued_monotonic_ns=1_500,
        )
        valid_snapshot = parse_rest()
        forged_snapshot = self._forged_copy(
            valid_snapshot,
            raw_bytes=b"NOT JSON",
            raw_sha256=hashlib.sha256(b"NOT JSON").hexdigest(),
        )
        with self.assertRaises(full_book.BybitFullBookSchemaError):
            sync.ingest_rest_snapshot(forged_snapshot, epoch=1, attempt=attempt)

    def test_accepted_delta_is_detached_from_caller_alias_before_anchor(self) -> None:
        bridge = parse_delta(update_id=100, cross_sequence=900)
        sync = synchronizer()
        sync.begin_epoch(1)
        self.assertEqual(sync.ingest_delta(bridge, epoch=1), "BUFFERED")

        object.__setattr__(bridge, "update_id", 999)
        object.__setattr__(bridge, "bids", (("999", "999"),))
        object.__setattr__(bridge, "raw_bytes", b"caller-mutated-after-acceptance")
        object.__setattr__(bridge, "raw_sha256", "f" * 64)

        self.assertTrue(issue_and_ingest(sync, parse_rest(), epoch=1))
        view = sync.causal_snapshot()
        self.assertEqual((view.update_id, view.cross_sequence), (100, 900))
        self.assertEqual(view.bids[0], ("10", "2"))
        self.assertRegex(view.evidence_chain_sha256 or "", r"^[0-9a-f]{64}$")

    def test_pending_rest_snapshot_is_detached_before_later_ws_bridge(self) -> None:
        sync = synchronizer()
        sync.begin_epoch(1)
        sync.ingest_delta(parse_delta(update_id=99, cross_sequence=899), epoch=1)
        pending = parse_rest(rest_raw(update_id=100, cross_sequence=900))
        attempt = sync.issue_rest_attempt(
            epoch=1,
            issued_received_ts=pending.received_ts - 0.001,
            issued_monotonic_ns=pending.monotonic_ns - 1,
        )
        self.assertFalse(
            sync.ingest_rest_snapshot(pending, epoch=1, attempt=attempt)
        )

        object.__setattr__(pending, "update_id", 777)
        object.__setattr__(pending, "bids", (("777", "7"),))
        object.__setattr__(pending, "raw_bytes", b"caller-mutated-pending-snapshot")
        object.__setattr__(pending, "raw_sha256", "e" * 64)

        self.assertEqual(
            sync.ingest_delta(
                parse_delta(
                    update_id=100,
                    cross_sequence=900,
                    monotonic_ns=2_100,
                ),
                epoch=1,
            ),
            "SYNCHRONIZED",
        )
        view = sync.causal_snapshot()
        self.assertEqual((view.update_id, view.cross_sequence), (100, 900))
        self.assertEqual(view.bids[0], ("10", "2"))

    def test_mutated_issued_rest_attempt_fails_closed_and_cannot_be_repaired(self) -> None:
        sync = synchronizer()
        sync.begin_epoch(1)
        sync.ingest_delta(parse_delta(update_id=100, cross_sequence=900), epoch=1)
        attempt = sync.issue_rest_attempt(
            epoch=1,
            issued_received_ts=(BASE_MS + 100) / 1000,
            issued_monotonic_ns=1_500,
        )
        object.__setattr__(attempt, "issued_monotonic_ns", 9_999)

        with self.assertRaises(full_book.BybitFullBookError) as caught:
            sync.ingest_rest_snapshot(parse_rest(), epoch=1, attempt=attempt)
        self.assertIsInstance(caught.exception, full_book.BybitFullBookStateError)
        self.assertRegex(str(caught.exception), "mutat|canonical")
        view = sync.causal_snapshot()
        self.assertTrue(view.resync_required)
        self.assertEqual(view.invalidation_reason, "REST_ATTEMPT_MUTATED")

        object.__setattr__(attempt, "issued_monotonic_ns", 1_500)
        with self.assertRaises(full_book.BybitFullBookStateError):
            sync.ingest_rest_snapshot(parse_rest(), epoch=1, attempt=attempt)

    def test_anchor_delta_proves_bridge_but_is_not_applied_over_rest_snapshot(self) -> None:
        sync = synchronizer()
        sync.begin_epoch(1)
        bridge = parse_delta(
            update_id=100,
            cross_sequence=900,
            bids=[["10", "999"]],
        )
        sync.ingest_delta(bridge, epoch=1)
        self.assertTrue(issue_and_ingest(sync, parse_rest(), epoch=1))
        self.assertEqual(sync.causal_snapshot().bids[0], ("10", "2"))

    def test_buffers_before_rest_matches_exact_seq_u_then_applies_later_deltas(self) -> None:
        sync = synchronizer(max_levels_per_side=2)
        sync.begin_epoch(1)
        self.assertEqual(
            sync.ingest_delta(
                parse_delta(update_id=100, cross_sequence=900, bids=[["10", "2.5"]]),
                epoch=1,
            ),
            "BUFFERED",
        )
        self.assertEqual(
            sync.ingest_delta(
                parse_delta(
                    update_id=101,
                    cross_sequence=905,
                    bids=[["10", "0"], ["10.5", "7"]],
                    asks=[["11", "3"]],
                ),
                epoch=1,
            ),
            "BUFFERED",
        )

        self.assertTrue(issue_and_ingest(sync, parse_rest(), epoch=1))
        view = sync.causal_snapshot()
        self.assertTrue(view.synchronized)
        self.assertTrue(view.book_structurally_ready)
        self.assertFalse(view.execution_ready)
        self.assertFalse(view.resync_required)
        self.assertEqual((view.update_id, view.cross_sequence), (101, 905))
        self.assertEqual(view.bids, (("10.5", "7"), ("9", "3")))
        self.assertEqual(view.asks, (("11", "3"), ("12", "5")))
        self.assertEqual(
            view.continuity_basis,
            "REST_EXACT_SEQ_U_BRIDGE_THEN_WS_U_PLUS_ONE_SEQ_NONDECREASING",
        )
        self.assertEqual(view.known_depth_limit_per_side, 2)
        self.assertEqual(
            view.known_depth_limitation,
            "BOUNDED_LEVELS_PER_SIDE_NOT_COMPLETE_MARKET_DEPTH",
        )
        self.assertEqual(view.rpi_coverage, "RPI_EXCLUDED_BY_BYBIT_PUBLIC_API")
        self.assertEqual(view.connection_path, "/v5/public/linear")
        self.assertEqual(view.connection_category, "linear")
        self.assertEqual(
            view.connection_provenance,
            "UNBOUND_EXACT_CONNECTION_DECLARATION",
        )
        self.assertEqual(view.evidence_record_count, 3)
        self.assertEqual(view.received_ts, (BASE_MS + 700) / 1000)
        self.assertEqual(view.monotonic_ns, 2_000)

    def test_mismatched_rest_snapshot_is_bounded_retry_and_never_ready(self) -> None:
        sync = synchronizer(max_snapshot_attempts=2)
        sync.begin_epoch(1)
        sync.ingest_delta(parse_delta(update_id=101, cross_sequence=901), epoch=1)

        self.assertFalse(
            issue_and_ingest(
                sync, parse_rest(rest_raw(update_id=99, cross_sequence=899)), epoch=1
            )
        )
        self.assertFalse(sync.causal_snapshot().execution_ready)
        self.assertFalse(
            issue_and_ingest(
                sync,
                parse_rest(rest_raw(update_id=100, cross_sequence=900), monotonic_ns=2_001),
                epoch=1,
            )
        )
        exhausted = sync.causal_snapshot()
        self.assertEqual(exhausted.status, "RETRY_EXHAUSTED")
        self.assertTrue(exhausted.resync_required)
        self.assertFalse(exhausted.synchronized)
        with self.assertRaises(full_book.BybitFullBookStateError):
            issue_and_ingest(sync, parse_rest(monotonic_ns=2_002), epoch=1)

    def test_u_gap_invalidates_and_clears_book_until_new_epoch(self) -> None:
        sync = self._ready_sync()
        self.assertEqual(
            sync.ingest_delta(parse_delta(update_id=102, cross_sequence=902), epoch=1),
            "INVALIDATED",
        )
        view = sync.causal_snapshot()
        self.assertEqual(view.status, "RESYNC_REQUIRED")
        self.assertFalse(view.execution_ready)
        self.assertEqual((view.bids, view.asks), ((), ()))
        with self.assertRaises(full_book.BybitFullBookStateError):
            sync.ingest_delta(parse_delta(update_id=101, cross_sequence=901), epoch=1)

    def test_cross_sequence_regression_invalidates_even_when_u_is_contiguous(self) -> None:
        sync = self._ready_sync()
        self.assertEqual(
            sync.ingest_delta(parse_delta(update_id=101, cross_sequence=899), epoch=1),
            "INVALIDATED",
        )
        self.assertTrue(sync.causal_snapshot().resync_required)

    def test_u_one_can_anchor_new_generation_but_invalidates_a_ready_book(self) -> None:
        sync = synchronizer()
        sync.begin_epoch(1)
        sync.ingest_delta(parse_delta(update_id=1, cross_sequence=10), epoch=1)
        one_snapshot = parse_rest(rest_raw(update_id=1, cross_sequence=10))
        self.assertTrue(issue_and_ingest(sync, one_snapshot, epoch=1))
        self.assertTrue(sync.causal_snapshot().book_structurally_ready)
        self.assertFalse(sync.causal_snapshot().execution_ready)

        self.assertEqual(
            sync.ingest_delta(parse_delta(update_id=1, cross_sequence=11), epoch=1),
            "INVALIDATED",
        )
        view = sync.causal_snapshot()
        self.assertTrue(view.resync_required)
        self.assertEqual(view.invalidation_reason, "WS_U_ONE_RESET")

    def test_rest_ahead_of_buffer_waits_boundedly_for_exact_bridge(self) -> None:
        sync = synchronizer()
        sync.begin_epoch(1)
        sync.ingest_delta(parse_delta(update_id=99, cross_sequence=899), epoch=1)
        future = parse_rest(rest_raw(update_id=100, cross_sequence=900))
        self.assertFalse(issue_and_ingest(sync, future, epoch=1))
        waiting = sync.causal_snapshot()
        self.assertEqual(waiting.status, "WAITING_FOR_BRIDGE")
        self.assertFalse(waiting.execution_ready)
        self.assertEqual(
            sync.ingest_delta(
                parse_delta(
                    update_id=100,
                    cross_sequence=900,
                    monotonic_ns=2_100,
                ),
                epoch=1,
            ),
            "SYNCHRONIZED",
        )
        anchored = sync.causal_snapshot()
        self.assertTrue(anchored.book_structurally_ready)
        self.assertFalse(anchored.execution_ready)
        self.assertEqual(anchored.monotonic_ns, 2_100)

    def test_exact_duplicate_is_ignored_but_conflicting_duplicate_invalidates(self) -> None:
        sync = synchronizer()
        sync.begin_epoch(1)
        first = parse_delta(update_id=100, cross_sequence=900)
        self.assertEqual(sync.ingest_delta(first, epoch=1), "BUFFERED")
        self.assertEqual(sync.ingest_delta(first, epoch=1), "DUPLICATE")
        conflict = parse_delta(
            update_id=100,
            cross_sequence=900,
            bids=[["10", "9"]],
            monotonic_ns=1_101,
        )
        self.assertEqual(sync.ingest_delta(conflict, epoch=1), "INVALIDATED")
        self.assertTrue(sync.causal_snapshot().resync_required)

    def test_reconnect_never_exposes_a_book_from_the_old_epoch(self) -> None:
        sync = self._ready_sync()
        old_chain = sync.causal_snapshot().evidence_chain_sha256
        sync.reconnect(2)
        view = sync.causal_snapshot()
        self.assertEqual(view.epoch, 2)
        self.assertEqual((view.bids, view.asks), ((), ()))
        self.assertFalse(view.execution_ready)
        self.assertNotEqual(view.evidence_chain_sha256, old_chain)
        with self.assertRaises(full_book.BybitFullBookEpochError):
            sync.ingest_delta(parse_delta(update_id=101, cross_sequence=901), epoch=1)

    def test_resync_and_buffer_limits_are_bounded(self) -> None:
        sync = synchronizer(max_resync_attempts=2, max_buffered_deltas=1)
        sync.begin_epoch(1)
        sync.ingest_delta(parse_delta(update_id=100, cross_sequence=900), epoch=1)
        self.assertEqual(
            sync.ingest_delta(parse_delta(update_id=101, cross_sequence=901), epoch=1),
            "INVALIDATED",
        )
        sync.reconnect(2)
        with self.assertRaises(full_book.BybitFullBookRetryExhausted):
            sync.reconnect(3)
        self.assertEqual(sync.causal_snapshot().status, "RETRY_EXHAUSTED")

    def test_buffer_is_bounded_by_bytes_and_causal_age(self) -> None:
        raw = delta_raw(update_id=100, cross_sequence=900)
        by_bytes = synchronizer(max_buffered_bytes=len(raw) - 1)
        by_bytes.begin_epoch(1)
        self.assertEqual(
            by_bytes.ingest_delta(
                parse_delta(update_id=100, cross_sequence=900, raw=raw), epoch=1
            ),
            "INVALIDATED",
        )
        self.assertEqual(
            by_bytes.causal_snapshot().invalidation_reason, "BUFFER_BYTES_EXCEEDED"
        )

        by_age = synchronizer(max_buffer_age_ms=1)
        by_age.begin_epoch(1)
        first = parse_delta(update_id=100, cross_sequence=900)
        later_raw = delta_raw(
            update_id=101,
            cross_sequence=901,
            gateway_ts_ms=BASE_MS + 101,
        )
        later = full_book.BybitFullBookDelta.from_raw(
            later_raw,
            received_ts=first.received_ts,
            monotonic_ns=first.monotonic_ns + 2_000_000,
            contract=CONTRACT,
            connection_path=full_book.FULL_ORDERBOOK_WS_PATH,
            connection_category=full_book.FULL_ORDERBOOK_WS_CATEGORY,
        )
        by_age.ingest_delta(first, epoch=1)
        self.assertEqual(by_age.ingest_delta(later, epoch=1), "INVALIDATED")
        self.assertEqual(
            by_age.causal_snapshot().invalidation_reason, "BUFFER_AGE_EXCEEDED"
        )

    def test_crossed_or_empty_book_is_never_execution_ready(self) -> None:
        sync = synchronizer()
        sync.begin_epoch(1)
        sync.ingest_delta(parse_delta(update_id=100, cross_sequence=900), epoch=1)
        crossed = parse_rest(
            rest_raw(bids=[["11", "2"]], asks=[["10", "2"]])
        )
        self.assertFalse(issue_and_ingest(sync, crossed, epoch=1))
        view = sync.causal_snapshot()
        self.assertFalse(view.execution_ready)
        self.assertTrue(view.resync_required)

    def test_evidence_chain_is_deterministic_for_the_same_raw_evidence(self) -> None:
        chains = []
        for _ in range(2):
            sync = synchronizer()
            sync.begin_epoch(7)
            sync.ingest_delta(parse_delta(update_id=100, cross_sequence=900), epoch=7)
            sync.ingest_delta(parse_delta(update_id=101, cross_sequence=901), epoch=7)
            self.assertTrue(issue_and_ingest(sync, parse_rest(), epoch=7))
            chains.append(sync.causal_snapshot().evidence_chain_sha256)
        self.assertEqual(chains[0], chains[1])
        self.assertRegex(chains[0] or "", r"^[0-9a-f]{64}$")

    def test_evidence_chain_binds_local_receive_clocks(self) -> None:
        chains = []
        for bridge_monotonic_ns in (1_100, 1_101):
            sync = synchronizer()
            sync.begin_epoch(7)
            bridge = parse_delta(
                update_id=100,
                cross_sequence=900,
                monotonic_ns=bridge_monotonic_ns,
            )
            sync.ingest_delta(bridge, epoch=7)
            snapshot = parse_rest(monotonic_ns=2_000)
            self.assertTrue(issue_and_ingest(sync, snapshot, epoch=7))
            chains.append(sync.causal_snapshot().evidence_chain_sha256)

        self.assertNotEqual(chains[0], chains[1])

    def test_evidence_chain_binds_rest_attempt_identity_and_clocks(self) -> None:
        chains = []
        for issued_monotonic_ns in (1_500, 1_501):
            sync = synchronizer()
            sync.begin_epoch(7)
            sync.ingest_delta(parse_delta(update_id=100, cross_sequence=900), epoch=7)
            attempt = sync.issue_rest_attempt(
                epoch=7,
                issued_received_ts=(BASE_MS + 100) / 1000,
                issued_monotonic_ns=issued_monotonic_ns,
            )
            snapshot = parse_rest(monotonic_ns=2_000)
            self.assertTrue(
                sync.ingest_rest_snapshot(snapshot, epoch=7, attempt=attempt)
            )
            chains.append(sync.causal_snapshot().evidence_chain_sha256)

        self.assertNotEqual(chains[0], chains[1])

    def test_constructor_bounds_depth_and_retry_arguments(self) -> None:
        invalid = (
            {"max_levels_per_side": 0},
            {"max_levels_per_side": 10_001},
            {"max_buffered_deltas": 0},
            {"max_buffered_bytes": 0},
            {"max_buffer_age_ms": 0},
            {"max_snapshot_attempts": 0},
            {"max_resync_attempts": 0},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                synchronizer(**arguments)

    def test_raw_snapshot_rejects_more_than_ten_thousand_levels_per_side(self) -> None:
        accepted_bids = [[str(20_000 - index), "1"] for index in range(10_000)]
        accepted = parse_rest(rest_raw(bids=accepted_bids, asks=[["30000", "1"]]))
        self.assertEqual(len(accepted.bids), 10_000)

        bids = [[str(20_000 - index), "1"] for index in range(10_001)]
        with self.assertRaises(full_book.BybitFullBookSchemaError):
            parse_rest(rest_raw(bids=bids, asks=[["30000", "1"]]))

    def _ready_sync(self) -> full_book.BybitFullBookSynchronizer:
        sync = synchronizer()
        sync.begin_epoch(1)
        sync.ingest_delta(parse_delta(update_id=100, cross_sequence=900), epoch=1)
        self.assertTrue(issue_and_ingest(sync, parse_rest(), epoch=1))
        return sync


class CapabilityBoundaryTests(unittest.TestCase):
    def test_module_has_no_network_filesystem_plan_token_claim_or_order_capability(self) -> None:
        source = (SRC / "bybit_full_book_v43.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports: set[str] = set()
        calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)

        self.assertTrue(
            imports.isdisjoint(
                {
                    "socket",
                    "ssl",
                    "urllib",
                    "requests",
                    "httpx",
                    "websocket",
                    "pathlib",
                    "subprocess",
                    "risk_gate",
                    "project_config",
                    "global_market_writer_claim",
                }
            )
        )
        self.assertTrue(
            calls.isdisjoint(
                {
                    "open",
                    "write",
                    "write_text",
                    "write_bytes",
                    "unlink",
                    "replace",
                    "request",
                    "urlopen",
                    "create_connection",
                    "place_order",
                    "submit_order",
                }
            )
        )
        lowered = source.lower()
        for forbidden in ("capture_token", "writer_claim", "private api", "place_order"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)


if __name__ == "__main__":
    unittest.main()
