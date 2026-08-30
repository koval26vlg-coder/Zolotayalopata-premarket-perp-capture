"""Small fail-closed RFC 6455 transport for public market-data streams.

The caller must inject the exact ``(host, port, path)`` allow-list.  This module owns
one TLS connection and deliberately has no redirect, retry, reconnect, venue,
capture, or trading policy.  It sends masked client frames and accepts only
unmasked text JSON plus the RFC control frames needed to keep one connection
well behaved.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import os
import queue
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable


DEFAULT_TIMEOUT_SEC = 10.0
DEFAULT_OVERALL_TIMEOUT_SEC = 10.0
DEFAULT_MAX_HANDSHAKE_BYTES = 16 * 1024
DEFAULT_MAX_FRAME_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_MESSAGE_BYTES = 8 * 1024 * 1024

HARD_MAX_TIMEOUT_SEC = 60.0
HARD_MAX_HANDSHAKE_BYTES = 64 * 1024
HARD_MAX_FRAME_BYTES = 16 * 1024 * 1024
HARD_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
MAX_CONTROL_FRAMES_PER_MESSAGE = 1_024
MAX_DATA_FRAMES_PER_MESSAGE = 1_024

_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HOST = re.compile(r"[a-z0-9.-]+")
_ALLOWED_OPCODES = frozenset({0x0, 0x1, 0x2, 0x8, 0x9, 0xA})
_DNS_RESOLVER_SLOT = threading.Lock()


class PublicWebSocketError(RuntimeError):
    """Base error for this single-connection transport."""


class EndpointNotAllowed(PublicWebSocketError):
    """The URL is not the exact public endpoint authorized by the caller."""


class RedirectNotAllowed(EndpointNotAllowed):
    """A WebSocket upgrade is single-hop and redirects are never followed."""


class DnsAddressNotAllowed(PublicWebSocketError):
    """An allowed hostname resolved to an address that is not globally routable."""


class HandshakeError(PublicWebSocketError):
    """The peer did not complete a valid RFC 6455 opening handshake."""


class ProtocolError(PublicWebSocketError):
    """The peer sent an invalid or unsupported RFC 6455 message."""


class FrameTooLarge(ProtocolError):
    """A frame length exceeded the configured bound."""


class MessageTooLarge(ProtocolError):
    """A complete text message exceeded the configured bound."""


class WebSocketTimeout(PublicWebSocketError):
    """The one bounded socket operation timed out."""


class StopRequested(PublicWebSocketError):
    """The caller's stop predicate became true."""


