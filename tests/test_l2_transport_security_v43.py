"""RED security contract for the future v43 L2 transport boundary.

These tests are deliberately networkless.  They reproduce the P1 findings from
the read-only transport audit without changing either implementation module.
"""

from __future__ import annotations

import hashlib
import inspect
import socket
import ssl
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import public_ws  # noqa: E402
from l2_book import (  # noqa: E402
    ApplyStatus,
    BookLevel,
    FrameKind,
    L2Book,
    MarketPhase,
    NormalizedL2Frame,
)


VENUE = "bybit"
SYMBOL = "NEWUSDT"
DECISION_RETENTION_LIMIT = 1_024
CONTROL_FRAME_BUDGET = 1_024


def raw_hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def level(price: str, size: str) -> BookLevel:
    return BookLevel(price=price, size=size)


def frame(
    *,
    sequence: int,
    previous_sequence: int | None = None,
    kind: FrameKind = FrameKind.DELTA,
    exchange_ts: float,
    received_ts: float,
    monotonic_ns: int,
    bids: tuple[BookLevel, ...] = (),
    asks: tuple[BookLevel, ...] = (),
    label: str,
) -> NormalizedL2Frame:
    return NormalizedL2Frame(
        venue=VENUE,
        symbol=SYMBOL,
        kind=kind,
        phase=MarketPhase.CONTINUOUS,
        connection_epoch=1,
        sequence=sequence,
        previous_sequence=previous_sequence,
        exchange_ts=exchange_ts,
        received_ts=received_ts,
        monotonic_ns=monotonic_ns,
        bids=bids,
        asks=asks,
        raw_sha256=raw_hash(label),
    )


def snapshot(
    *,
    sequence: int,
    exchange_ts: float,
    received_ts: float,
    monotonic_ns: int,
    bids: tuple[BookLevel, ...] = (level("99", "1"),),
    asks: tuple[BookLevel, ...] = (level("101", "1"),),
    label: str,
) -> NormalizedL2Frame:
    return frame(
        sequence=sequence,
        kind=FrameKind.SNAPSHOT,
        exchange_ts=exchange_ts,
        received_ts=received_ts,
        monotonic_ns=monotonic_ns,
        bids=bids,
        asks=asks,
        label=label,
    )


def server_frame(opcode: int, payload: bytes = b"", *, fin: bool = True) -> bytes:
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    if length < 126:
        return bytes((first, length)) + payload
    if length <= 0xFFFF:
        return bytes((first, 126)) + length.to_bytes(2, "big") + payload
    return bytes((first, 127)) + length.to_bytes(8, "big") + payload


class MemorySocket:
    def __init__(self, incoming: bytes = b"") -> None:
        self.incoming = bytearray(incoming)
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout: float | None = None

    def recv(self, amount: int) -> bytes:
        if self.closed:
            return b""
        if not self.incoming:
            raise socket.timeout("fixture exhausted")
        size = min(amount, len(self.incoming))
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result

    def sendall(self, payload: bytes) -> None:
        if self.closed:
            raise OSError("closed")
        self.sent.append(bytes(payload))

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def close(self) -> None:
        self.closed = True


class TrickleSocket(MemorySocket):
    """Return one byte before each per-read timeout while total time expires."""

    def __init__(self, incoming: bytes, *, delay_sec: float) -> None:
        super().__init__(incoming)
        self.delay_sec = delay_sec

    def recv(self, _amount: int) -> bytes:
        if self.closed:
            return b""
        if not self.incoming:
            raise socket.timeout("fixture exhausted")
        time.sleep(self.delay_sec)
        result = bytes(self.incoming[:1])
        del self.incoming[:1]
        return result


class DelayedControlSocket(MemorySocket):
    def __init__(self, incoming: bytes, *, first_recv_delay_sec: float) -> None:
        super().__init__(incoming)
        self.first_recv_delay_sec = first_recv_delay_sec
        self._delayed = False
        self.control_send_timeout: float | None = None

    def recv(self, amount: int) -> bytes:
        if not self._delayed:
            self._delayed = True
            time.sleep(self.first_recv_delay_sec)
        return super().recv(amount)

    def sendall(self, _payload: bytes) -> None:
        self.control_send_timeout = self.timeout
        raise socket.timeout("simulated bounded control send")


