"""Fail-closed tests for the additive v43 public WebSocket transport.

The suite is intentionally networkless.  A scripted TLS socket exercises the
RFC 6455 handshake and framing bytes while the real DNS and socket constructors
remain unreachable.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    import public_ws  # type: ignore[import-not-found]  # noqa: E402
except ModuleNotFoundError:
    public_ws = None


HOST = "stream.example"
PATH = "/public/linear"
URL = f"wss://{HOST}{PATH}"
ALLOW_LIST = frozenset({(HOST, 443, PATH)})
KEY_BYTES = b"K" * 16
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _accept_value(key_bytes: bytes = KEY_BYTES) -> str:
    key = base64.b64encode(key_bytes).decode("ascii")
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()  # noqa: S324
    return base64.b64encode(digest).decode("ascii")


def _handshake(
    *,
    status: str = "101 Switching Protocols",
    accept: str | None = None,
    upgrade: str = "websocket",
    connection: str = "Upgrade",
    extra_headers: tuple[str, ...] = (),
) -> bytes:
    headers = [
        f"HTTP/1.1 {status}",
        f"Upgrade: {upgrade}",
        f"Connection: {connection}",
        f"Sec-WebSocket-Accept: {accept or _accept_value()}",
        *extra_headers,
        "",
        "",
    ]
    return "\r\n".join(headers).encode("ascii")


def _server_frame(opcode: int, payload: bytes, *, fin: bool = True, masked: bool = False) -> bytes:
    first = (0x80 if fin else 0) | opcode
    mask_bit = 0x80 if masked else 0
    length = len(payload)
    if length < 126:
        header = bytes((first, mask_bit | length))
    elif length <= 0xFFFF:
        header = bytes((first, mask_bit | 126)) + length.to_bytes(2, "big")
    else:
        header = bytes((first, mask_bit | 127)) + length.to_bytes(8, "big")
    if not masked:
        return header + payload
    key = b"MASK"
    encoded = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return header + key + encoded


def _decode_client_frame(frame: bytes) -> tuple[bool, int, bytes]:
    first, second = frame[:2]
    masked = bool(second & 0x80)
    length = second & 0x7F
    cursor = 2
    if length == 126:
        length = int.from_bytes(frame[cursor : cursor + 2], "big")
        cursor += 2
    elif length == 127:
        length = int.from_bytes(frame[cursor : cursor + 8], "big")
        cursor += 8
    key = frame[cursor : cursor + 4]
    cursor += 4
    payload = frame[cursor : cursor + length]
    decoded = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return masked, first & 0x0F, decoded


class ScriptedSocket:
    def __init__(self, incoming: bytes = b"") -> None:
        self.incoming = bytearray(incoming)
        self.sent: list[bytes] = []
        self.timeout: float | None = None
        self.closed = False
        self.recv_calls = 0

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def sendall(self, data: bytes) -> None:
        if self.closed:
            raise OSError("closed")
        self.sent.append(bytes(data))

    def recv(self, amount: int) -> bytes:
        self.recv_calls += 1
        if self.closed:
            return b""
        if not self.incoming:
            raise socket.timeout("scripted timeout")
        size = min(amount, len(self.incoming))
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result

    def close(self) -> None:
        self.closed = True


class PublicWsModulePresenceTests(unittest.TestCase):
    def test_public_ws_transport_module_exists(self) -> None:
        self.assertIsNotNone(public_ws, "src/public_ws.py has not been implemented")


@unittest.skipIf(public_ws is None, "public_ws implementation absent")
class PublicWsTests(unittest.TestCase):
    def _open(
        self,
        incoming: bytes = b"",
        *,
        url: str = URL,
        allowed_endpoints=ALLOW_LIST,
        max_frame_bytes: int = 1024,
        max_message_bytes: int = 2048,
        stop_requested=None,
    ):
        sock = ScriptedSocket(_handshake() + incoming)
        with mock.patch.object(public_ws, "_open_tls_socket", return_value=sock) as opener, mock.patch.object(
            public_ws.os, "urandom", side_effect=lambda amount: KEY_BYTES if amount == 16 else b"M" * amount
        ):
            client = public_ws.open_public_websocket(
                url,
                allowed_endpoints=allowed_endpoints,
                timeout_sec=2.5,
                max_frame_bytes=max_frame_bytes,
                max_message_bytes=max_message_bytes,
                stop_requested=stop_requested,
            )
        return client, sock, opener

    def test_public_api_does_not_accept_headers_redirect_or_reconnect_overrides(self) -> None:
        parameters = inspect.signature(public_ws.open_public_websocket).parameters
        for forbidden in ("headers", "authorization", "cookies", "redirects", "reconnect"):
            with self.subTest(parameter=forbidden):
                self.assertNotIn(forbidden, parameters)

    def test_url_policy_is_exact_and_rejects_before_connect(self) -> None:
        bad_urls = (
            f"ws://{HOST}{PATH}",
            f"https://{HOST}{PATH}",
            f"wss://user@{HOST}{PATH}",
            f"wss://{HOST}:444{PATH}",
            f"wss://{HOST}{PATH}?topic=book",
            f"wss://{HOST}{PATH}#fragment",
            f"wss://{HOST}{PATH}/extra",
            f"wss://{HOST}/public/../public/linear",
            f"wss://{HOST}/public%2flinear",
            f"wss://{HOST}/public\\linear",
        )
        for url in bad_urls:
            with self.subTest(url=url), mock.patch.object(
                public_ws, "_open_tls_socket", side_effect=AssertionError("connect reached")
            ) as opener, self.assertRaises(public_ws.EndpointNotAllowed):
                public_ws.open_public_websocket(url, allowed_endpoints=ALLOW_LIST)
            opener.assert_not_called()

    def test_explicit_tls_port_and_exact_injected_allow_list_are_accepted(self) -> None:
        client, sock, opener = self._open(url=f"wss://{HOST}:443{PATH}")
        self.addCleanup(client.close)
        opener.assert_called_once_with(HOST, port=443, timeout_sec=2.5)
        request = sock.sent[0].decode("ascii")
        self.assertTrue(request.startswith(f"GET {PATH} HTTP/1.1\r\n"))
        self.assertIn(f"Host: {HOST}\r\n", request)
        self.assertIn("Upgrade: websocket\r\n", request)
        self.assertIn("Connection: Upgrade\r\n", request)
        self.assertIn("Sec-WebSocket-Version: 13\r\n", request)
        self.assertNotIn("Authorization:", request)
        self.assertNotIn("Cookie:", request)
        self.assertEqual(sock.timeout, 2.5)

    def test_nondefault_official_tls_port_requires_an_exact_port_binding(self) -> None:
        client, sock, opener = self._open(
            url=f"wss://{HOST}:8443{PATH}",
            allowed_endpoints=frozenset({(HOST, 8443, PATH)}),
        )
        self.addCleanup(client.close)
        opener.assert_called_once_with(HOST, port=8443, timeout_sec=2.5)
        request = sock.sent[0].decode("ascii")
        self.assertIn(f"Host: {HOST}:8443\r\n", request)

        with mock.patch.object(
            public_ws, "_open_tls_socket", side_effect=AssertionError("connect reached")
        ) as blocked, self.assertRaises(public_ws.EndpointNotAllowed):
            public_ws.open_public_websocket(
                f"wss://{HOST}:8443{PATH}",
                allowed_endpoints=ALLOW_LIST,
            )
        blocked.assert_not_called()

    def test_handshake_rejects_redirect_without_second_connection(self) -> None:
        sock = ScriptedSocket(_handshake(status="302 Found", extra_headers=("Location: wss://else.invalid/ws",)))
        with mock.patch.object(public_ws, "_open_tls_socket", return_value=sock) as opener, mock.patch.object(
            public_ws.os, "urandom", return_value=KEY_BYTES
        ), self.assertRaises(public_ws.RedirectNotAllowed):
            public_ws.open_public_websocket(URL, allowed_endpoints=ALLOW_LIST)
        opener.assert_called_once()
        self.assertTrue(sock.closed)

    def test_handshake_validates_accept_upgrade_connection_and_extensions(self) -> None:
        responses = (
            _handshake(accept="wrong"),
            _handshake(upgrade="not-websocket"),
            _handshake(connection="keep-alive"),
            _handshake(extra_headers=("Sec-WebSocket-Extensions: permessage-deflate",)),
        )
        for response in responses:
            sock = ScriptedSocket(response)
            with self.subTest(response=response), mock.patch.object(
                public_ws, "_open_tls_socket", return_value=sock
            ), mock.patch.object(public_ws.os, "urandom", return_value=KEY_BYTES), self.assertRaises(
                public_ws.HandshakeError
            ):
                public_ws.open_public_websocket(URL, allowed_endpoints=ALLOW_LIST)
            self.assertTrue(sock.closed)

    def test_handshake_headers_are_bounded(self) -> None:
        sock = ScriptedSocket(b"HTTP/1.1 101 Switching Protocols\r\nX-Fill: " + b"x" * 200)
        with mock.patch.object(public_ws, "_open_tls_socket", return_value=sock), mock.patch.object(
            public_ws.os, "urandom", return_value=KEY_BYTES
        ), self.assertRaises(public_ws.HandshakeError):
            public_ws.open_public_websocket(
                URL,
                allowed_endpoints=ALLOW_LIST,
                max_handshake_bytes=128,
            )
        self.assertTrue(sock.closed)

    def test_send_json_uses_a_masked_client_text_frame(self) -> None:
        client, sock, _opener = self._open()
        self.addCleanup(client.close)
        sock.sent.clear()
        with mock.patch.object(public_ws.os, "urandom", return_value=b"Z" * 4):
            client.send_json({"op": "subscribe", "args": ["book"]})
        self.assertEqual(len(sock.sent), 1)
        masked, opcode, payload = _decode_client_frame(sock.sent[0])
        self.assertTrue(masked)
        self.assertEqual(opcode, 0x1)
        self.assertEqual(json.loads(payload.decode("utf-8")), {"op": "subscribe", "args": ["book"]})

    def test_receive_returns_text_json_and_rejects_binary(self) -> None:
        expected = {"topic": "book", "sequence": 7}
        client, _sock, _opener = self._open(
            _server_frame(0x1, json.dumps(expected).encode("utf-8"))
        )
        self.addCleanup(client.close)
        self.assertEqual(client.recv_json(), expected)

        binary_client, _binary_sock, _ = self._open(_server_frame(0x2, b"{}"))
        self.addCleanup(binary_client.close)
        with self.assertRaises(public_ws.ProtocolError):
            binary_client.recv_json()

    def test_fragmented_text_message_is_reassembled_with_message_bound(self) -> None:
        incoming = _server_frame(0x1, b'{"value":', fin=False) + _server_frame(0x0, b"123}")
        client, _sock, _opener = self._open(incoming, max_frame_bytes=16, max_message_bytes=16)
        self.addCleanup(client.close)
        self.assertEqual(client.recv_json(), {"value": 123})

        too_large = _server_frame(0x1, b'{"value":', fin=False) + _server_frame(0x0, b"12345}")
        limited, _limited_sock, _ = self._open(too_large, max_frame_bytes=16, max_message_bytes=12)
        self.addCleanup(limited.close)
        with self.assertRaises(public_ws.MessageTooLarge):
            limited.recv_json()

    def test_oversized_frame_is_rejected_from_header_before_payload_read(self) -> None:
        declared_length = 2049
        header_only = bytes((0x81, 126)) + declared_length.to_bytes(2, "big")
        client, _sock, _opener = self._open(header_only, max_frame_bytes=1024)
        self.addCleanup(client.close)
        with self.assertRaises(public_ws.FrameTooLarge):
            client.recv_json()

    def test_masked_server_frame_and_non_minimal_length_are_rejected(self) -> None:
        bad_frames = (
            _server_frame(0x1, b"{}", masked=True),
            bytes((0x81, 126)) + (2).to_bytes(2, "big") + b"{}",
        )
        for frame in bad_frames:
            client, _sock, _opener = self._open(frame)
            self.addCleanup(client.close)
            with self.subTest(frame=frame), self.assertRaises(public_ws.ProtocolError):
                client.recv_json()

    def test_ping_is_answered_with_masked_pong_before_json(self) -> None:
        incoming = _server_frame(0x9, b"alive") + _server_frame(0x1, b'{"ok":true}')
        client, sock, _opener = self._open(incoming)
        self.addCleanup(client.close)
        sock.sent.clear()
        with mock.patch.object(public_ws.os, "urandom", return_value=b"P" * 4):
            self.assertEqual(client.recv_json(), {"ok": True})
        self.assertEqual(len(sock.sent), 1)
        masked, opcode, payload = _decode_client_frame(sock.sent[0])
        self.assertTrue(masked)
        self.assertEqual((opcode, payload), (0xA, b"alive"))

    def test_close_is_acknowledged_with_masked_close_and_closes_socket(self) -> None:
        payload = (1000).to_bytes(2, "big") + b"done"
        client, sock, _opener = self._open(_server_frame(0x8, payload))
        sock.sent.clear()
        with mock.patch.object(public_ws.os, "urandom", return_value=b"C" * 4), self.assertRaises(
            public_ws.WebSocketClosed
        ):
            client.recv_json()
        self.assertTrue(sock.closed)
        masked, opcode, echoed = _decode_client_frame(sock.sent[0])
        self.assertTrue(masked)
        self.assertEqual((opcode, echoed), (0x8, payload))

    def test_socket_timeout_and_stop_are_distinct_and_never_reconnect(self) -> None:
        client, _sock, opener = self._open()
        self.addCleanup(client.close)
        with self.assertRaises(public_ws.WebSocketTimeout):
            client.recv_json()
        opener.assert_called_once()

        stopped = False
        stop_client, stop_sock, stop_opener = self._open(stop_requested=lambda: stopped)
        self.addCleanup(stop_client.close)
        stopped = True
        before = stop_sock.recv_calls
        with self.assertRaises(public_ws.StopRequested):
            stop_client.recv_json()
        self.assertEqual(stop_sock.recv_calls, before)
        stop_opener.assert_called_once()

    def test_invalid_utf8_or_json_is_rejected(self) -> None:
        frames = (_server_frame(0x1, b"\xff"), _server_frame(0x1, b"not-json"))
        for frame in frames:
            client, _sock, _opener = self._open(frame)
            self.addCleanup(client.close)
            with self.subTest(frame=frame), self.assertRaises(public_ws.ProtocolError):
                client.recv_json()

    def test_outbound_payload_and_close_reason_are_bounded(self) -> None:
        client, _sock, _opener = self._open(max_frame_bytes=8, max_message_bytes=8)
        self.addCleanup(client.close)
        with self.assertRaises(public_ws.MessageTooLarge):
            client.send_json({"long": "payload"})
        with self.assertRaises(ValueError):
            client.close(reason="x" * 124)


if __name__ == "__main__":
    unittest.main()
