"""Networkless contracts for byte-exact public WebSocket JSON receive."""

from __future__ import annotations

import dataclasses
import socket
import sys
import threading
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import public_ws  # noqa: E402


def _server_frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    if length < 126:
        return bytes((first, length)) + payload
    if length <= 0xFFFF:
        return bytes((first, 126)) + length.to_bytes(2, "big") + payload
    return bytes((first, 127)) + length.to_bytes(8, "big") + payload


class MemorySocket:
    def __init__(self, incoming: bytes) -> None:
        self.incoming = bytearray(incoming)
        self.sent: list[bytes] = []
        self.closed = False

    def settimeout(self, _value: float) -> None:
        return None

    def recv(self, amount: int) -> bytes:
        if self.closed:
            return b""
        if not self.incoming:
            raise socket.timeout("no scripted bytes remain")
        take = min(amount, len(self.incoming))
        result = bytes(self.incoming[:take])
        del self.incoming[:take]
        return result

    def sendall(self, payload: bytes) -> None:
        self.sent.append(bytes(payload))

    def close(self) -> None:
        self.closed = True


def _client(
    incoming: bytes,
    *,
    max_frame_bytes: int = 1_024,
    max_message_bytes: int = 2_048,
    wall_clock=None,
    monotonic_clock_ns=None,
) -> tuple[public_ws.PublicWebSocket, MemorySocket]:
    sock = MemorySocket(incoming)
    client = public_ws.PublicWebSocket(
        sock,
        initial_bytes=b"",
        timeout_sec=1.0,
        overall_timeout_sec=1.0,
        max_frame_bytes=max_frame_bytes,
        max_message_bytes=max_message_bytes,
        stop_requested=lambda: False,
        _wall_clock=wall_clock or (lambda: 1_700_000_000.125),
        _monotonic_clock_ns=monotonic_clock_ns or (lambda: 9_000_000_125),
    )
    return client, sock