def client_for(
    incoming: bytes,
    *,
    timeout_sec: float = 1.0,
    sock: MemorySocket | None = None,
) -> tuple[public_ws.PublicWebSocket, MemorySocket]:
    transport = sock or MemorySocket(incoming)
    client = public_ws.PublicWebSocket(
        transport,
        initial_bytes=b"",
        timeout_sec=timeout_sec,
        max_frame_bytes=64 * 1024,
        max_message_bytes=64 * 1024,
        stop_requested=lambda: False,
    )
    return client, transport


class L2ClockAndLineageSecurityTests(unittest.TestCase):
    def test_nonpositive_causal_clocks_are_rejected_at_normalization_boundary(self) -> None:
        cases = (
            {"exchange_ts": 0.0, "received_ts": 1.0, "monotonic_ns": 1},
            {"exchange_ts": 1.0, "received_ts": -1.0, "monotonic_ns": 1},
            {"exchange_ts": 1.0, "received_ts": 2.0, "monotonic_ns": 0},
        )
        for clocks in cases:
            with self.subTest(clocks=clocks), self.assertRaises(ValueError):
                snapshot(sequence=1, label="invalid-clock", **clocks)

    def test_backward_clock_delta_cannot_remain_execution_ready(self) -> None:
        book = L2Book(venue=VENUE, symbol=SYMBOL, max_levels=4)
        book.begin_connection(1)
        book.apply(
            snapshot(
                sequence=10,
                exchange_ts=1_000.0,
                received_ts=1_001.0,
                monotonic_ns=10_000,
                label="clock-snapshot",
            )
        )

        decision = book.apply(
            frame(
                sequence=11,
                previous_sequence=10,
                exchange_ts=900.0,
                received_ts=901.0,
                monotonic_ns=9_000,
                bids=(level("100", "1"),),
                label="backward-clock-delta",
            )
        )
        causal = book.causal_snapshot()

        self.assertNotEqual(decision.status, ApplyStatus.APPLIED_DELTA)
        self.assertTrue(causal is None or not causal.execution_ready)

    def test_stale_same_epoch_snapshot_cannot_clear_a_tainted_book(self) -> None:
        book = L2Book(venue=VENUE, symbol=SYMBOL, max_levels=4)
        book.begin_connection(1)
        book.apply(
            snapshot(
                sequence=10,
                exchange_ts=1_000.0,
                received_ts=1_001.0,
                monotonic_ns=10_000,
                label="fresh-snapshot",
            )
        )
        gap = book.apply(
            frame(
                sequence=12,
                previous_sequence=8,
                exchange_ts=1_002.0,
                received_ts=1_003.0,
                monotonic_ns=11_000,
                label="sequence-gap",
            )
        )
        self.assertEqual(gap.status, ApplyStatus.TAINTED_SEQUENCE_GAP)

        stale = book.apply(
            snapshot(
                sequence=1,
                exchange_ts=800.0,
                received_ts=801.0,
                monotonic_ns=8_000,
                label="stale-snapshot",
            )
        )
        causal = book.causal_snapshot()

        self.assertNotEqual(stale.status, ApplyStatus.APPLIED_SNAPSHOT)
        self.assertTrue(causal is None or not causal.execution_ready)

    def test_recovery_snapshot_must_advance_past_highest_seen_gap_sequence(self) -> None:
        book = L2Book(venue=VENUE, symbol=SYMBOL, max_levels=4)
        book.begin_connection(1)
        book.apply(
            snapshot(
                sequence=10,
                exchange_ts=1_000.0,
                received_ts=1_001.0,
                monotonic_ns=10_000,
                label="high-water-snapshot",
            )
        )
        gap = book.apply(
            frame(
                sequence=20,
                previous_sequence=19,
                exchange_ts=1_020.0,
                received_ts=1_021.0,
                monotonic_ns=20_000,
                label="high-water-gap",
            )
        )
        self.assertEqual(gap.status, ApplyStatus.TAINTED_SEQUENCE_GAP)

        stale_recovery = book.apply(
            snapshot(
                sequence=11,
                exchange_ts=1_022.0,
                received_ts=1_023.0,
                monotonic_ns=21_000,
                label="behind-gap-snapshot",
            )
        )
        causal = book.causal_snapshot()

        self.assertNotEqual(stale_recovery.status, ApplyStatus.APPLIED_SNAPSHOT)
        self.assertTrue(causal is None or not causal.execution_ready)

    def test_first_snapshot_must_advance_past_presnapshot_sequence_high_water(self) -> None:
        book = L2Book(venue=VENUE, symbol=SYMBOL, max_levels=4)
        book.begin_connection(1)
        before_snapshot = book.apply(
            frame(
                sequence=20,
                previous_sequence=19,
                exchange_ts=1_020.0,
                received_ts=1_021.0,
                monotonic_ns=20_000,
                label="pre-snapshot-delta",
            )
        )
        self.assertEqual(before_snapshot.status, ApplyStatus.IGNORED_SNAPSHOT_REQUIRED)

        stale_first_snapshot = book.apply(
            snapshot(
                sequence=10,
                exchange_ts=1_022.0,
                received_ts=1_023.0,
                monotonic_ns=21_000,
                label="stale-first-snapshot",
            )
        )

        self.assertNotEqual(stale_first_snapshot.status, ApplyStatus.APPLIED_SNAPSHOT)
        self.assertIsNone(book.causal_snapshot())

    def test_causal_book_exposes_a_cumulative_frame_chain_not_only_last_delta(self) -> None:
        book = L2Book(venue=VENUE, symbol=SYMBOL, max_levels=4)
        book.begin_connection(1)
        source_snapshot = snapshot(
            sequence=10,
            exchange_ts=1_000.0,
            received_ts=1_001.0,
            monotonic_ns=10_000,
            label="lineage-snapshot",
        )
        source_delta = frame(
            sequence=11,
            previous_sequence=10,
            exchange_ts=1_002.0,
            received_ts=1_003.0,
            monotonic_ns=11_000,
            bids=(level("100", "1"),),
            label="lineage-delta",
        )
        book.apply(source_snapshot)
        first = book.causal_snapshot()
        assert first is not None
        first_chain = getattr(first, "frame_chain_sha256", None)
        book.apply(source_delta)
        second = book.causal_snapshot()
        assert second is not None
        second_chain = getattr(second, "frame_chain_sha256", None)

        for value in (first_chain, second_chain):
            self.assertIsInstance(value, str)
            self.assertRegex(value, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first_chain, second_chain)
        self.assertNotEqual(second_chain, source_delta.raw_sha256)
        self.assertEqual(second.asks[0].price, level("101", "1").price)

    def test_frame_chain_binds_causal_timestamps_and_link_metadata(self) -> None:
        common = dict(
            sequence=10,
            exchange_ts=1_000.0,
            monotonic_ns=10_000,
            label="same-raw-frame",
        )
        first_book = L2Book(venue=VENUE, symbol=SYMBOL, max_levels=4)
        second_book = L2Book(venue=VENUE, symbol=SYMBOL, max_levels=4)
        first_book.begin_connection(1)
        second_book.begin_connection(1)
        first_book.apply(snapshot(received_ts=1_001.0, **common))
        second_book.apply(snapshot(received_ts=1_002.0, **common))

        first = first_book.causal_snapshot()
        second = second_book.causal_snapshot()
        assert first is not None and second is not None
        self.assertEqual(first.raw_sha256, second.raw_sha256)
        self.assertNotEqual(first.frame_chain_sha256, second.frame_chain_sha256)

    def test_frame_chain_binds_the_normalized_depth_used_for_execution(self) -> None:
        common = dict(
            sequence=10,
            exchange_ts=1_000.0,
            received_ts=1_001.0,
            monotonic_ns=10_000,
            label="same-raw-and-metadata",
        )
        first_book = L2Book(venue=VENUE, symbol=SYMBOL, max_levels=4)
        second_book = L2Book(venue=VENUE, symbol=SYMBOL, max_levels=4)
        first_book.begin_connection(1)
        second_book.begin_connection(1)
        first_book.apply(
            snapshot(bids=(level("99", "1"),), asks=(level("101", "1"),), **common)
        )
        second_book.apply(
            snapshot(bids=(level("50", "1"),), asks=(level("101", "1"),), **common)
        )

        first = first_book.causal_snapshot()
        second = second_book.causal_snapshot()
        assert first is not None and second is not None
        self.assertEqual(first.raw_sha256, second.raw_sha256)
        self.assertNotEqual(first.frame_chain_sha256, second.frame_chain_sha256)


