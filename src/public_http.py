"""One HTTP layer, and the runtime half of the endpoint allow-list.

The capability scan reads source and rejects a URL that is not declared. That catches
a literal, but a URL assembled at runtime - a base joined to a path, a venue name from
a config - is invisible to it. This module checks the address of every request as it is
made, so the static and dynamic halves of the same rule cannot disagree.

The retry policy is carried over from the spot project's audit rather than rediscovered:
identify yourself, retry only what is worth retrying, honour Retry-After, jitter the
backoff, bound the body, and treat a venue's error object as an error rather than as an
empty result.
"""

from __future__ import annotations

import ipaddress
import http.client
import json
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from types import MappingProxyType
from typing import Any, Callable, Mapping

import project_config as config


USER_AGENT = (
    "ZolotyayLopata-premarket-perp-capture/1.0 "
    "(research-only; public market data)"
)
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_RETRIES = 3
BASE_BACKOFF_SEC = 0.5
MAX_BACKOFF_SEC = 8.0
MAX_TOTAL_BACKOFF_SEC = 30.0
# 408 and 429 are transient by definition; any other 4xx says the request itself is
# wrong, and repeating it only burns the rate-limit budget.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# Query names are capabilities too.  A host/path allow-list that accepts arbitrary
# query fields can be widened into authenticated/private behavior without changing
# the nominal endpoint.  This table is deliberately local to the HTTP runtime: its
# implementation hash is PlanOnly-bound, and callers cannot replace it at runtime.
ALLOWED_QUERY_KEYS_BY_ENDPOINT: Mapping[tuple[str, str], frozenset[str]] = MappingProxyType({
    ("api.bybit.com", "/v5/market/instruments-info"): frozenset(
        {"category", "symbol", "status", "baseCoin", "limit", "cursor"}
    ),
    ("api.bybit.com", "/v5/market/tickers"): frozenset(
        {"category", "symbol", "baseCoin", "expDate"}
    ),
    ("api.bybit.com", "/v5/market/kline"): frozenset(
        {"category", "symbol", "interval", "start", "end", "limit"}
    ),
    ("api.bybit.com", "/v5/market/orderbook"): frozenset(
        {"category", "symbol", "limit"}
    ),
    ("api.bybit.com", "/v5/market/recent-trade"): frozenset(
        {"category", "symbol", "baseCoin", "optionType", "limit"}
    ),
    ("www.okx.com", "/api/v5/public/instruments"): frozenset(
        {"instType", "uly", "instFamily", "instId"}
    ),
    ("www.okx.com", "/api/v5/market/tickers"): frozenset(
        {"instType", "uly", "instFamily"}
    ),
    ("www.okx.com", "/api/v5/market/ticker"): frozenset({"instId"}),
    ("www.okx.com", "/api/v5/market/candles"): frozenset(
        {"instId", "after", "before", "bar", "limit"}
    ),
    ("www.okx.com", "/api/v5/market/books"): frozenset({"instId", "sz"}),
    ("www.okx.com", "/api/v5/market/trades"): frozenset({"instId", "limit"}),
    ("api.gateio.ws", "/api/v4/futures/usdt/contracts"): frozenset(),
    ("api.gateio.ws", "/api/v4/futures/usdt/tickers"): frozenset({"contract"}),
    ("api.gateio.ws", "/api/v4/futures/usdt/candlesticks"): frozenset(
        {"contract", "from", "to", "interval"}
    ),
    ("api.gateio.ws", "/api/v4/futures/usdt/order_book"): frozenset(
        {"contract", "interval", "limit", "with_id"}
    ),
    ("api.gateio.ws", "/api/v4/futures/usdt/trades"): frozenset(
        {"contract", "from", "to", "limit", "last_id", "reverse"}
    ),
})


class PublicHttpError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class EndpointNotAllowed(PublicHttpError):
    """The request address is not in the declared allow-list.

    Raised before any connection is opened: reach is a property of the plan, not of
    whatever a caller happened to assemble."""


class VenueErrorPayload(PublicHttpError):
    """The venue answered with an error object where data was expected."""


