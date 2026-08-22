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

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, Sequence

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


def split_endpoint(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(url)
    return parsed.netloc.lower(), parsed.path or "/"


def endpoint_is_allowed(url: str, allowed: Sequence[tuple[str, str]] | None = None) -> bool:
    allowed = config.ALLOWED_ENDPOINTS if allowed is None else allowed
    host, path = split_endpoint(url)
    return any(host == a_host and path.startswith(a_path) for a_host, a_path in allowed)


def require_allowed_endpoint(url: str, allowed: Sequence[tuple[str, str]] | None = None) -> None:
    if not endpoint_is_allowed(url, allowed):
        host, path = split_endpoint(url)
        raise EndpointNotAllowed(
            f"endpoint not declared in the plan: {host}{path}. Declare it in "
            f"ALLOWED_ENDPOINTS, reissue the PlanOnly, and have the change reviewed.",
            url=url,
        )


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


def build_opener() -> Any:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_json(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    timeout_sec: int = 20,
    max_retries: int = DEFAULT_MAX_RETRIES,
    opener: Any = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    allowed_endpoints: Sequence[tuple[str, str]] | None = None,
) -> Any:
    require_allowed_endpoint(url, allowed_endpoints)
    rng = rng or random.Random()
    opener = opener or build_opener()
    query = urllib.parse.urlencode(dict(params or {}))
    request_url = f"{url}?{query}" if query else url

    total_slept = 0.0
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        retry_after: float | None = None
        try:
            request = urllib.request.Request(
                request_url,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
            with opener.open(request, timeout=timeout_sec) as response:
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