class RawReceiveTests(unittest.TestCase):
    def test_handshake_remainder_uses_its_receive_clock_not_late_processing_time(self) -> None:
        raw = b'{"topic":"book","n":1}'
        frame = _server_frame(0x1, raw)

        class NoReceiveSocket(MemorySocket):
            def recv(self, _amount: int) -> bytes:
                raise AssertionError("complete handshake remainder must not read the socket")

        late_clock_calls: list[str] = []
        client = public_ws.PublicWebSocket(
            NoReceiveSocket(b""),
            initial_bytes=frame,
            initial_received_ts=1_700_000_000.125,
            initial_monotonic_ns=9_000_000_125,
            timeout_sec=1.0,
            overall_timeout_sec=1.0,
            max_frame_bytes=1_024,
            max_message_bytes=2_048,
            stop_requested=lambda: False,
            _wall_clock=lambda: late_clock_calls.append("wall") or 1_800_000_000.0,
            _monotonic_clock_ns=lambda: late_clock_calls.append("mono") or 99_000_000_000,
        )

        result = client.recv_json_message()

        self.assertEqual(result.raw_payload, raw)
        self.assertEqual(result.received_ts, 1_700_000_000.125)
        self.assertEqual(result.monotonic_ns, 9_000_000_125)
        self.assertEqual(late_clock_calls, [])

    def test_handshake_remainder_without_receive_clock_fails_closed(self) -> None:
        with self.assertRaises(public_ws.PublicWebSocketError):
            public_ws.PublicWebSocket(
                MemorySocket(b""),
                initial_bytes=_server_frame(0x1, b'{"topic":"book"}'),
                timeout_sec=1.0,
                overall_timeout_sec=1.0,
                max_frame_bytes=1_024,
                max_message_bytes=2_048,
                stop_requested=lambda: False,
            )

    def test_live_frame_clock_is_sampled_at_final_socket_receive(self) -> None:
        clock = {"wall": 1_700_000_000.050, "mono": 9_000_000_050}

        class FinalByteClockSocket(MemorySocket):
            def recv(self, amount: int) -> bytes:
                chunk = super().recv(amount)
                if not self.incoming:
                    clock["wall"] = 1_700_000_000.125
                    clock["mono"] = 9_000_000_125
                return chunk

        sock = FinalByteClockSocket(_server_frame(0x1, b'{"topic":"book"}'))
        client = public_ws.PublicWebSocket(
            sock,
            initial_bytes=b"",
            timeout_sec=1.0,
            overall_timeout_sec=1.0,
            max_frame_bytes=1_024,
            max_message_bytes=2_048,
            stop_requested=lambda: False,
            _wall_clock=lambda: clock["wall"],
            _monotonic_clock_ns=lambda: clock["mono"],
        )
        original_read_frame = client._read_frame

        def delay_after_complete_frame(*, deadline: float):
            frame = original_read_frame(deadline=deadline)
            clock["wall"] = 1_800_000_000.0
            clock["mono"] = 99_000_000_000
            return frame

        with mock.patch.object(client, "_read_frame", side_effect=delay_after_complete_frame):
            result = client.recv_json_message()

        self.assertEqual(result.received_ts, 1_700_000_000.125)
        self.assertEqual(result.monotonic_ns, 9_000_000_125)

    def test_unfragmented_result_preserves_exact_bytes_and_is_immutable(self) -> None:
        raw = b'{ "topic" : "book", "data" : {"px": "10.00"} }'
        order: list[str] = []
        client, _sock = _client(
            _server_frame(0x1, raw),
            wall_clock=lambda: order.append("received_ts") or 1_700_000_000.125,
            monotonic_clock_ns=lambda: order.append("monotonic_ns") or 9_000_000_125,
        )
        original_decode = client._decode_json

        def decode_after_clocks(payload: bytes):
            order.append("decode")
            return original_decode(payload)

        with mock.patch.object(client, "_decode_json", side_effect=decode_after_clocks):
            result = client.recv_json_message()

        self.assertIsInstance(result, public_ws.ReceivedJsonMessage)
        self.assertEqual(result.raw_payload, raw)
        self.assertEqual(
            result.decoded,
            {"topic": "book", "data": {"px": "10.00"}},
        )
        self.assertIsInstance(result.decoded, MappingProxyType)
        self.assertIsInstance(result.decoded["data"], MappingProxyType)
        self.assertEqual(result.application_ordinal, 1)
        self.assertEqual(result.data_frame_count, 1)
        self.assertEqual(result.control_frame_count, 0)
        self.assertEqual(result.received_ts, 1_700_000_000.125)
        self.assertEqual(result.monotonic_ns, 9_000_000_125)
        self.assertEqual(
            order,
            [
                "received_ts",
                "monotonic_ns",
                "received_ts",
                "monotonic_ns",
                "decode",
            ],
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.application_ordinal = 7  # type: ignore[misc]
        with self.assertRaises(TypeError):
            result.decoded["topic"] = "changed"  # type: ignore[index]
        with self.assertRaises(TypeError):
            result.decoded["data"]["px"] = "0"  # type: ignore[index]

    def test_result_constructor_is_internal_to_the_transport(self) -> None:
        with self.assertRaises(TypeError):
            public_ws.ReceivedJsonMessage(
                raw_payload=b'{"topic":"book"}',
                decoded={"topic": "forged"},
                application_ordinal=1,
                data_frame_count=1,
                control_frame_count=0,
                received_ts=1_700_000_000.0,
                monotonic_ns=1,
            )

    def test_fragmented_text_counts_data_and_interleaved_control_frames(self) -> None:
        incoming = b"".join(
            (
                _server_frame(0x1, b'{"topic":', fin=False),
                _server_frame(0x9, b"ping"),
                _server_frame(0xA, b"peer-pong"),
                _server_frame(0x0, b'"book","n":1}'),
            )
        )
        client, sock = _client(incoming)

        with mock.patch.object(public_ws.os, "urandom", return_value=b"MASK"):
            result = client.recv_json_message()

        self.assertEqual(result.raw_payload, b'{"topic":"book","n":1}')
        self.assertEqual(result.decoded, {"topic": "book", "n": 1})
        self.assertEqual(result.data_frame_count, 2)
        self.assertEqual(result.control_frame_count, 2)
        self.assertEqual(len(sock.sent), 1, "ping must receive one pong")

    def test_application_ordinal_increases_only_for_returned_messages(self) -> None:
        incoming = b"".join(
            (
                _server_frame(0x9, b"first-ping"),
                _server_frame(0x1, b'{"n":1}'),
                _server_frame(0xA, b"between"),
                _server_frame(0x1, b'{"n":2}'),
            )
        )
        client, _sock = _client(incoming)

        first = client.recv_json_message()
        second = client.recv_json_message()

        self.assertEqual((first.application_ordinal, second.application_ordinal), (1, 2))
        self.assertEqual((first.control_frame_count, second.control_frame_count), (1, 1))

    def test_recv_json_is_a_compatibility_wrapper_for_decoded_mapping(self) -> None:
        source, _source_sock = _client(_server_frame(0x1, b'{"topic":"book"}'))
        exact = source.recv_json_message()
        client, _sock = _client(b"")

        with mock.patch.object(client, "recv_json_message", return_value=exact) as receive:
            result = client.recv_json()

        receive.assert_called_once_with()
        self.assertIs(result, exact.decoded)

    def test_malformed_duplicate_key_and_non_mapping_json_fail_closed(self) -> None:
        payloads = (
            b"not-json",
            b'{"topic":"a","topic":"b"}',
            b'["not", "an", "object"]',
        )
        for payload in payloads:
            client, sock = _client(_server_frame(0x1, payload))
            with self.subTest(payload=payload), self.assertRaises(public_ws.ProtocolError):
                client.recv_json_message()
            self.assertTrue(sock.closed)

    def test_binary_and_invalid_utf8_application_messages_fail_closed(self) -> None:
        cases = (
            _server_frame(0x2, b"{}"),
            _server_frame(0x1, b'{"bad":"\xff"}'),
        )
        for incoming in cases:
            client, sock = _client(incoming)
            with self.subTest(incoming=incoming), self.assertRaises(public_ws.ProtocolError):
                client.recv_json_message()
            self.assertTrue(sock.closed)

    def test_oversize_and_protocol_errors_fail_closed_without_result(self) -> None:
        cases = (
            (
                _server_frame(0x1, b'{"long":', fin=False)
                + _server_frame(0x0, b'"value"}'),
                16,
                12,
                public_ws.MessageTooLarge,
            ),
            (
                bytes((0x81, 126)) + (2).to_bytes(2, "big") + b"{}",
                16,
                16,
                public_ws.ProtocolError,
            ),
        )
        for incoming, frame_limit, message_limit, error in cases:
            client, sock = _client(
                incoming,
                max_frame_bytes=frame_limit,
                max_message_bytes=message_limit,
            )
            with self.subTest(error=error), self.assertRaises(error):
                client.recv_json_message()
            self.assertTrue(sock.closed)

    def test_fragment_count_is_bounded_even_when_fragments_are_empty(self) -> None:
        incoming = _server_frame(0x1, b"{", fin=False)
        incoming += _server_frame(0x0, b"", fin=False) * 1_024
        incoming += _server_frame(0x0, b"}")
        client, sock = _client(incoming)

        with self.assertRaisesRegex(public_ws.ProtocolError, "data-frame budget"):
            client.recv_json_message()

        self.assertTrue(sock.closed)

    def test_connection_rejects_a_concurrent_second_reader(self) -> None:
        class BlockingSocket(MemorySocket):
            def __init__(self) -> None:
                super().__init__(b"")
                self.entered = threading.Event()
                self.released = threading.Event()
                self.calls = 0
                self.calls_lock = threading.Lock()

            def recv(self, _amount: int) -> bytes:
                with self.calls_lock:
                    self.calls += 1
                    call = self.calls
                if call == 1:
                    self.entered.set()
                    self.released.wait(2.0)
                return b""

            def close(self) -> None:
                super().close()
                self.released.set()

        sock = BlockingSocket()
        client = public_ws.PublicWebSocket(
            sock,
            initial_bytes=b"",
            timeout_sec=1.0,
            overall_timeout_sec=1.0,
            max_frame_bytes=1_024,
            max_message_bytes=2_048,
            stop_requested=lambda: False,
        )
        first_errors: list[BaseException] = []

        def first_reader() -> None:
            try:
                client.recv_json_message()
            except BaseException as exc:  # captured for deterministic thread cleanup
                first_errors.append(exc)

        thread = threading.Thread(target=first_reader)
        thread.start()
        self.assertTrue(sock.entered.wait(1.0))

        try:
            client.recv_json_message()
        except BaseException as exc:
            concurrent_error = exc
        else:
            concurrent_error = None

        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(sock.closed)
        self.assertEqual(len(first_errors), 1)
        self.assertIsInstance(first_errors[0], public_ws.WebSocketClosed)
        self.assertIsInstance(concurrent_error, public_ws.ProtocolError)
        self.assertIn("concurrent", str(concurrent_error))


if __name__ == "__main__":
    unittest.main()