class L2ResourceBoundSecurityTests(unittest.TestCase):
    def test_duplicate_price_rows_cannot_bypass_raw_update_limit(self) -> None:
        book = L2Book(venue=VENUE, symbol=SYMBOL, max_levels=1)
        book.begin_connection(1)
        duplicate_rows = tuple(level("99", "1") for _ in range(5_000))

        decision = book.apply(
            snapshot(
                sequence=1,
                exchange_ts=1_000.0,
                received_ts=1_001.0,
                monotonic_ns=10_000,
                bids=duplicate_rows,
                label="duplicate-price-flood",
            )
        )

        self.assertEqual(decision.status, ApplyStatus.TAINTED_MAX_LEVELS)
        self.assertIsNone(book.causal_snapshot())

    def test_in_memory_decision_audit_has_a_hard_retention_bound(self) -> None:
        book = L2Book(venue=VENUE, symbol=SYMBOL, max_levels=2)
        book.begin_connection(1)
        book.apply(
            snapshot(
                sequence=1,
                exchange_ts=1_000.0,
                received_ts=1_001.0,
                monotonic_ns=10_000,
                label="decision-snapshot",
            )
        )
        for index in range(DECISION_RETENTION_LIMIT + 100):
            book.apply(
                frame(
                    sequence=1,
                    previous_sequence=0,
                    exchange_ts=1_002.0 + index,
                    received_ts=1_003.0 + index,
                    monotonic_ns=11_000 + index,
                    label=f"old-decision-{index}",
                )
            )

        self.assertLessEqual(len(book.decisions), DECISION_RETENTION_LIMIT)


class WebSocketProtocolAndBudgetSecurityTests(unittest.TestCase):
    def test_protocol_error_irreversibly_poisons_and_closes_connection(self) -> None:
        incoming = server_frame(0x2, b"{}") + server_frame(0x1, b'{"ok":true}')
        client, transport = client_for(incoming)

        with self.assertRaises(public_ws.ProtocolError):
            client.recv_json()

        self.assertTrue(client.closed)
        self.assertTrue(transport.closed)
        with self.assertRaises(public_ws.WebSocketClosed):
            client.recv_json()

    def test_trickle_frames_cannot_reset_the_total_message_deadline(self) -> None:
        incoming = server_frame(0x1, b'{"ok":true}')
        transport = TrickleSocket(incoming, delay_sec=0.002)
        client, _ = client_for(incoming, timeout_sec=0.005, sock=transport)

        with self.assertRaises(public_ws.WebSocketTimeout):
            client.recv_json()
        self.assertTrue(client.closed)

    def test_control_frames_have_a_per_message_budget(self) -> None:
        incoming = (
            b"".join(server_frame(0xA) for _ in range(CONTROL_FRAME_BUDGET + 1))
            + server_frame(0x1, b'{"ok":true}')
        )
        client, transport = client_for(incoming)

        with self.assertRaises(public_ws.ProtocolError):
            client.recv_json()
        self.assertTrue(client.closed)
        self.assertTrue(transport.closed)

    def test_control_reply_refreshes_remaining_absolute_deadline(self) -> None:
        incoming = server_frame(0x9) + server_frame(0x1, b'{"ok":true}')
        transport = DelayedControlSocket(incoming, first_recv_delay_sec=0.02)
        client, _ = client_for(incoming, timeout_sec=0.05, sock=transport)

        with self.assertRaises(public_ws.WebSocketTimeout):
            client.recv_json()

        assert transport.control_send_timeout is not None
        self.assertGreater(transport.control_send_timeout, 0)
        self.assertLess(transport.control_send_timeout, 0.04)
        self.assertTrue(client.closed)

    def test_nonstandard_or_parser_limit_json_poison_connection(self) -> None:
        invalid_payloads = (
            b'{"value":NaN}',
            b'{"value":Infinity}',
            b'{"value":1,"value":2}',
            b'{"value":' + (b"9" * 5_000) + b"}",
        )
        for payload in invalid_payloads:
            incoming = server_frame(0x1, payload) + server_frame(0x1, b'{"ok":true}')
            client, transport = client_for(incoming)
            with self.subTest(payload_prefix=payload[:24]), self.assertRaises(
                public_ws.ProtocolError
            ):
                client.recv_json()
            self.assertTrue(client.closed)
            self.assertTrue(transport.closed)
            with self.assertRaises(public_ws.WebSocketClosed):
                client.recv_json()


class DnsAndTlsSecurityTests(unittest.TestCase):
    @staticmethod
    def _answer(address: str, port: int = 443):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
        return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)

    def test_mixed_or_private_dns_answers_fail_closed_before_connect(self) -> None:
        cases = (
            [self._answer("93.184.216.34"), self._answer("127.0.0.1")],
            [self._answer("10.0.0.1")],
            [self._answer("::1")],
        )
        for answers in cases:
            with self.subTest(answers=answers), mock.patch.object(
                public_ws.socket, "getaddrinfo", return_value=answers
            ), mock.patch.object(
                public_ws.socket,
                "create_connection",
                side_effect=AssertionError("connect must not run"),
            ) as connector, self.assertRaises(public_ws.DnsAddressNotAllowed):
                public_ws._open_tls_socket("stream.example", port=443, timeout_sec=1.0)
            connector.assert_not_called()

    def test_tls_connects_to_validated_numeric_ip_with_default_verification_and_sni(self) -> None:
        class RawSocket:
            def __init__(self) -> None:
                self.closed = False
                self.timeout: float | None = None

            def settimeout(self, value: float) -> None:
                self.timeout = value

            def close(self) -> None:
                self.closed = True

        class WrappedSocket:
            def __init__(self) -> None:
                self.timeout: float | None = None

            def settimeout(self, value: float) -> None:
                self.timeout = value

        class Context:
            def __init__(self) -> None:
                self.minimum_version = None
                self.check_hostname = True
                self.verify_mode = ssl.CERT_REQUIRED
                self.calls: list[tuple[object, str]] = []
                self.wrapped = WrappedSocket()

            def wrap_socket(self, raw, *, server_hostname: str):
                self.calls.append((raw, server_hostname))
                return self.wrapped

        raw = RawSocket()
        context = Context()
        with mock.patch.object(
            public_ws, "_resolve_public_addresses", return_value=("93.184.216.34",)
        ) as resolver, mock.patch.object(
            public_ws.socket, "create_connection", return_value=raw
        ) as connector, mock.patch.object(
            public_ws.ssl, "create_default_context", return_value=context
        ) as context_factory:
            wrapped = public_ws._open_tls_socket(
                "stream.example", port=8443, timeout_sec=2.5
            )

        resolver.assert_called_once()
        resolver_args, resolver_kwargs = resolver.call_args
        self.assertEqual(resolver_args, ("stream.example", 8443))
        self.assertGreater(resolver_kwargs["deadline"], time.monotonic())
        connector.assert_called_once()
        connector_args, _connector_kwargs = connector.call_args
        self.assertEqual(connector_args[0], ("93.184.216.34", 8443))
        self.assertGreater(connector_args[1], 0)
        self.assertLessEqual(connector_args[1], 2.5)
        context_factory.assert_called_once_with()
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(context.calls, [(raw, "stream.example")])
        self.assertIs(wrapped, context.wrapped)
        assert wrapped.timeout is not None
        self.assertGreater(wrapped.timeout, 0)
        self.assertLessEqual(wrapped.timeout, 2.5)

    def test_tls_handshake_receives_only_connect_deadline_remainder(self) -> None:
        class RawSocket:
            def __init__(self) -> None:
                self.closed = False
                self.timeout: float | None = None

            def settimeout(self, value: float) -> None:
                self.timeout = value

            def close(self) -> None:
                self.closed = True

        class Context:
            def __init__(self) -> None:
                self.minimum_version = None
                self.observed_timeout: float | None = None

            def wrap_socket(self, raw, *, server_hostname: str):
                self.observed_timeout = raw.timeout
                raise socket.timeout(f"TLS timeout for {server_hostname}")

        raw = RawSocket()
        context = Context()

        def delayed_connect(*_args, **_kwargs):
            time.sleep(0.02)
            return raw

        with mock.patch.object(
            public_ws, "_resolve_public_addresses", return_value=("93.184.216.34",)
        ), mock.patch.object(
            public_ws.socket, "create_connection", side_effect=delayed_connect
        ), mock.patch.object(
            public_ws.ssl, "create_default_context", return_value=context
        ), self.assertRaises(public_ws.WebSocketTimeout):
            public_ws._open_tls_socket("stream.example", port=443, timeout_sec=0.05)

        assert context.observed_timeout is not None
        self.assertGreater(context.observed_timeout, 0)
        self.assertLess(context.observed_timeout, 0.04)
        self.assertTrue(raw.closed)

    def test_public_open_surface_declares_an_overall_deadline_not_only_per_recv_timeout(self) -> None:
        parameters = inspect.signature(public_ws.open_public_websocket).parameters
        self.assertIn("overall_timeout_sec", parameters)

    def test_dns_resolution_is_bounded_by_the_same_overall_deadline(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocked_resolution(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=1.0)
            return [self._answer("93.184.216.34")]

        started = time.monotonic()
        try:
            with mock.patch.object(
                public_ws.socket, "getaddrinfo", side_effect=blocked_resolution
            ), self.assertRaises(public_ws.WebSocketTimeout):
                public_ws._resolve_public_addresses(
                    "stream.example",
                    443,
                    deadline=time.monotonic() + 0.01,
                )
            elapsed = time.monotonic() - started
            self.assertTrue(entered.is_set())
            self.assertLess(elapsed, 0.25)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