class WebSocketClosed(PublicWebSocketError):
    """The peer closed the connection or the TCP stream ended."""

    def __init__(self, message: str, *, code: int | None = None, reason: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason


def _reject(url: str, reason: str) -> EndpointNotAllowed:
    return EndpointNotAllowed(f"WebSocket endpoint not allowed: {reason}: {url}")


def _validate_allow_list(
    allowed_endpoints: Iterable[tuple[str, int, str]],
) -> frozenset[tuple[str, int, str]]:
    try:
        entries = frozenset(allowed_endpoints)
    except (TypeError, ValueError) as exc:
        raise EndpointNotAllowed(
            "allowed_endpoints must contain exact (host, port, path) triples"
        ) from exc
    if not entries:
        raise EndpointNotAllowed("allowed_endpoints must not be empty")
    for entry in entries:
        if not isinstance(entry, tuple) or len(entry) != 3:
            raise EndpointNotAllowed(
                "each allowed endpoint must be a (host, port, path) tuple"
            )
        host, port, path = entry
        if (
            not isinstance(host, str)
            or not host
            or host != host.lower()
            or _HOST.fullmatch(host) is None
            or host.startswith(".")
            or host.endswith(".")
            or ".." in host
        ):
            raise EndpointNotAllowed("allow-list hosts must be canonical lowercase DNS names")
        if isinstance(port, bool) or port not in {443, 8443}:
            raise EndpointNotAllowed("allow-list port must be official TLS port 443 or 8443")
        if not _path_is_canonical(path):
            raise EndpointNotAllowed("allow-list paths must be canonical exact paths")
    return entries


def _path_is_canonical(path: object) -> bool:
    return bool(
        isinstance(path, str)
        and path.startswith("/")
        and not any(ord(char) <= 0x20 or ord(char) == 0x7F for char in path)
        and "\\" not in path
        and "%" not in path
        and "?" not in path
        and "#" not in path
        and not any(segment in (".", "..") for segment in path.split("/"))
    )


def require_allowed_endpoint(
    url: str,
    *,
    allowed_endpoints: Iterable[tuple[str, int, str]],
) -> tuple[str, int, str]:
    """Return host/port/path only when the exact injected triple permits it."""

    allowed = _validate_allow_list(allowed_endpoints)
    if not isinstance(url, str) or not url:
        raise _reject(str(url), "URL must be a non-empty string")
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
        raise _reject(url, "whitespace and control characters are forbidden")
    if "\\" in url:
        raise _reject(url, "backslashes are forbidden")
    if "?" in url:
        raise _reject(url, "queries are forbidden")
    if "#" in url:
        raise _reject(url, "fragments are forbidden")

    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise _reject(url, f"malformed authority: {exc}") from exc
    if parsed.scheme.lower() != "wss":
        raise _reject(url, "scheme must be WSS")
    if not parsed.netloc or parsed.hostname is None:
        raise _reject(url, "hostname is required")
    if parsed.username is not None or parsed.password is not None:
        raise _reject(url, "userinfo is forbidden")
    effective_port = 443 if port is None else port
    if effective_port not in {443, 8443}:
        raise _reject(url, "only approved TLS ports 443 and 8443 are allowed")
    if parsed.query or parsed.fragment:
        raise _reject(url, "query and fragment are forbidden")

    host = parsed.hostname.lower()
    path = parsed.path or "/"
    if _HOST.fullmatch(host) is None or not _path_is_canonical(path):
        raise _reject(url, "host or path is not canonical")
    if (host, effective_port, path) not in allowed:
        raise _reject(url, "host/port/path triple is not in the injected allow-list")
    return host, effective_port, path


def _positive_float(name: str, value: object, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0 or result > maximum:
        raise ValueError(f"{name} must be in (0, {maximum}]")
    return result


def _positive_int(name: str, value: object, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > maximum:
        raise ValueError(f"{name} must be an integer in [1, {maximum}]")
    return value


def _bounded_getaddrinfo(host: str, port: int, *, deadline: float) -> list[tuple]:
    remaining = _remaining_time(deadline)
    if not _DNS_RESOLVER_SLOT.acquire(timeout=remaining):
        raise WebSocketTimeout("public DNS resolver is busy past the operation deadline")

    outcome: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            value: object = socket.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
            result = (True, value)
        except Exception as exc:  # delivered to the bounded caller thread
            result = (False, exc)
        try:
            outcome.put_nowait(result)
        finally:
            _DNS_RESOLVER_SLOT.release()

    worker = threading.Thread(
        target=resolve,
        name="public-ws-dns-resolver",
        daemon=True,
    )
    try:
        worker.start()
    except Exception:
        _DNS_RESOLVER_SLOT.release()
        raise

    try:
        succeeded, value = outcome.get(timeout=_remaining_time(deadline))
    except queue.Empty as exc:
        raise WebSocketTimeout("public DNS resolution exceeded the operation deadline") from exc
    _remaining_time(deadline)
    if not succeeded:
        assert isinstance(value, Exception)
        raise value
    if not isinstance(value, list):
        raise PublicWebSocketError("DNS resolver returned an invalid result container")
    return value


def _resolve_public_addresses(
    host: str,
    port: int,
    *,
    deadline: float | None = None,
) -> tuple[str, ...]:
    if deadline is None:
        deadline = time.monotonic() + DEFAULT_TIMEOUT_SEC
    try:
        answers = _bounded_getaddrinfo(host, port, deadline=deadline)
    except socket.gaierror as exc:
        raise PublicWebSocketError(f"DNS resolution failed for {host}: {exc}") from exc
    if not answers:
        raise PublicWebSocketError(f"DNS returned no addresses for {host}")

    addresses: list[str] = []
    for answer in answers:
        try:
            raw = str(answer[4][0]).split("%", 1)[0]
            address = ipaddress.ip_address(raw)
        except (IndexError, TypeError, ValueError) as exc:
            raise DnsAddressNotAllowed(f"DNS returned a non-IP address for {host}") from exc
        if not address.is_global:
            raise DnsAddressNotAllowed(
                f"DNS returned a non-public address for {host}: {address}"
            )
        rendered = str(address)
        if rendered not in addresses:
            addresses.append(rendered)
    return tuple(addresses)


def _open_tls_socket(host: str, *, port: int, timeout_sec: float):
    """Open exactly one TLS socket to a DNS answer validated immediately beforehand."""

    if isinstance(port, bool) or port not in {443, 8443}:
        raise EndpointNotAllowed("TLS port is not an approved public WebSocket port")
    deadline = time.monotonic() + timeout_sec
    addresses = _resolve_public_addresses(host, port, deadline=deadline)
    try:
        raw = socket.create_connection(
            (addresses[0], port),
            min(timeout_sec, _remaining_time(deadline)),
        )
    except socket.timeout as exc:
        raise WebSocketTimeout("public WebSocket TCP connect exceeded its deadline") from exc
    except OSError as exc:
        raise PublicWebSocketError(f"public WebSocket TCP connect failed: {exc}") from exc
    wrapped = None
    try:
        _remaining_time(deadline)
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        raw.settimeout(min(timeout_sec, _remaining_time(deadline)))
        wrapped = context.wrap_socket(raw, server_hostname=host)
        wrapped.settimeout(min(timeout_sec, _remaining_time(deadline)))
        return wrapped
    except socket.timeout as exc:
        (raw if wrapped is None else wrapped).close()
        raise WebSocketTimeout("public WebSocket TLS handshake exceeded its deadline") from exc
    except Exception:
        (raw if wrapped is None else wrapped).close()
        raise


def _check_stop(stop_requested: Callable[[], bool]) -> None:
    try:
        stopped = stop_requested()
    except Exception as exc:
        raise StopRequested(f"stop predicate failed closed: {exc}") from exc
    if stopped is not False and stopped is not True:
        raise StopRequested("stop predicate must return bool")
    if stopped:
        raise StopRequested("stop requested")


def _remaining_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WebSocketTimeout("WebSocket overall operation deadline expired")
    return remaining


def _recv_from_socket(
    sock,
    amount: int,
    stop_requested: Callable[[], bool],
    *,
    deadline: float | None = None,
    timeout_sec: float | None = None,
    wall_clock: Callable[[], float] | None = None,
    monotonic_clock_ns: Callable[[], int] | None = None,
) -> tuple[bytes, float, int]:
    _check_stop(stop_requested)
    if deadline is not None:
        remaining = _remaining_time(deadline)
        if timeout_sec is not None:
            remaining = min(remaining, timeout_sec)
        try:
            sock.settimeout(remaining)
        except OSError as exc:
            raise PublicWebSocketError(f"WebSocket timeout configuration failed: {exc}") from exc
    try:
        chunk = sock.recv(amount)
    except socket.timeout as exc:
        _check_stop(stop_requested)
        raise WebSocketTimeout("WebSocket receive timed out") from exc
    except OSError as exc:
        raise PublicWebSocketError(f"WebSocket receive failed: {exc}") from exc
    received_ts, received_monotonic_ns = _sample_receive_clock(
        time.time if wall_clock is None else wall_clock,
        time.monotonic_ns if monotonic_clock_ns is None else monotonic_clock_ns,
    )
    if not isinstance(chunk, (bytes, bytearray)):
        raise ProtocolError("socket.recv() returned a non-bytes value")
    if deadline is not None:
        _remaining_time(deadline)
    return bytes(chunk), received_ts, received_monotonic_ns


def _read_handshake(
    sock,
    *,
    max_handshake_bytes: int,
    stop_requested: Callable[[], bool],
    deadline: float,
    timeout_sec: float,
) -> tuple[bytes, bytes, float | None, int | None]:
    data = bytearray()
    marker = b"\r\n\r\n"
    last_received_ts: float | None = None
    last_monotonic_ns: int | None = None
    while True:
        position = data.find(marker)
        if position >= 0:
            end = position + len(marker)
            if end > max_handshake_bytes:
                raise HandshakeError("WebSocket handshake headers exceed the byte bound")
            remainder = bytes(data[end:])
            return (
                bytes(data[:end]),
                remainder,
                last_received_ts if remainder else None,
                last_monotonic_ns if remainder else None,
            )
        if len(data) >= max_handshake_bytes:
            raise HandshakeError("WebSocket handshake headers exceed the byte bound")
        chunk, chunk_received_ts, chunk_monotonic_ns = _recv_from_socket(
            sock,
            max_handshake_bytes + 1 - len(data),
            stop_requested,
            deadline=deadline,
            timeout_sec=timeout_sec,
        )
        if not chunk:
            raise HandshakeError("connection closed before WebSocket handshake completed")
        last_received_ts = chunk_received_ts
        last_monotonic_ns = chunk_monotonic_ns
        data.extend(chunk)
        if len(data) > max_handshake_bytes and marker not in data:
            raise HandshakeError("WebSocket handshake headers exceed the byte bound")


def _parse_handshake(response: bytes, *, expected_accept: str) -> None:
    try:
        text = response.decode("ascii")
    except UnicodeDecodeError as exc:
        raise HandshakeError("WebSocket handshake headers must be ASCII") from exc
    lines = text[:-4].split("\r\n")
    if not lines or not lines[0].startswith("HTTP/1.1 "):
        raise HandshakeError("WebSocket handshake status line must use HTTP/1.1")
    status_parts = lines[0].split(" ", 2)
    if len(status_parts) < 2 or not status_parts[1].isdigit():
        raise HandshakeError("malformed WebSocket handshake status")
    status = int(status_parts[1])
    if 300 <= status <= 399:
        raise RedirectNotAllowed("WebSocket handshake redirect refused")
    if status != 101:
        raise HandshakeError(f"WebSocket upgrade returned HTTP {status}")

    headers: dict[str, list[str]] = {}
    for raw_line in lines[1:]:
        if not raw_line or raw_line[0] in " \t" or ":" not in raw_line:
            raise HandshakeError("malformed WebSocket handshake header")
        name, value = raw_line.split(":", 1)
        if _HEADER_NAME.fullmatch(name) is None:
            raise HandshakeError("malformed WebSocket handshake header name")
        headers.setdefault(name.lower(), []).append(value.strip())

    for required in ("upgrade", "connection", "sec-websocket-accept"):
        if len(headers.get(required, ())) != 1:
            raise HandshakeError(f"WebSocket handshake requires one {required} header")
    upgrade_tokens = {token.strip().lower() for token in headers["upgrade"][0].split(",")}
    connection_tokens = {
        token.strip().lower() for token in headers["connection"][0].split(",")
    }
    if upgrade_tokens != {"websocket"}:
        raise HandshakeError("WebSocket Upgrade header is invalid")
    if "upgrade" not in connection_tokens:
        raise HandshakeError("WebSocket Connection header lacks Upgrade")
    if not secrets.compare_digest(headers["sec-websocket-accept"][0], expected_accept):
        raise HandshakeError("WebSocket accept proof does not match the client key")
    if "sec-websocket-extensions" in headers or "sec-websocket-protocol" in headers:
        raise HandshakeError("unsolicited WebSocket extension or subprotocol refused")


def _expected_accept(encoded_key: str) -> str:
    material = (encoded_key + _WEBSOCKET_GUID).encode("ascii")
    digest = hashlib.sha1(material, usedforsecurity=False).digest()
    return base64.b64encode(digest).decode("ascii")


def _valid_close_code(code: int) -> bool:
    if code in {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014}:
        return True
    return 3000 <= code <= 4999


@dataclass(frozen=True)
class _Frame:
    fin: bool
    opcode: int
    payload: bytes
    received_ts: float
    monotonic_ns: int


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _validated_receive_clock(received_ts: object, monotonic_ns: object) -> tuple[float, int]:
    if isinstance(received_ts, bool) or not isinstance(received_ts, (int, float)):
        raise PublicWebSocketError("WebSocket receive wall clock is invalid")
    rendered_received_ts = float(received_ts)
    if not math.isfinite(rendered_received_ts) or rendered_received_ts <= 0:
        raise PublicWebSocketError("WebSocket receive wall clock is invalid")
    if (
        isinstance(monotonic_ns, bool)
        or not isinstance(monotonic_ns, int)
        or monotonic_ns <= 0
    ):
        raise PublicWebSocketError("WebSocket receive monotonic clock is invalid")
    return rendered_received_ts, monotonic_ns


def _sample_receive_clock(
    wall_clock: Callable[[], float],
    monotonic_clock_ns: Callable[[], int],
) -> tuple[float, int]:
    try:
        received_ts = wall_clock()
        monotonic_ns = monotonic_clock_ns()
    except Exception as exc:
        raise PublicWebSocketError("WebSocket receive clock failed") from exc
    return _validated_receive_clock(received_ts, monotonic_ns)


@dataclass(frozen=True, slots=True, init=False)
class ReceivedJsonMessage:
    """One byte-exact application message; provenance, never capture authority.

    Construction is transport-internal.  A downstream evidence boundary must still
    parse and validate ``raw_payload`` itself instead of trusting ``decoded`` or this
    Python type as an authority token.
    """

    raw_payload: bytes
    decoded: Mapping[str, Any]
    application_ordinal: int
    data_frame_count: int
    control_frame_count: int
    received_ts: float
    monotonic_ns: int


def _received_json_message(
    *,
    raw_payload: bytes,
    decoded: Mapping[str, Any],
    application_ordinal: int,
    data_frame_count: int,
    control_frame_count: int,
    received_ts: float,
    monotonic_ns: int,
) -> ReceivedJsonMessage:
    try:
        immutable_decoded = _freeze_json(dict(decoded))
    except RecursionError as exc:
        raise ProtocolError("WebSocket JSON nesting exceeds the immutable result bound") from exc
    result = object.__new__(ReceivedJsonMessage)
    for name, value in (
        ("raw_payload", raw_payload),
        ("decoded", immutable_decoded),
        ("application_ordinal", application_ordinal),
        ("data_frame_count", data_frame_count),
        ("control_frame_count", control_frame_count),
        ("received_ts", received_ts),
        ("monotonic_ns", monotonic_ns),
    ):
        object.__setattr__(result, name, value)
    return result


class PublicWebSocket:
    """One bounded RFC 6455 connection with JSON text helpers."""

    def __init__(
        self,
        sock,
        *,
        initial_bytes: bytes,
        timeout_sec: float,
        max_frame_bytes: int,
        max_message_bytes: int,
        stop_requested: Callable[[], bool],
        overall_timeout_sec: float | None = None,
        initial_received_ts: float | None = None,
        initial_monotonic_ns: int | None = None,
        _wall_clock: Callable[[], float] | None = None,
        _monotonic_clock_ns: Callable[[], int] | None = None,
    ) -> None:
        self._sock = sock
        self._buffer = bytearray(initial_bytes)
        self._timeout_sec = timeout_sec
        self._overall_timeout_sec = _positive_float(
            "overall_timeout_sec",
            timeout_sec if overall_timeout_sec is None else overall_timeout_sec,
            maximum=HARD_MAX_TIMEOUT_SEC,
        )
        self._max_frame_bytes = max_frame_bytes
        self._max_message_bytes = max_message_bytes
        self._stop_requested = stop_requested
        self._wall_clock = time.time if _wall_clock is None else _wall_clock
        self._monotonic_clock_ns = (
            time.monotonic_ns if _monotonic_clock_ns is None else _monotonic_clock_ns
        )
        if not callable(self._wall_clock) or not callable(self._monotonic_clock_ns):
            raise TypeError("receive clocks must be callables")
        if initial_bytes:
            if initial_received_ts is None or initial_monotonic_ns is None:
                raise PublicWebSocketError(
                    "initial WebSocket bytes require their handshake receive clock"
                )
            self._initial_receive_clock: tuple[float, int] | None = (
                _validated_receive_clock(initial_received_ts, initial_monotonic_ns)
            )
        else:
            self._initial_receive_clock = None
        self._last_exact_receive_clock: tuple[float, int] | None = None
        self._closed = False
        self._close_sent = False
        self._application_ordinal = 0
        self._receive_lock = threading.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    def _read_exact(self, amount: int, *, deadline: float) -> bytes:
        result = bytearray()
        final_clock = self._last_exact_receive_clock
        while len(result) < amount:
            _check_stop(self._stop_requested)
            if self._buffer:
                take = min(amount - len(result), len(self._buffer))
                result.extend(self._buffer[:take])
                del self._buffer[:take]
                if self._initial_receive_clock is None:
                    raise PublicWebSocketError(
                        "handshake remainder lacks its final-byte receive clock"
                    )
                final_clock = self._initial_receive_clock
                continue
            chunk, received_ts, monotonic_ns = _recv_from_socket(
                self._sock,
                amount - len(result),
                self._stop_requested,
                deadline=deadline,
                timeout_sec=self._timeout_sec,
                wall_clock=self._wall_clock,
                monotonic_clock_ns=self._monotonic_clock_ns,
            )
            if not chunk:
                self._shutdown()
                raise WebSocketClosed("WebSocket TCP stream ended without a close frame")
            result.extend(chunk)
            final_clock = (received_ts, monotonic_ns)
        if amount:
            if final_clock is None:
                raise PublicWebSocketError("WebSocket frame lacks a final-byte receive clock")
            self._last_exact_receive_clock = final_clock
        return bytes(result)

    def _read_frame(self, *, deadline: float) -> _Frame:
        first, second = self._read_exact(2, deadline=deadline)
        fin = bool(first & 0x80)
        rsv = first & 0x70
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if rsv:
            raise ProtocolError("RSV bits are forbidden without negotiated extensions")
        if opcode not in _ALLOWED_OPCODES:
            raise ProtocolError(f"unsupported WebSocket opcode: {opcode}")
        if masked:
            raise ProtocolError("server-to-client WebSocket frames must not be masked")

        if length == 126:
            length = int.from_bytes(self._read_exact(2, deadline=deadline), "big")
            if length < 126:
                raise ProtocolError("non-minimal WebSocket frame length encoding")
        elif length == 127:
            length = int.from_bytes(self._read_exact(8, deadline=deadline), "big")
            if length & (1 << 63):
                raise ProtocolError("WebSocket frame length uses the reserved high bit")
            if length <= 0xFFFF:
                raise ProtocolError("non-minimal WebSocket frame length encoding")
        if length > self._max_frame_bytes:
            raise FrameTooLarge(
                f"WebSocket frame declares {length} bytes; limit is {self._max_frame_bytes}"
            )
        if opcode >= 0x8 and (not fin or length > 125):
            raise ProtocolError("WebSocket control frames must be final and at most 125 bytes")
        payload = self._read_exact(length, deadline=deadline)
        if opcode == 0x8 and len(payload) == 1:
            raise ProtocolError("WebSocket close payload cannot contain a one-byte code")
        if self._last_exact_receive_clock is None:
            raise PublicWebSocketError("WebSocket frame lacks a final-byte receive clock")
        received_ts, monotonic_ns = self._last_exact_receive_clock
        return _Frame(
            fin=fin,
            opcode=opcode,
            payload=payload,
            received_ts=received_ts,
            monotonic_ns=monotonic_ns,
        )

    def _send_frame(
        self,
        opcode: int,
        payload: bytes,
        *,
        deadline: float | None = None,
    ) -> None:
        if self._closed:
            raise WebSocketClosed("WebSocket is already closed")
        _check_stop(self._stop_requested)
        if opcode >= 0x8 and len(payload) > 125:
            raise ValueError("WebSocket control payload exceeds 125 bytes")
        if len(payload) > self._max_frame_bytes:
            raise MessageTooLarge("outbound message exceeds the frame byte bound")

        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = bytes((first, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((first, 0x80 | 126)) + length.to_bytes(2, "big")
        else:
            header = bytes((first, 0x80 | 127)) + length.to_bytes(8, "big")
        mask = os.urandom(4)
        if len(mask) != 4:
            raise PublicWebSocketError("mask source returned the wrong number of bytes")
        encoded = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if deadline is not None:
            try:
                self._sock.settimeout(
                    min(self._timeout_sec, _remaining_time(deadline))
                )
            except OSError as exc:
                raise PublicWebSocketError(
                    f"WebSocket timeout configuration failed: {exc}"
                ) from exc
        try:
            self._sock.sendall(header + mask + encoded)
        except socket.timeout as exc:
            raise WebSocketTimeout("WebSocket send timed out") from exc
        except OSError as exc:
            raise PublicWebSocketError(f"WebSocket send failed: {exc}") from exc
        if deadline is not None:
            _remaining_time(deadline)

    def send_json(self, value: Any) -> None:
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"value cannot be encoded as strict JSON: {exc}") from exc
        if len(encoded) > self._max_message_bytes or len(encoded) > self._max_frame_bytes:
            raise MessageTooLarge("outbound JSON exceeds the configured byte bounds")
        deadline = time.monotonic() + self._overall_timeout_sec
        try:
            self._send_frame(0x1, encoded, deadline=deadline)
        except PublicWebSocketError:
            self._shutdown()
            raise

    def _decode_json(self, payload: bytes) -> Any:
        if len(payload) > self._max_message_bytes:
            raise MessageTooLarge("WebSocket text message exceeds the configured byte bound")
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProtocolError("WebSocket text message is not valid UTF-8") from exc

        def reject_constant(value: str) -> None:
            raise ValueError(f"non-standard JSON constant is forbidden: {value}")

        def finite_float(value: str) -> float:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("non-finite JSON number is forbidden")
            return number

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON object key is forbidden: {key}")
                result[key] = value
            return result

        try:
            return json.loads(
                text,
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
                parse_float=finite_float,
            )
        except (json.JSONDecodeError, RecursionError, ValueError, OverflowError) as exc:
            raise ProtocolError(f"WebSocket text message is not valid JSON: {exc}") from exc

    def _receive_close(self, payload: bytes, *, deadline: float) -> None:
        code: int | None = None
        reason = ""
        if payload:
            code = int.from_bytes(payload[:2], "big")
            if not _valid_close_code(code):
                raise ProtocolError(f"invalid WebSocket close code: {code}")
            try:
                reason = payload[2:].decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ProtocolError("WebSocket close reason is not valid UTF-8") from exc
        if not self._close_sent:
            self._send_frame(0x8, payload, deadline=deadline)
            self._close_sent = True
        self._shutdown()
        raise WebSocketClosed("WebSocket peer closed the connection", code=code, reason=reason)

    def _complete_json_message(
        self,
        assembled_payload: bytes | bytearray,
        *,
        data_frame_count: int,
        control_frame_count: int,
        received_ts: float,
        monotonic_ns: int,
    ) -> ReceivedJsonMessage:
        received_ts, monotonic_ns = _validated_receive_clock(received_ts, monotonic_ns)

        raw_payload = bytes(assembled_payload)
        decoded = self._decode_json(raw_payload)
        if not isinstance(decoded, dict):
            raise ProtocolError("WebSocket JSON application message must be an object")
        next_ordinal = self._application_ordinal + 1
        result = _received_json_message(
            raw_payload=raw_payload,
            decoded=decoded,
            application_ordinal=next_ordinal,
            data_frame_count=data_frame_count,
            control_frame_count=control_frame_count,
            received_ts=received_ts,
            monotonic_ns=monotonic_ns,
        )
        self._application_ordinal = next_ordinal
        return result

    def recv_json_message(self) -> ReceivedJsonMessage:
        """Receive one JSON object while preserving its exact application bytes."""

        if self._closed:
            raise WebSocketClosed("WebSocket is already closed")
        if not self._receive_lock.acquire(blocking=False):
            self._shutdown()
            raise ProtocolError("concurrent WebSocket receive is forbidden")
        try:
            return self._recv_json_message_unlocked()
        finally:
            self._receive_lock.release()

    def _recv_json_message_unlocked(self) -> ReceivedJsonMessage:
        if self._closed:
            raise WebSocketClosed("WebSocket is already closed")
        deadline = time.monotonic() + self._overall_timeout_sec
        control_frames = 0
        data_frames = 0
        fragments: bytearray | None = None
        try:
            while True:
                frame = self._read_frame(deadline=deadline)
                if frame.opcode >= 0x8:
                    control_frames += 1
                    if control_frames > MAX_CONTROL_FRAMES_PER_MESSAGE:
                        raise ProtocolError("WebSocket control-frame budget exceeded")
                if frame.opcode == 0x8:
                    self._receive_close(frame.payload, deadline=deadline)
                if frame.opcode == 0x9:
                    self._send_frame(0xA, frame.payload, deadline=deadline)
                    continue
                if frame.opcode == 0xA:
                    continue
                if frame.opcode == 0x2:
                    raise ProtocolError("binary WebSocket messages are not accepted")
                if frame.opcode == 0x1:
                    data_frames += 1
                    if data_frames > MAX_DATA_FRAMES_PER_MESSAGE:
                        raise ProtocolError("WebSocket data-frame budget exceeded")
                    if fragments is not None:
                        raise ProtocolError("new text frame arrived before fragmented message ended")
                    if len(frame.payload) > self._max_message_bytes:
                        raise MessageTooLarge("WebSocket text message exceeds the byte bound")
                    if frame.fin:
                        return self._complete_json_message(
                            frame.payload,
                            data_frame_count=data_frames,
                            control_frame_count=control_frames,
                            received_ts=frame.received_ts,
                            monotonic_ns=frame.monotonic_ns,
                        )
                    fragments = bytearray(frame.payload)
                    continue
                if frame.opcode == 0x0:
                    data_frames += 1
                    if data_frames > MAX_DATA_FRAMES_PER_MESSAGE:
                        raise ProtocolError("WebSocket data-frame budget exceeded")
                    if fragments is None:
                        raise ProtocolError("continuation frame arrived without a fragmented message")
                    if len(fragments) + len(frame.payload) > self._max_message_bytes:
                        raise MessageTooLarge("fragmented WebSocket message exceeds the byte bound")
                    fragments.extend(frame.payload)
                    if frame.fin:
                        return self._complete_json_message(
                            fragments,
                            data_frame_count=data_frames,
                            control_frame_count=control_frames,
                            received_ts=frame.received_ts,
                            monotonic_ns=frame.monotonic_ns,
                        )
        except WebSocketClosed:
            raise
        except PublicWebSocketError:
            self._shutdown()
            raise

    def recv_json(self) -> Mapping[str, Any]:
        """Compatibility wrapper returning only the decoded JSON mapping."""

        return self.recv_json_message().decoded

    def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass

    def close(self, *, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        if isinstance(code, bool) or not isinstance(code, int) or not _valid_close_code(code):
            raise ValueError("invalid WebSocket close code")
        if not isinstance(reason, str):
            raise TypeError("close reason must be a string")
        try:
            encoded_reason = reason.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("close reason is not valid UTF-8") from exc
        if len(encoded_reason) > 123:
            raise ValueError("close reason exceeds 123 UTF-8 bytes")
        try:
            if not self._close_sent:
                try:
                    self._send_frame(
                        0x8,
                        code.to_bytes(2, "big") + encoded_reason,
                        deadline=time.monotonic() + self._overall_timeout_sec,
                    )
                    self._close_sent = True
                except StopRequested:
                    # Stop is an instruction to cease I/O, including a new close write.
                    pass
        finally:
            self._shutdown()

    def __enter__(self) -> "PublicWebSocket":
        return self

    def __exit__(self, *_args: object) -> bool:
        self.close()
        return False


def open_public_websocket(
    url: str,
    *,
    allowed_endpoints: Iterable[tuple[str, int, str]],
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    overall_timeout_sec: float = DEFAULT_OVERALL_TIMEOUT_SEC,
    max_handshake_bytes: int = DEFAULT_MAX_HANDSHAKE_BYTES,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    stop_requested: Callable[[], bool] | None = None,
) -> PublicWebSocket:
    """Open one exact, public, bounded WebSocket connection.

    There is intentionally no retry or reconnect branch.  A caller that needs a
    policy for subsequent connection epochs must implement and evidence it at a
    higher layer.
    """

    host, port, path = require_allowed_endpoint(url, allowed_endpoints=allowed_endpoints)
    timeout = _positive_float("timeout_sec", timeout_sec, maximum=HARD_MAX_TIMEOUT_SEC)
    overall_timeout = _positive_float(
        "overall_timeout_sec", overall_timeout_sec, maximum=HARD_MAX_TIMEOUT_SEC
    )
    handshake_limit = _positive_int(
        "max_handshake_bytes", max_handshake_bytes, maximum=HARD_MAX_HANDSHAKE_BYTES
    )
    frame_limit = _positive_int(
        "max_frame_bytes", max_frame_bytes, maximum=HARD_MAX_FRAME_BYTES
    )
    message_limit = _positive_int(
        "max_message_bytes", max_message_bytes, maximum=HARD_MAX_MESSAGE_BYTES
    )
    if stop_requested is None:
        stop_requested = lambda: False
    if not callable(stop_requested):
        raise TypeError("stop_requested must be callable")
    _check_stop(stop_requested)

    deadline = time.monotonic() + overall_timeout
    sock = _open_tls_socket(host, port=port, timeout_sec=min(timeout, overall_timeout))
    try:
        sock.settimeout(min(timeout, _remaining_time(deadline)))
        raw_key = os.urandom(16)
        if len(raw_key) != 16:
            raise HandshakeError("handshake key source returned the wrong number of bytes")
        encoded_key = base64.b64encode(raw_key).decode("ascii")
        host_header = host if port == 443 else f"{host}:{port}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {encoded_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        try:
            sock.settimeout(min(timeout, _remaining_time(deadline)))
            sock.sendall(request)
            _remaining_time(deadline)
        except socket.timeout as exc:
            raise WebSocketTimeout("WebSocket handshake send timed out") from exc
        except OSError as exc:
            raise HandshakeError(f"WebSocket handshake send failed: {exc}") from exc
        response, remainder, remainder_received_ts, remainder_monotonic_ns = _read_handshake(
            sock,
            max_handshake_bytes=handshake_limit,
            stop_requested=stop_requested,
            deadline=deadline,
            timeout_sec=timeout,
        )
        _parse_handshake(response, expected_accept=_expected_accept(encoded_key))
        return PublicWebSocket(
            sock,
            initial_bytes=remainder,
            timeout_sec=timeout,
            max_frame_bytes=frame_limit,
            max_message_bytes=message_limit,
            stop_requested=stop_requested,
            overall_timeout_sec=overall_timeout,
            initial_received_ts=remainder_received_ts,
            initial_monotonic_ns=remainder_monotonic_ns,
        )
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise
