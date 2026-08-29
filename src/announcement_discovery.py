"""Bounded discovery of official listing-announcement candidates.

The official index is a lead, not authority. This module never fetches an article
body, never chooses a time from prose, never writes the event registry and never calls
the attestation or capture runtimes. It appends only unverified candidates that a
person can inspect later.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import announcement_candidate_store as candidate_store
import event_registry as registry
import project_config as config
import public_http
import risk_gate
from canonical_hash import canonical_hash


EVIDENCE_CLASS = "UNVERIFIED_ANNOUNCEMENT_DISCOVERY"
CANDIDATE_SCHEMA = "premarket_official_announcement_candidate_v1"
BYBIT_PAGE_SIZE = 20
BITGET_PAGE_SIZE = 10
KUCOIN_PAGE_SIZE = 50
SOURCE_ENDPOINTS: Mapping[str, tuple[str, Mapping[str, str], str]] = {
    "bybit": (
        "https://api.bybit.com/v5/announcements/index",
        {"locale": "en-US", "type": "new_crypto", "limit": str(BYBIT_PAGE_SIZE)},
        "page",
    ),
    "bitget": (
        "https://api.bitget.com/api/v2/public/annoucements",
        {
            "language": "en_US",
            "annType": "coin_listings",
            "limit": str(BITGET_PAGE_SIZE),
        },
        "cursor",
    ),
    "kucoin": (
        "https://api.kucoin.com/api/v3/announcements",
        {
            "pageSize": str(KUCOIN_PAGE_SIZE),
            "annType": "new-listings",
            "lang": "en_US",
        },
        "currentPage",
    ),
}


class AnnouncementDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class AnnouncementPage:
    articles: tuple[dict[str, Any], ...]
    next_page: int | str | None


def utc_from_ts(value: int) -> str:
    return datetime.fromtimestamp(int(value), timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _required_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AnnouncementDiscoveryError(f"{field} must be an object")
    return value


def _required_list(value: Any, *, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise AnnouncementDiscoveryError(f"{field} must be an array")
    return value


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise AnnouncementDiscoveryError(f"{field} must be a non-negative integer")
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or value < 0:
        raise AnnouncementDiscoveryError(f"{field} must be a non-negative integer")
    return value


def _optional_epoch_ms(value: Any, *, field: str) -> int | None:
    if value is None or value == "":
        return None
    parsed = _nonnegative_int(value, field=field)
    return parsed if parsed > 0 else None


def _canonical_article_url(listing_venue: str, value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AnnouncementDiscoveryError("article URL must be a canonical string")
    if "\\" in value:
        raise AnnouncementDiscoveryError("article URL contains a backslash")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.fragment
    ):
        raise AnnouncementDiscoveryError("article URL is not canonical HTTPS")
    host = parsed.hostname.lower()
    allowed = config.OFFICIAL_ANNOUNCEMENT_HOSTS.get(listing_venue, ())
    if host not in allowed:
        raise AnnouncementDiscoveryError(
            f"article host is not official for {listing_venue}: {host}"
        )
    path = parsed.path
    if not path.startswith("/") or "/../" in urllib.parse.unquote(path):
        raise AnnouncementDiscoveryError("article path is not canonical")
    if listing_venue == "bybit" and not (
        host == "announcements.bybit.com" or path.startswith("/en/help-center/")
    ):
        raise AnnouncementDiscoveryError(
            "Bybit article path is outside the official index"
        )
    if listing_venue == "bitget" and "/support/articles/" not in path:
        raise AnnouncementDiscoveryError(
            "Bitget article path is outside the support index"
        )
    if listing_venue == "kucoin" and not path.startswith("/announcement/"):
        raise AnnouncementDiscoveryError(
            "KuCoin article path is outside the announcement index"
        )
    if parsed.query:
        if listing_venue != "kucoin" or urllib.parse.parse_qsl(
            parsed.query, keep_blank_values=True
        ) != [("lang", "en_US")]:
            raise AnnouncementDiscoveryError("article URL query is not canonical")
    return urllib.parse.urlunsplit(("https", host, path, "", ""))


def _article(
    *,
    listing_venue: str,
    article_id: Any,
    title: Any,
    url: Any,
    published_at_ms: Any,
    source_page: int,
    source_payload_sha256: str,
) -> dict[str, Any]:
    if (
        not isinstance(article_id, str)
        or not article_id
        or article_id != article_id.strip()
    ):
        raise AnnouncementDiscoveryError("article id must be a canonical string")
    if not isinstance(title, str) or not title.strip() or title != title.strip():
        raise AnnouncementDiscoveryError("article title must be a canonical string")
    if len(title) > 1000:
        raise AnnouncementDiscoveryError("article title exceeds the bound")
    return {
        "article_id": article_id,
        "title": title,
        "url": _canonical_article_url(listing_venue, url),
        "published_at_ms": _optional_epoch_ms(
            published_at_ms, field="published_at_ms"
        ),
        "source_page": int(source_page),
        "source_payload_sha256": source_payload_sha256,
    }


def parse_bybit_page(payload: Any, *, page: int) -> AnnouncementPage:
    root = _required_mapping(payload, field="Bybit payload")
    code = root.get("retCode")
    if isinstance(code, bool) or str(code) != "0":
        raise AnnouncementDiscoveryError(
            "Bybit announcement response is not successful"
        )
    result = _required_mapping(root.get("result"), field="Bybit result")
    rows = _required_list(result.get("list"), field="Bybit result.list")
    total = _nonnegative_int(result.get("total"), field="Bybit result.total")
    payload_hash = canonical_hash(root)
    articles: list[dict[str, Any]] = []
    for row_raw in rows:
        row = _required_mapping(row_raw, field="Bybit article")
        row_type = _required_mapping(row.get("type"), field="Bybit article.type")
        tags = _required_list(row.get("tags"), field="Bybit article.tags")
        if row_type.get("key") != "new_crypto" or not any(
            str(tag).strip().lower() in {"spot", "spot listings"} for tag in tags
        ):
            raise AnnouncementDiscoveryError(
                "Bybit response contains a non-spot new_crypto row"
            )
        url = row.get("url")
        parsed = urllib.parse.urlsplit(str(url or ""))
        article_id = parsed.path.rstrip("/").split("/")[-1]
        articles.append(
            _article(
                listing_venue="bybit",
                article_id=article_id,
                title=row.get("title"),
                url=url,
                published_at_ms=row.get("publishTime"),
                source_page=page,
                source_payload_sha256=payload_hash,
            )
        )
    next_page = page + 1 if page * BYBIT_PAGE_SIZE < total else None
    return AnnouncementPage(tuple(articles), next_page)


def parse_bitget_page(payload: Any, *, page: int) -> AnnouncementPage:
    root = _required_mapping(payload, field="Bitget payload")
    if str(root.get("code")) != "00000":
        raise AnnouncementDiscoveryError(
            "Bitget announcement response is not successful"
        )
    rows = _required_list(root.get("data"), field="Bitget data")
    payload_hash = canonical_hash(root)
    articles: list[dict[str, Any]] = []
    for row_raw in rows:
        row = _required_mapping(row_raw, field="Bitget article")
        if (
            row.get("annType") != "coin_listings"
            or row.get("annSubType") != "spot"
        ):
            continue
        articles.append(
            _article(
                listing_venue="bitget",
                article_id=row.get("annId"),
                title=row.get("annTitle"),
                url=row.get("annUrl"),
                published_at_ms=row.get("cTime"),
                source_page=page,
                source_payload_sha256=payload_hash,
            )
        )
    next_page: str | None = None
    if len(rows) == BITGET_PAGE_SIZE:
        final_row = _required_mapping(rows[-1], field="Bitget final article")
        cursor = final_row.get("annId")
        if not isinstance(cursor, str) or not cursor.strip():
            raise AnnouncementDiscoveryError("Bitget cursor is missing or invalid")
        next_page = cursor
    return AnnouncementPage(tuple(articles), next_page)


def parse_kucoin_page(payload: Any, *, page: int) -> AnnouncementPage:
    root = _required_mapping(payload, field="KuCoin payload")
    if str(root.get("code")) != "200000":
        raise AnnouncementDiscoveryError(
            "KuCoin announcement response is not successful"
        )
    data = _required_mapping(root.get("data"), field="KuCoin data")
    rows = _required_list(data.get("items"), field="KuCoin data.items")
    total_pages = _nonnegative_int(
        data.get("totalPage"), field="KuCoin data.totalPage"
    )
    current_page = _nonnegative_int(
        data.get("currentPage"), field="KuCoin data.currentPage"
    )
    page_size = _nonnegative_int(
        data.get("pageSize"), field="KuCoin data.pageSize"
    )
    if current_page != page or page_size <= 0 or page_size > KUCOIN_PAGE_SIZE:
        raise AnnouncementDiscoveryError("KuCoin pagination echo is invalid")
    payload_hash = canonical_hash(root)
    articles: list[dict[str, Any]] = []
    for row_raw in rows:
        row = _required_mapping(row_raw, field="KuCoin article")
        types = _required_list(row.get("annType"), field="KuCoin article.annType")
        if "new-listings" not in types:
            raise AnnouncementDiscoveryError(
                "KuCoin response contains a non-listing announcement"
            )
        articles.append(
            _article(
                listing_venue="kucoin",
                article_id=str(row.get("annId") or ""),
                title=row.get("annTitle"),
                url=row.get("annUrl"),
                published_at_ms=row.get("cTime"),
                source_page=page,
                source_payload_sha256=payload_hash,
            )
        )
    next_page = page + 1 if page < total_pages else None
    return AnnouncementPage(tuple(articles), next_page)


PARSERS = {
    "bybit": parse_bybit_page,
    "bitget": parse_bitget_page,
    "kucoin": parse_kucoin_page,
}


def flatten_unique_pages(
    pages: Sequence[AnnouncementPage],
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    articles: list[dict[str, Any]] = []
    for page in pages:
        for raw in page.articles:
            article = dict(raw)
            article_id = str(article.get("article_id") or "")
            if not article_id or article_id in seen:
                raise AnnouncementDiscoveryError(
                    f"duplicate or missing article id across pages: {article_id!r}"
                )
            seen.add(article_id)
            articles.append(article)
    if len(articles) > config.ANNOUNCEMENT_MAX_ARTICLES_PER_VENUE:
        raise AnnouncementDiscoveryError("announcement article budget exceeded")
    return articles


def discover_venue(venue: str) -> list[dict[str, Any]]:
    if venue not in SOURCE_ENDPOINTS or venue not in config.ANNOUNCEMENT_VENUES:
        raise AnnouncementDiscoveryError(
            f"unsupported announcement venue: {venue}"
        )
    endpoint, fixed_params, page_key = SOURCE_ENDPOINTS[venue]
    parser = PARSERS[venue]
    pages: list[AnnouncementPage] = []
    page_number = 1
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while page_number <= config.ANNOUNCEMENT_MAX_PAGES:
        params = dict(fixed_params)
        if venue == "bitget":
            if cursor is not None:
                params[page_key] = cursor
        else:
            params[page_key] = str(page_number)
        payload = public_http.get_json(
            endpoint,
            params=params,
            max_retries=0,
        )
        parsed = parser(payload, page=page_number)
        pages.append(parsed)
        if parsed.next_page is None:
            break
        if venue == "bitget":
            if (
                not isinstance(parsed.next_page, str)
                or parsed.next_page in seen_cursors
            ):
                raise AnnouncementDiscoveryError(
                    "announcement cursor is missing, repeated or malformed"
                )
            seen_cursors.add(parsed.next_page)
            cursor = parsed.next_page
            page_number += 1
        else:
            if parsed.next_page != page_number + 1:
                raise AnnouncementDiscoveryError(
                    "announcement pagination is non-monotonic"
                )
            page_number = parsed.next_page
    return flatten_unique_pages(pages)


def title_mentions_ticker(title: str, ticker: str) -> bool:
    title_upper = str(title or "").upper()
    ticker_upper = str(ticker or "").upper()
    if not ticker_upper or not re.fullmatch(r"[A-Z0-9]{1,32}", ticker_upper):
        return False
    return (
        re.search(
            rf"(?<![A-Z0-9]){re.escape(ticker_upper)}(?![A-Z0-9])",
            title_upper,
        )
        is not None
    )


def make_candidate(
    *,
    target: Mapping[str, Any],
    listing_venue: str,
    article: Mapping[str, Any],
    detected_at_utc: str,
) -> dict[str, Any]:
    issuer_id = str(target.get("issuer_id") or "")
    title = str(article.get("title") or "")
    if not title_mentions_ticker(title, issuer_id):
        raise AnnouncementDiscoveryError(
            "article title has no exact target ticker token"
        )
    candidate_id = candidate_store.make_candidate_id(
        episode_id=str(target.get("episode_id") or ""),
        listing_venue=listing_venue,
        article_id=str(article.get("article_id") or ""),
    )
    lineage_fields = (
        "episode_id",
        "perpetual_venue",
        "premarket_contract_id",
        "lifecycle_generation",
        "asset_class",
        "issuer_namespace",
        "issuer_id",
        "asset_identity_hash",
        "registry_sha256",
        "registry_tail_record_hash",
        "mutation_receipt_seq",
        "mutation_receipt_hash",
        "summary_content_hash",
        "registry_authority_state_hash",
        "plan_id",
        "plan_hash",
        "metadata_refresh_received_at",
    )
    candidate = {
        "schema": CANDIDATE_SCHEMA,
        "candidate_id": candidate_id,
        "evidence_class": EVIDENCE_CLASS,
        "review_state": "HUMAN_ATTESTATION_REQUIRED",
        "identity_match_basis": "EXACT_TICKER_TOKEN_HEURISTIC_ONLY",
        "identity_authority": "NONE_UNTIL_HUMAN_SAME_UNDERLYING_ATTESTATION",
        "listing_venue": listing_venue,
        "article_id": str(article["article_id"]),
        "article_title": title,
        "article_url": str(article["url"]),
        "article_published_at_ms": article.get("published_at_ms"),
        "source_page": int(article.get("source_page", 0) or 0),
        "source_payload_sha256": str(
            article.get("source_payload_sha256") or ""
        ),
        "detected_at_utc": detected_at_utc,
        "article_body_fetched": False,
        "registry_write": False,
        "human_attestation_required": True,
    }
    for field in lineage_fields:
        candidate[field] = target.get(field)
    return candidate


def _preflight(run_id: str) -> dict[str, Any]:
    try:
        receipt = risk_gate.preflight(
            write_class="announcement_discovery",
            run_id=run_id,
        )
    except Exception as exc:  # noqa: BLE001
        raise AnnouncementDiscoveryError(
            f"PREFLIGHT_BLOCKED: {type(exc).__name__}: {exc}"
        ) from exc
    if (
        receipt.get("ok") is not True
        or receipt.get("verified") is not True
        or receipt.get("decision") != "ALLOW_ANNOUNCEMENT_DISCOVERY"
        or receipt.get("write_class") != "announcement_discovery"
        or receipt.get("action") != risk_gate.ANNOUNCEMENT_DISCOVERY_ACTION
        or receipt.get("run_id") != run_id
    ):
        raise AnnouncementDiscoveryError(
            "PREFLIGHT_BLOCKED: receipt is not exact"
        )
    return receipt


def _same_target_authority(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> bool:
    if (
        before.get("status") != "TARGETS_READY"
        or after.get("status") != "TARGETS_READY"
    ):
        return False
    fields = (
        "episode_id",
        "lifecycle_generation",
        "registry_sha256",
        "registry_tail_record_hash",
        "mutation_receipt_hash",
        "summary_content_hash",
        "registry_authority_state_hash",
        "plan_id",
        "plan_hash",
    )
    before_rows = {
        str(item.get("episode_id")): tuple(item.get(field) for field in fields)
        for item in before.get("targets") or []
    }
    after_rows = {
        str(item.get("episode_id")): tuple(item.get(field) for field in fields)
        for item in after.get("targets") or []
    }
    return before_rows == after_rows


def run_discovery(*, now_ts: int | None = None) -> dict[str, Any]:
    tick_ts = int(time.time()) if now_ts is None else int(now_ts)
    detected_at_utc = utc_from_ts(tick_ts)
    selected = registry.select_unattested_crypto_premarket_episodes(
        now_ts=tick_ts
    )
    if selected.get("status") != "TARGETS_READY":
        return {
            **dict(selected),
            "announcement_requests": 0,
            "appended_candidates": 0,
            "pending_retry": selected.get("status")
            in {"METADATA_REFRESH_REQUIRED", "REGISTRY_RECOVERY_REQUIRED"},
            "capture_authorized": False,
        }

    run_id = "announcement_" + datetime.fromtimestamp(
        tick_ts, timezone.utc
    ).strftime("%Y%m%dT%H%M%SZ")
    try:
        initial_preflight = _preflight(run_id)
    except AnnouncementDiscoveryError as exc:
        return {
            "status": "RETRY_NEXT_INTERVAL",
            "reason": str(exc),
            "announcement_requests": 0,
            "appended_candidates": 0,
            "pending_retry": True,
            "capture_authorized": False,
        }

    venue_articles: dict[str, list[dict[str, Any]]] = {}
    venue_errors: dict[str, str] = {}
    announcement_requests = 0
    candidates: list[dict[str, Any]] = []
    with candidate_store.candidate_store_lock(
        config.ANNOUNCEMENT_CANDIDATE_PATH,
        run_id=run_id,
    ) as lock_owner:
        for venue in config.ANNOUNCEMENT_VENUES:
            try:
                articles = discover_venue(venue)
                venue_articles[venue] = articles
                announcement_requests += max(
                    1,
                    max(
                        (
                            int(item.get("source_page", 1))
                            for item in articles
                        ),
                        default=1,
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                venue_errors[venue] = f"{type(exc).__name__}: {exc}"
                continue
        for item in selected.get("targets") or []:
            ticker = str(item.get("issuer_id") or "")
            for venue, articles in venue_articles.items():
                for article in articles:
                    if title_mentions_ticker(
                        str(article.get("title") or ""), ticker
                    ):
                        candidates.append(
                            make_candidate(
                                target=item,
                                listing_venue=venue,
                                article=article,
                                detected_at_utc=detected_at_utc,
                            )
                        )

        if len(venue_errors) == len(config.ANNOUNCEMENT_VENUES):
            return {
                "status": "RETRY_NEXT_INTERVAL",
                "reason": "all official announcement indexes failed",
                "venue_errors": dict(sorted(venue_errors.items())),
                "announcement_requests": announcement_requests,
                "appended_candidates": 0,
                "pending_retry": True,
                "capture_authorized": False,
            }
        selected_after = registry.select_unattested_crypto_premarket_episodes(
            now_ts=tick_ts
        )
        if not _same_target_authority(selected, selected_after):
            return {
                "status": "RETRY_NEXT_INTERVAL",
                "reason": (
                    "registry authority changed during announcement discovery"
                ),
                "venue_errors": dict(sorted(venue_errors.items())),
                "announcement_requests": announcement_requests,
                "appended_candidates": 0,
                "pending_retry": True,
                "capture_authorized": False,
            }
        try:
            commit_preflight = _preflight(run_id)
        except AnnouncementDiscoveryError as exc:
            return {
                "status": "RETRY_NEXT_INTERVAL",
                "reason": str(exc),
                "venue_errors": dict(sorted(venue_errors.items())),
                "announcement_requests": announcement_requests,
                "appended_candidates": 0,
                "pending_retry": True,
                "capture_authorized": False,
            }
        if any(
            commit_preflight.get(field) != initial_preflight.get(field)
            for field in ("plan_id", "plan_hash", "resolved_paths_hash")
        ):
            return {
                "status": "RETRY_NEXT_INTERVAL",
                "reason": "write authority changed before candidate append",
                "venue_errors": dict(sorted(venue_errors.items())),
                "announcement_requests": announcement_requests,
                "appended_candidates": 0,
                "pending_retry": True,
                "capture_authorized": False,
            }
        append_result = candidate_store.append_candidates(
            config.ANNOUNCEMENT_CANDIDATE_PATH,
            candidates,
            run_id=run_id,
            lock_owner=lock_owner,
        )

    partial = bool(venue_errors)
    appended = int(append_result.get("appended_records", 0) or 0)
    if partial:
        status = "PARTIAL_RETRY_NEXT_INTERVAL"
    elif candidates:
        status = "CANDIDATES_RECORDED_HUMAN_ATTESTATION_REQUIRED"
    else:
        status = "NO_MATCHING_ANNOUNCEMENTS"
    return {
        "status": status,
        "targets": len(selected.get("targets") or []),
        "matched_candidates": len(candidates),
        "appended_candidates": appended,
        "duplicate_candidates": int(
            append_result.get("duplicate_records", 0) or 0
        ),
        "announcement_requests": announcement_requests,
        "venue_errors": dict(sorted(venue_errors.items())),
        "pending_retry": partial,
        "human_attestation_required": bool(candidates),
        "capture_authorized": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Discover unverified official listing announcement candidates."
        )
    )
    parser.add_argument("--scheduled-tick", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.scheduled_tick:
        parser.error("--scheduled-tick is required")
    result = run_discovery()
    print(json.dumps(result, ensure_ascii=False))
    return (
        0
        if result.get("status")
        not in {
            "RETRY_NEXT_INTERVAL",
            "PARTIAL_RETRY_NEXT_INTERVAL",
            "REGISTRY_RECOVERY_REQUIRED",
        }
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