class DnsAddressNotAllowed(PublicHttpError):
    """DNS resolved an allowed hostname to a non-public address."""


class RedirectNotAllowed(EndpointNotAllowed):
    """Public-data requests are single-hop; redirects are not a capability."""


def _reject(url: str, reason: str) -> EndpointNotAllowed:
    return EndpointNotAllowed(f"endpoint not allowed: {reason}", url=url)


def _strict_split(url: str) -> tuple[urllib.parse.SplitResult, str, str]:
    if not isinstance(url, str) or not url:
        raise _reject(str(url), "URL must be a non-empty string")
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
        raise _reject(url, "whitespace and control characters are forbidden")
    if "\\" in url:
        raise _reject(url, "backslashes are forbidden")
    if "#" in url:
        raise _reject(url, "fragments are forbidden")

    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise _reject(url, f"malformed authority: {exc}") from exc

    if parsed.scheme.lower() != "https":
        raise _reject(url, "scheme must be HTTPS")
    if not parsed.netloc or parsed.hostname is None:
        raise _reject(url, "hostname is required")
    if parsed.username is not None or parsed.password is not None:
        raise _reject(url, "userinfo is forbidden")
    if port not in (None, 443):
        raise _reject(url, "only the HTTPS port 443 is allowed")
    if parsed.fragment:
        raise _reject(url, "fragments are forbidden")

    host = parsed.hostname.lower()
    path = parsed.path or "/"
    # No allowed endpoint needs path encoding.  Rejecting all encoded path octets is
    # stronger than trying to predict which intermediary decodes %2f, %5c or %2e twice.
    if "%" in path:
        raise _reject(url, "percent-encoded path octets are forbidden")
    if any(segment in (".", "..") for segment in path.split("/")):
        raise _reject(url, "dot-segments are forbidden")
    return parsed, host, path


def split_endpoint(url: str) -> tuple[str, str]:
    _parsed, host, path = _strict_split(url)
    return host, path


def endpoint_is_allowed(url: str) -> bool:
    try:
        host, path = split_endpoint(url)
    except EndpointNotAllowed:
        return False
    return (host, path) in config.ALLOWED_ENDPOINTS


def _require_allowed_query(parsed: urllib.parse.SplitResult, endpoint: tuple[str, str]) -> None:
    allowed_keys = ALLOWED_QUERY_KEYS_BY_ENDPOINT.get(endpoint)
    if allowed_keys is None:
        raise _reject(parsed.geturl(), "endpoint has no declared query policy")
    try:
        items = urllib.parse.parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            separator="&",
        )
    except ValueError as exc:
        raise _reject(parsed.geturl(), f"malformed query: {exc}") from exc

    seen: set[str] = set()
    for key, _value in items:
        if not key:
            raise _reject(parsed.geturl(), "empty query key")
        if key in seen:
            raise _reject(parsed.geturl(), f"duplicate query key: {key}")
        if key not in allowed_keys:
            raise _reject(parsed.geturl(), f"query key is not declared: {key}")
        seen.add(key)


def require_allowed_endpoint(url: str) -> None:
    parsed, host, path = _strict_split(url)
    endpoint = (host, path)
    if endpoint not in config.ALLOWED_ENDPOINTS:
        raise EndpointNotAllowed(
            f"endpoint not declared in the plan: {host}{path}. Declare it in "
            f"ALLOWED_ENDPOINTS, reissue the PlanOnly, and have the change reviewed.",
            url=url,
        )
    _require_allowed_query(parsed, endpoint)


def require_public_dns(url: str) -> tuple[str, ...]:
    """Resolve immediately before connect and reject the entire mixed answer set.

    Accepting one public address from a set that also contains a loopback/private
    address would leave address selection to a lower layer and defeat the check.
    """
    _parsed, host, _path = _strict_split(url)
    try:
        answers = socket.getaddrinfo(
            host,
            443,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise PublicHttpError(f"DNS resolution failed for {host}: {exc}", url=url) from exc
    if not answers:
        raise PublicHttpError(f"DNS returned no addresses for {host}", url=url)

    addresses: list[str] = []
    for answer in answers:
        try:
            raw_address = str(answer[4][0]).split("%", 1)[0]
            address = ipaddress.ip_address(raw_address)
        except (IndexError, TypeError, ValueError) as exc:
            raise DnsAddressNotAllowed(
                f"DNS returned a non-IP address for {host}", url=url
            ) from exc
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
            or not address.is_global
        ):
            raise DnsAddressNotAllowed(
                f"DNS resolved {host} to non-public address {address}", url=url
            )
        addresses.append(str(address))
    return tuple(addresses)


def _require_public_ip_literal(raw_address: str, *, url: str) -> str:
    try:
        address = ipaddress.ip_address(str(raw_address).split("%", 1)[0])
    except ValueError as exc:
        raise DnsAddressNotAllowed(
            f"validated connection target is not an IP address: {raw_address}",
            url=url,
        ) from exc
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or not address.is_global
    ):
        raise DnsAddressNotAllowed(
            f"validated connection target is not public: {address}",
            url=url,
        )
    return str(address)


def _looks_like_error_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    # Bybit answers 200 with retCode != 0; OKX with code != "0"; Gate with label/message.
    ret_code = payload.get("retCode")
    if ret_code not in (None, 0, "0"):
        return f"retCode={ret_code} retMsg={payload.get('retMsg')}"
    code = payload.get("code")
    if code not in (None, 0, "0") and "data" in payload:
        return f"code={code} msg={payload.get('msg')}"
    if "label" in payload and "message" in payload:
        return f"label={payload.get('label')} message={payload.get('message')}"
    return None


def _retry_after_sec(headers: Any) -> float | None:
    try:
        raw = headers.get("Retry-After") if headers is not None else None
    except AttributeError:
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        return None


def backoff_delay(attempt: int, *, rng: random.Random) -> float:
    """Exponential with full jitter: un-jittered backoff wakes every caller at once,
    which is precisely wrong against a venue that has just rate-limited us."""
    return rng.uniform(0.0, min(MAX_BACKOFF_SEC, BASE_BACKOFF_SEC * (2 ** attempt)))


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # noqa: PLR0913
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise RedirectNotAllowed(
            f"redirects are disabled: HTTP {code} to {newurl}",
            status=code,
            url=req.full_url,
        )


class BoundHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose TCP peer is a previously validated DNS answer.

    `HTTPSConnection` normally resolves `self.host` inside `connect()`.  Resolving once
    for policy and again for the socket leaves a DNS-rebinding gap.  This connection
    opens TCP to the validated literal while retaining the venue hostname for TLS SNI,
    certificate verification and the HTTP Host header managed by urllib.
    """

    def __init__(
        self,
        host: str,
        *,
        resolved_address: str,
        **kwargs: Any,
    ) -> None:
        self.resolved_address = _require_public_ip_literal(
            resolved_address,
            url=urllib.parse.urlunsplit(("https", host, "/", "", "")),
        )
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self.resolved_address, self.port),
            self.timeout,
            self.source_address,
        )
        server_hostname = self.host
        if self._tunnel_host:
            self._tunnel()
            server_hostname = self._tunnel_host
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=server_hostname,
        )


class BoundHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, *, expected_host: str, resolved_address: str) -> None:
        super().__init__()
        self.expected_host = expected_host.lower()
        self.resolved_address = _require_public_ip_literal(
            resolved_address,
            url=urllib.parse.urlunsplit(("https", expected_host, "/", "", "")),
        )

    def https_open(self, request: Any) -> Any:
        request_host = urllib.parse.urlsplit(request.full_url).hostname
        if request_host is None or request_host.lower() != self.expected_host:
            raise EndpointNotAllowed(
                "bound HTTPS handler host does not match the validated endpoint",
                url=request.full_url,
            )

        def connection_factory(host: str, **kwargs: Any) -> BoundHTTPSConnection:
            authority_host = urllib.parse.urlsplit(f"//{host}").hostname
            if authority_host is None or authority_host.lower() != self.expected_host:
                raise EndpointNotAllowed(
                    "connection authority changed after endpoint validation",
                    url=request.full_url,
                )
            return BoundHTTPSConnection(
                host,
                resolved_address=self.resolved_address,
                **kwargs,
            )

        return self.do_open(connection_factory, request)


def build_bound_opener(
    request_url: str,
    validated_addresses: tuple[str, ...],
    *,
    attempt: int,
) -> Any:
    require_allowed_endpoint(request_url)
    _parsed, host, _path = _strict_split(request_url)
    if not validated_addresses:
        raise PublicHttpError(f"no validated addresses for {host}", url=request_url)
    addresses = tuple(
        _require_public_ip_literal(address, url=request_url)
        for address in validated_addresses
    )
    selected = addresses[attempt % len(addresses)]
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirectHandler(),
        BoundHTTPSHandler(expected_host=host, resolved_address=selected),
    )


def _build_request_url(url: str, params: Mapping[str, Any] | None) -> str:
    require_allowed_endpoint(url)
    parsed = urllib.parse.urlsplit(url)
    existing = urllib.parse.parse_qsl(
        parsed.query,
        keep_blank_values=True,
        strict_parsing=True,
        separator="&",
    )
    seen = {key for key, _value in existing}
    additions: list[tuple[str, Any]] = []
    for raw_key, value in (params or {}).items():
        if not isinstance(raw_key, str) or not raw_key:
            raise _reject(url, "parameter keys must be non-empty strings")
        if raw_key in seen:
            raise _reject(url, f"duplicate query key: {raw_key}")
        seen.add(raw_key)
        additions.append((raw_key, value))
    query = urllib.parse.urlencode([*existing, *additions])
    request_url = urllib.parse.urlunsplit(parsed._replace(query=query))
    # This second check is intentional: params are untrusted caller input and must be
    # validated after they have become the actual on-wire URL.
    require_allowed_endpoint(request_url)
    return request_url


def get_json(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    timeout_sec: int = 20,
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> Any:
    request_url = _build_request_url(url, params)
    rng = rng or random.Random()

    total_slept = 0.0
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        retry_after: float | None = None
        try:
            # Repeat immediately before every connection attempt: DNS is mutable, and
            # reusing a result from a previous retry creates a rebinding gap.
            validated_addresses = require_public_dns(request_url)
            opener = build_bound_opener(
                request_url,
                validated_addresses,
                attempt=attempt,
            )
            request = urllib.request.Request(
                request_url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
            with opener.open(request, timeout=timeout_sec) as response:
                final_url = response.geturl()
                require_allowed_endpoint(final_url)
                if final_url != request_url:
                    raise RedirectNotAllowed(
                        f"redirected response URL is forbidden: {final_url}",
                        url=request_url,
                    )
                body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise PublicHttpError(
                    f"response exceeds {MAX_RESPONSE_BYTES} bytes", url=request_url
                )
            payload = json.loads(body.decode("utf-8"))
            detail = _looks_like_error_payload(payload)
            if detail is not None:
                raise VenueErrorPayload(
                    f"venue returned an error payload: {detail}", url=request_url
                )
            return payload
        except urllib.error.HTTPError as exc:
            last_error = PublicHttpError(
                f"HTTP {exc.code} for {request_url}", status=exc.code, url=request_url
            )
            if exc.code not in RETRYABLE_STATUS:
                raise last_error from exc
            retry_after = _retry_after_sec(getattr(exc, "headers", None))
        except VenueErrorPayload:
            raise  # the venue understood us and said no
        except (EndpointNotAllowed, DnsAddressNotAllowed, RedirectNotAllowed):
            raise  # capability failures are permanent and must not be retried
        except PublicHttpError:
            raise
        except (OSError, ValueError) as exc:
            last_error = exc

        if attempt >= max_retries:
            break
        delay = retry_after if retry_after is not None else backoff_delay(attempt, rng=rng)
        if total_slept + delay > MAX_TOTAL_BACKOFF_SEC:
            break
        total_slept += delay
        sleep_fn(delay)

    if isinstance(last_error, PublicHttpError):
        raise last_error
    raise PublicHttpError(f"GET failed: {request_url}") from last_error
