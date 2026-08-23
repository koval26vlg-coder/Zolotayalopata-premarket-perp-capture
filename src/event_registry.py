"""The registry of listing events, and where each t0 actually came from.

The hypothesis this project exists to test is about the seconds around t0. A t0 that
is wrong by a minute makes every capture built on it worthless, so the registry treats
the provenance of that timestamp as the primary datum rather than an annotation.

The spot monitor's audit is the reason. There, MEXC's firstOpenTime and Gate's
min(buy_start, sell_start) were pooled into one `listed_ts` column: one is when trading
actually began, the other is a schedule that can still move. Nothing recorded which was
which, and nothing stopped them being averaged together. Here a source class travels
with every event and events of different classes are never merged.

Two further properties matter for pre-market listings specifically:

* venues move launch times, sometimes more than once. A revision is appended with the
  previous value, never written over it - a t0 that silently changed after a capture
  would invalidate the capture without leaving a trace.
* nothing here is an official announcement yet. OFFICIAL_ANNOUNCEMENT exists as a
  class so that adding such a source later is a different class by construction rather
  than a quiet upgrade of what venue metadata means.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import time
import unicodedata
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import project_config as config
import public_http
import risk_gate
import frozen_plan_bindings as trust_root
from canonical_hash import canonical_hash


def _has_forbidden_unicode_controls(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)


REGISTRY_SCHEMA = "premarket_perp_event_registry_v2"
REGISTRY_V1_PATH = config.PROJECT_ROOT / "docs/registry/listing-events.jsonl"
REGISTRY_PATH = config.PROJECT_ROOT / "docs/registry/listing-events-v2.jsonl"
REGISTRY_SUMMARY_PATH = config.PROJECT_ROOT / "docs/registry/listing-events-v2.summary.json"
REGISTRY_LOCK_PATH = config.PROJECT_ROOT / "docs/registry/listing-events-v2.lock"
ACTIVE_CONTRACTS_FIELD = "active_contract_ids_by_venue"
ACTIVE_LIFECYCLE_GENERATIONS_FIELD = "active_lifecycle_generations_by_venue"
LIFECYCLE_GENERATION_HIGH_WATER_FIELD = "lifecycle_generation_high_water_by_venue"
LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD = (
    "last_complete_metadata_refresh_received_at_utc"
)
MAX_COMPLETE_METADATA_REFRESH_AGE_SEC = config.MAX_COMPLETE_METADATA_REFRESH_AGE_SEC
RAW_UNIVERSE_ROWS_FIELD = "raw_universe_rows_by_venue"
MIN_FULL_UNIVERSE_RETENTION_RATIO = config.MIN_FULL_UNIVERSE_RETENTION_RATIO
MUTATION_RECEIPT_SCHEMA = "premarket_perp_registry_mutation_receipt_v1"
MUTATION_RECEIPT_DIR_SUFFIX = ".mutation-receipts"
MUTATION_SUMMARY_LINK_FIELDS = frozenset(
    {
        "mutation_receipt_schema",
        "mutation_seq",
        "previous_mutation_receipt_hash",
        "mutation_receipt_hash",
    }
)
# How a t0 was learned. These are never mixed inside one analysis, and the ordering
# here is deliberate: metadata is what we have, an announcement is what we would prefer.
SOURCE_OFFICIAL_ANNOUNCEMENT = "OFFICIAL_ANNOUNCEMENT"
SOURCE_VENUE_INSTRUMENT_METADATA = "VENUE_INSTRUMENT_METADATA"
SOURCE_OBSERVED_PUBLIC_TRADE = "OBSERVED_PUBLIC_TRADE"
SOURCE_OBSERVED_LIFECYCLE = "OBSERVED_LIFECYCLE"
SOURCE_CLASSES = (
    SOURCE_OFFICIAL_ANNOUNCEMENT,
    SOURCE_VENUE_INSTRUMENT_METADATA,
    SOURCE_OBSERVED_PUBLIC_TRADE,
    SOURCE_OBSERVED_LIFECYCLE,
)

TIMESTAMP_PREMARKET_CONTRACT_LAUNCH = "premarket_contract_launch_ts"
TIMESTAMP_OFFICIAL_SPOT_T0 = "official_spot_t0"
TIMESTAMP_FIRST_TRADE = "first_trade_ts"
TIMESTAMP_TRANSITION = "transition_ts"
TIMESTAMP_CONTRACT_CREATED = "contract_created_ts"
TIMESTAMP_KINDS = (
    TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
    TIMESTAMP_OFFICIAL_SPOT_T0,
    TIMESTAMP_FIRST_TRADE,
    TIMESTAMP_TRANSITION,
    TIMESTAMP_CONTRACT_CREATED,
)
INSTRUMENT_ROLES = ("premarket_perp", "spot", "standard_perp")


class EventRegistryError(RuntimeError):
    pass


def _is_sha256_text(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _preflight_is_exact(
    receipt: Any,
    *,
    write_class: str,
    run_id: str,
    decision: str,
    action: str,
) -> bool:
    """Accept only the current gate schema and external trust-root identity."""
    return bool(
        isinstance(receipt, Mapping)
        and receipt.get("schema") == risk_gate.PREFLIGHT_RESULT_SCHEMA
        and receipt.get("ok") is True
        and receipt.get("verified") is True
        and receipt.get("decision") == decision
        and receipt.get("write_class") == write_class
        and receipt.get("run_id") == run_id
        and receipt.get("action") == action
        and receipt.get("plan_id") == trust_root.PLAN_ID
        and receipt.get("plan_hash") == trust_root.PLAN_HASH
        and _is_sha256_text(receipt.get("resolved_paths_hash"))
    )


@dataclass(frozen=True)
class RegistryLockOwner:
    path: Path
    owner_pid: int
    owner_host: str
    run_id: str
    nonce: str
    plan_hash: str
    acquired_at_utc: str


def acquire_registry_lock(
    path: Path,
    *,
    run_id: str,
    plan_hash: str,
) -> RegistryLockOwner:
    """Atomically claim the short metadata-registry critical section."""
    if not run_id.strip():
        raise EventRegistryError("registry lock run_id is required")
    if len(plan_hash) != 64:
        raise EventRegistryError("registry lock requires a SHA-256 plan hash")
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = RegistryLockOwner(
        path=path,
        owner_pid=os.getpid(),
        owner_host=socket.gethostname(),
        run_id=run_id,
        nonce=secrets.token_hex(32),
        plan_hash=plan_hash,
        acquired_at_utc=utc_now_iso(),
    )
    payload = {
        "schema": "premarket_perp_registry_lock_v1",
        "owner_pid": owner.owner_pid,
        "owner_host": owner.owner_host,
        "run_id": owner.run_id,
        "nonce": owner.nonce,
        "plan_hash": owner.plan_hash,
        "acquired_at_utc": owner.acquired_at_utc,
    }
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise EventRegistryError(f"REGISTRY_LOCKED: {path}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise
    return owner


def _assert_registry_lock_owner(owner: RegistryLockOwner) -> None:
    try:
        payload = json.loads(owner.path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EventRegistryError(
            f"registry lock owner mismatch or unreadable lock: {owner.path}"
        ) from exc
    expected = {
        "owner_pid": owner.owner_pid,
        "owner_host": owner.owner_host,
        "run_id": owner.run_id,
        "nonce": owner.nonce,
        "plan_hash": owner.plan_hash,
        "acquired_at_utc": owner.acquired_at_utc,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise EventRegistryError(f"registry lock owner mismatch: {owner.path}")


def release_registry_lock(owner: RegistryLockOwner) -> None:
    """Release only the exact claim we acquired; stale locks remain fail-closed."""
    _assert_registry_lock_owner(owner)
    owner.path.unlink()


@contextmanager
def registry_lock(
    path: Path,
    *,
    run_id: str,
    plan_hash: str,
):
    owner = acquire_registry_lock(path, run_id=run_id, plan_hash=plan_hash)
    try:
        yield owner
    finally:
        release_registry_lock(owner)


@dataclass(frozen=True)
class VenueAdapter:
    venue: str
    url: str
    params: dict[str, str]
    symbol_field: str
    t0_field: str
    t0_unit: str                 # "ms" or "s"
    t0_semantics: str
    # Which named timestamp this field actually is. Declared here, beside the field,
    # because it was previously inferred from the venue name in the normaliser - so
    # changing which field a venue reads left the kind quietly saying the old thing.
    t0_kind: str
    t0_precision_sec: int
    caveats: tuple[str, ...] = ()
    rows_path: tuple[str, ...] = ()
    # Bybit pages its instrument list and caps a page at 500. Following the cursor is
    # not an optimisation: without it the registry silently held 500 of 833 linear
    # instruments and looked complete while doing so.
    cursor_path: tuple[str, ...] = ()
    cursor_param: str = ""
    max_pages: int = 20

    def rows(self, payload: Any) -> list[Mapping[str, Any]]:
        current: Any = payload
        for key in self.rows_path:
            if not isinstance(current, Mapping):
                return []
            current = current.get(key)
        return [row for row in (current or []) if isinstance(row, Mapping)]

    def next_cursor(self, payload: Any) -> str:
        if not self.cursor_param:
            return ""
        current: Any = payload
        for key in self.cursor_path:
            if not isinstance(current, Mapping):
                return ""
            current = current.get(key)
        return str(current or "").strip()


ADAPTERS: tuple[VenueAdapter, ...] = (
    VenueAdapter(
        venue="bybit",
        url="https://api.bybit.com/v5/market/instruments-info",
        params={"category": "linear", "status": "PreLaunch", "limit": "1000"},
        symbol_field="symbol",
        t0_field="launchTime",
        t0_unit="ms",
        t0_semantics="venue-declared contract launch time",
        t0_kind=TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
        t0_precision_sec=1,
        rows_path=("result", "list"),
        cursor_path=("result", "nextPageCursor"),
        cursor_param="cursor",
    ),
    VenueAdapter(
        venue="okx",
        url="https://www.okx.com/api/v5/public/instruments",
        # SWAP, because this project observes perpetuals and OKX carries its
        # pre-market perpetuals there. Measured 2026-08-23: instType=SWAP holds three
        # rows with ruleType=pre_market (ANTHROPIC, MOONSHOT, OPENAI - the same
        # underlyings Bybit lists), while instType=FUTURES holds none at all. The
        # dated xperp contracts under FUTURES are a different instrument class and
        # carry an expTime.
        params={"instType": "SWAP"},
        symbol_field="instId",
        t0_field="listTime",
        t0_unit="ms",
        t0_semantics="venue-declared instrument listing time",
        t0_kind=TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
        t0_precision_sec=1,
        rows_path=("data",),
    ),
    VenueAdapter(
        venue="gate",
        url="https://api.gateio.ws/api/v4/futures/usdt/contracts",
        params={},
        symbol_field="name",
        # launch_time, not create_time. Measured 2026-08-23: the two differ on 419 of
        # 935 contracts, and on ANTHROPIC_USDT by more than two hours - created
        # 09:54:33, launched 12:00:00. create_time was carried with a
        # CONTRACT_CREATION_NOT_TRADING_START caveat since the registry was built,
        # while the venue was publishing the launch instant in a field beside it.
        t0_field="launch_time",
        t0_unit="s",
        t0_semantics="venue-declared contract launch time",
        t0_kind=TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
        t0_precision_sec=1,
        caveats=(),
        rows_path=(),
    ),
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _writer_refresh_completed_at_utc() -> str:
    """Writer-owned timestamp taken only after all live venue fetches finish."""
    return utc_now_iso()


def _to_seconds(raw: Any, unit: str) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return int(value / 1000) if unit == "ms" else int(value)


def _is_premarket_row(adapter: VenueAdapter, row: Mapping[str, Any]) -> bool:
    """Return rows relevant to lifecycle history, including terminal transition rows."""
    if adapter.venue == "bybit":
        return (
            str(row.get("status") or "") == "PreLaunch"
            and row.get("isPreListing") is True
            and str(row.get("contractType") or "") == "LinearPerpetual"
        )
    if adapter.venue == "okx":
        rule_type = str(row.get("ruleType") or "")
        instrument_type = str(row.get("instType") or "")
        # A pre-market perpetual. This is the row this project is about.
        if instrument_type == "SWAP" and rule_type == "pre_market":
            return True
        # A dated pre-market future that OKX will switch to a perpetual. Kept because
        # the flow is real, but it matched nothing on 2026-08-23: none of the 142
        # xperp rows carried preMktSwTime, so requiring it here means this branch is
        # dormant rather than silently wrong.
        return (
            instrument_type == "FUTURES"
            and rule_type == "xperp"
            and _to_seconds(row.get("preMktSwTime"), "ms") is not None
        )
    if adapter.venue == "gate":
        # is_pre_market, not status. Measured 2026-08-23: all 935 contracts report
        # status "trading" and none ever reports "prelaunch", so the old predicate
        # could not match a single row - while nine contracts carried
        # is_pre_market true, among them the same underlyings Bybit and OKX list.
        return row.get("is_pre_market") is True
    return False


def _is_currently_active_premarket_row(
    adapter: VenueAdapter, row: Mapping[str, Any]
) -> bool:
    """Return only contracts currently advertised as an active pre-market market."""
    if adapter.venue == "okx":
        return (
            str(row.get("instType") or "") == "SWAP"
            and str(row.get("ruleType") or "") == "pre_market"
            and str(row.get("state") or "").lower() == "live"
        )
    return _is_premarket_row(adapter, row)


def event_id(venue: str, symbol: str) -> str:
    return f"{venue}:{symbol}"


def make_episode_id(
    venue: str, native_premarket_contract_id: str, lifecycle_generation: int = 0
) -> str:
    """Return identity that survives schedule and symbol-mapping revisions."""
    if venue not in {adapter.venue for adapter in ADAPTERS}:
        raise EventRegistryError(f"unknown venue: {venue}")
    native_id = native_premarket_contract_id.strip()
    if not native_id:
        raise EventRegistryError("native pre-market contract id is required")
    if lifecycle_generation < 0:
        raise EventRegistryError("lifecycle_generation must be non-negative")
    digest = canonical_hash(
        {
            "venue": venue,
            "native_premarket_contract_id": native_id,
            "lifecycle_generation": lifecycle_generation,
        }
    )
    return f"ep_{digest}"


def _stream_id(
    *,
    episode_id: str,
    timestamp_kind: str,
    instrument_role: str,
    source_class: str,
    source_identity: str,
) -> str:
    return canonical_hash(
        {
            "episode_id": episode_id,
            "timestamp_kind": timestamp_kind,
            "instrument_role": instrument_role,
            "source_class": source_class,
            "source_identity": source_identity,
        }
    )


def make_timestamp_observation(
    *,
    episode_id: str,
    venue: str,
    premarket_contract_id: str,
    spot_symbol: str | None,
    timestamp_kind: str,
    timestamp_ts: int,
    instrument_role: str,
    source_class: str,
    source_identity: str,
    source_url: str,
    received_at_utc: str,
    precision_sec: int = 1,
    caveats: Sequence[str] = (),
    lifecycle_generation: int = 0,
) -> dict[str, Any]:
    """Build one validated timestamp observation for an immutable listing episode."""
    if not episode_id.strip():
        raise EventRegistryError("episode_id is required")
    if venue not in {adapter.venue for adapter in ADAPTERS}:
        raise EventRegistryError(f"unknown venue: {venue}")
    if not premarket_contract_id.strip():
        raise EventRegistryError("premarket_contract_id is required")
    if timestamp_kind not in TIMESTAMP_KINDS:
        raise EventRegistryError(f"unknown timestamp kind: {timestamp_kind}")
    if int(timestamp_ts) <= 0:
        raise EventRegistryError("timestamp_ts must be positive")
    if instrument_role not in INSTRUMENT_ROLES:
        raise EventRegistryError(f"unknown instrument role: {instrument_role}")
    if source_class not in SOURCE_CLASSES:
        raise EventRegistryError(f"unknown source class: {source_class}")
    if not source_identity.strip():
        raise EventRegistryError("source_identity is required")
    if not source_url.strip():
        raise EventRegistryError("source_url is required")
    if not received_at_utc.strip():
        raise EventRegistryError("received_at_utc is required")
    if int(lifecycle_generation) < 0:
        raise EventRegistryError("lifecycle_generation must be non-negative")
    if timestamp_kind == TIMESTAMP_OFFICIAL_SPOT_T0:
        if source_class != SOURCE_OFFICIAL_ANNOUNCEMENT:
            raise EventRegistryError("official_spot_t0 requires OFFICIAL_ANNOUNCEMENT")
        if instrument_role != "spot" or not str(spot_symbol or "").strip():
            raise EventRegistryError("official_spot_t0 requires a spot instrument and symbol")
    if timestamp_kind == TIMESTAMP_FIRST_TRADE and source_class != SOURCE_OBSERVED_PUBLIC_TRADE:
        raise EventRegistryError("first_trade_ts requires OBSERVED_PUBLIC_TRADE")

    acceptance_anchor = timestamp_kind == TIMESTAMP_OFFICIAL_SPOT_T0
    observation = {
        "schema": REGISTRY_SCHEMA,
        "record_type": "timestamp_observation",
        "event_id": episode_id,
        "episode_id": episode_id,
        "venue": venue,
        "symbol": premarket_contract_id,
        "premarket_contract_id": premarket_contract_id,
        "spot_symbol": spot_symbol,
        "lifecycle_generation": int(lifecycle_generation),
        "premarket_contract_launch_ts": None,
        "official_spot_t0": None,
        "first_trade_ts": None,
        "transition_ts": None,
        "contract_created_ts": None,
        "timestamp_kind": timestamp_kind,
        "timestamp_ts": int(timestamp_ts),
        "instrument_role": instrument_role,
        "t0_source_class": source_class,
        "source_class": source_class,
        "source_identity": source_identity,
        "source_url": source_url,
        "received_at_utc": received_at_utc,
        "observed_at_utc": received_at_utc,
        "t0_precision_sec": int(precision_sec),
        "caveats": list(caveats),
        "evidence_use": "ACCEPTANCE_ANCHOR" if acceptance_anchor else "DESCRIPTIVE_ONLY",
        "capture_eligible": acceptance_anchor,
    }
    observation[timestamp_kind] = int(timestamp_ts)
    observation["stream_id"] = _stream_id(
        episode_id=episode_id,
        timestamp_kind=timestamp_kind,
        instrument_role=instrument_role,
        source_class=source_class,
        source_identity=source_identity,
    )
    return observation


@dataclass(frozen=True)
class VenueFetch:
    rows: list[Mapping[str, Any]]
    pages: int
    truncated: bool


def fetch_venue(adapter: VenueAdapter, fetch: Any) -> VenueFetch:
    """Follow the venue's cursor to the end - and say so when we stop early.

    A page cap has to exist or a misbehaving cursor loops forever, but stopping at it
    silently is how a partial universe passes for a whole one."""
    rows: list[Mapping[str, Any]] = []
    cursor = ""
    seen_cursors: set[str] = set()
    pages = 0
    while True:
        params = dict(adapter.params)
        if cursor:
            params[adapter.cursor_param] = cursor
        payload = fetch(adapter, params)
        rows.extend(adapter.rows(payload))
        pages += 1
        cursor = adapter.next_cursor(payload)
        if not cursor:
            return VenueFetch(rows=rows, pages=pages, truncated=False)
        if cursor in seen_cursors:
            raise EventRegistryError(
                f"{adapter.venue} repeated pagination cursor: {cursor}"
            )
        seen_cursors.add(cursor)
        if pages >= adapter.max_pages:
            return VenueFetch(rows=rows, pages=pages, truncated=True)


def _validate_payload_shape(adapter: VenueAdapter, payload: Any) -> None:
    if adapter.venue == "bybit":
        if not isinstance(payload, Mapping) or payload.get("retCode") not in (0, "0"):
            raise EventRegistryError("bybit payload has no successful retCode")
        result = payload.get("result")
        if not isinstance(result, Mapping) or not isinstance(result.get("list"), list):
            raise EventRegistryError("bybit payload has no result.list array")
        category = str(result.get("category") or "")
        if category and category != "linear":
            raise EventRegistryError("bybit payload category is not linear")
        return
    if adapter.venue == "okx":
        if not isinstance(payload, Mapping) or str(payload.get("code")) != "0":
            raise EventRegistryError("okx payload has no successful code")
        if not isinstance(payload.get("data"), list):
            raise EventRegistryError("okx payload has no data array")
        return
    if adapter.venue == "gate":
        if not isinstance(payload, list):
            raise EventRegistryError("gate payload is not an array")
        return
    raise EventRegistryError(f"unsupported venue adapter: {adapter.venue}")


def normalise_rows(
    adapter: VenueAdapter,
    rows: Sequence[Mapping[str, Any]],
    *,
    observed_at_utc: str,
    lifecycle_generations: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    lifecycle_generations = lifecycle_generations or {}
    for row in rows:
        if not _is_premarket_row(adapter, row):
            continue
        symbol = str(row.get(adapter.symbol_field) or "").strip()
        if not symbol:
            continue
        t0_ts = _to_seconds(row.get(adapter.t0_field), adapter.t0_unit)
        if t0_ts is None:
            # A missing launch time is not a zero: the event is simply not usable yet.
            continue
        lifecycle_generation = int(lifecycle_generations.get(symbol, 0))
        timestamp_kind = adapter.t0_kind
        observation = make_timestamp_observation(
            episode_id=make_episode_id(
                adapter.venue, symbol, lifecycle_generation
            ),
            venue=adapter.venue,
            premarket_contract_id=symbol,
            spot_symbol=None,
            timestamp_kind=timestamp_kind,
            timestamp_ts=t0_ts,
            instrument_role="premarket_perp",
            source_class=SOURCE_VENUE_INSTRUMENT_METADATA,
            source_identity=(
                f"{adapter.venue}:instrument_metadata:{adapter.t0_field}"
            ),
            source_url=adapter.url,
            received_at_utc=observed_at_utc,
            precision_sec=adapter.t0_precision_sec,
            caveats=adapter.caveats,
            lifecycle_generation=lifecycle_generation,
        )
        # Kept only as a descriptive v1 read-compatibility projection.  Runtime
        # selection never reads this generic field.
        observation.update(
            {
                "legacy_event_id": event_id(adapter.venue, symbol),
                "t0_ts": t0_ts,
                "t0_source_field": adapter.t0_field,
                "t0_semantics": adapter.t0_semantics,
            }
        )
        events.append(observation)
        if adapter.venue == "okx":
            transition_ts = _to_seconds(row.get("preMktSwTime"), "ms")
            if transition_ts is not None:
                transition = make_timestamp_observation(
                    episode_id=observation["episode_id"],
                    venue=adapter.venue,
                    premarket_contract_id=symbol,
                    spot_symbol=None,
                    timestamp_kind=TIMESTAMP_TRANSITION,
                    timestamp_ts=transition_ts,
                    instrument_role="standard_perp",
                    source_class=SOURCE_VENUE_INSTRUMENT_METADATA,
                    source_identity="okx:instrument_metadata:preMktSwTime",
                    source_url=adapter.url,
                    received_at_utc=observed_at_utc,
                    precision_sec=1,
                    caveats=("VENUE_METADATA_TRANSITION_TIMESTAMP",),
                    lifecycle_generation=lifecycle_generation,
                )
                transition.update(
                    {
                        "legacy_event_id": event_id(adapter.venue, symbol),
                        "t0_source_field": "preMktSwTime",
                        "t0_semantics": (
                            "venue-declared transition from pre-market to xperp"
                        ),
                    }
                )
                events.append(transition)
    events.sort(key=lambda item: (item["timestamp_ts"], item["venue"], item["symbol"]))
    return events


def normalise(adapter: VenueAdapter, payload: Any, *, observed_at_utc: str) -> list[dict[str, Any]]:
    """Single-page convenience wrapper."""
    return normalise_rows(adapter, adapter.rows(payload), observed_at_utc=observed_at_utc)


def _active_contract_ids(observed: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    active: dict[str, set[str]] = {adapter.venue: set() for adapter in ADAPTERS}
    for entry in observed:
        venue = str(entry.get("venue") or "")
        contract = str(entry.get("premarket_contract_id") or "")
        if venue in active and contract:
            active[venue].add(contract)
    return {venue: sorted(contracts) for venue, contracts in sorted(active.items())}


def _active_contract_ids_from_rows(
    staged_rows: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[str]]:
    """Return every currently advertised pre-market id, even without a usable t0."""
    active: dict[str, set[str]] = {adapter.venue: set() for adapter in ADAPTERS}
    adapters = {adapter.venue: adapter for adapter in ADAPTERS}
    for venue, rows in staged_rows.items():
        adapter = adapters.get(venue)
        if adapter is None:
            continue
        for row in rows:
            if not _is_currently_active_premarket_row(adapter, row):
                continue
            contract = str(row.get(adapter.symbol_field) or "").strip()
            if contract:
                active[venue].add(contract)
    return {venue: sorted(contracts) for venue, contracts in sorted(active.items())}


def _lifecycle_contract_ids_from_rows(
    staged_rows: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[str]]:
    relevant: dict[str, set[str]] = {adapter.venue: set() for adapter in ADAPTERS}
    adapters = {adapter.venue: adapter for adapter in ADAPTERS}
    for venue, rows in staged_rows.items():
        adapter = adapters.get(venue)
        if adapter is None:
            continue
        for row in rows:
            if not _is_premarket_row(adapter, row):
                continue
            contract = str(row.get(adapter.symbol_field) or "").strip()
            if contract:
                relevant[venue].add(contract)
    return {
        venue: sorted(contracts)
        for venue, contracts in sorted(relevant.items())
    }


def _derived_lifecycle_high_water(
    existing: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {
        adapter.venue: {} for adapter in ADAPTERS
    }
    for entry in existing:
        venue = str(entry.get("venue") or "")
        contract = str(
            entry.get("premarket_contract_id") or entry.get("symbol") or ""
        ).strip()
        if venue not in result or not contract:
            continue
        generation = int(entry.get("lifecycle_generation", 0) or 0)
        result[venue][contract] = max(
            result[venue].get(contract, -1), generation
        )
    return {
        venue: dict(sorted(values.items()))
        for venue, values in sorted(result.items())
    }


def _parse_lifecycle_generation_state(
    raw: Any,
    *,
    field_name: str,
) -> dict[str, dict[str, int]]:
    if not isinstance(raw, Mapping):
        raise EventRegistryError(
            f"LIFECYCLE_GENERATION_STATE_MISSING: {field_name} is not an object"
        )
    expected_venues = {adapter.venue for adapter in ADAPTERS}
    if set(raw) != expected_venues:
        raise EventRegistryError(
            f"LIFECYCLE_GENERATION_STATE_MISSING: {field_name} venue set is invalid"
        )
    result: dict[str, dict[str, int]] = {}
    for venue in sorted(expected_venues):
        contracts = raw.get(venue)
        if not isinstance(contracts, Mapping):
            raise EventRegistryError(
                f"LIFECYCLE_GENERATION_STATE_MISSING: {field_name}.{venue} is invalid"
            )
        parsed: dict[str, int] = {}
        for contract, generation in contracts.items():
            if (
                not isinstance(contract, str)
                or not contract
                or contract != contract.strip()
                or isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation < 0
            ):
                raise EventRegistryError(
                    f"LIFECYCLE_GENERATION_STATE_MISSING: "
                    f"{field_name}.{venue} entry is invalid"
                )
            parsed[contract] = generation
        result[venue] = dict(sorted(parsed.items()))
    return result


def _parse_active_contract_ids(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, Mapping):
        raise EventRegistryError(
            "LIFECYCLE_GENERATION_STATE_MISSING: invalid active contract state"
        )
    expected_venues = {adapter.venue for adapter in ADAPTERS}
    if set(raw) != expected_venues:
        raise EventRegistryError(
            "LIFECYCLE_GENERATION_STATE_MISSING: active contract venue set is invalid"
        )
    result: dict[str, list[str]] = {}
    for venue in sorted(expected_venues):
        values = raw.get(venue)
        if not isinstance(values, list) or not all(
            isinstance(value, str)
            and bool(value)
            and value == value.strip()
            for value in values
        ):
            raise EventRegistryError(
                "LIFECYCLE_GENERATION_STATE_MISSING: invalid active contract state"
            )
        if len(values) != len(set(values)):
            raise EventRegistryError(
                "LIFECYCLE_GENERATION_STATE_MISSING: duplicate active contract id"
            )
        result[venue] = sorted(values)
    return result


def _parse_raw_universe_counts(raw: Any) -> dict[str, int]:
    if not isinstance(raw, Mapping) or set(raw) != {
        adapter.venue for adapter in ADAPTERS
    }:
        raise EventRegistryError("raw universe row counts are invalid")
    result: dict[str, int] = {}
    for adapter in ADAPTERS:
        value = raw.get(adapter.venue)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EventRegistryError("raw universe row counts are invalid")
        result[adapter.venue] = value
    return dict(sorted(result.items()))


def _summary_raw_universe_counts(summary_path: Path) -> dict[str, int] | None:
    if not summary_path.is_file():
        return None
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EventRegistryError(f"raw universe summary is unreadable: {exc}") from exc
    if not isinstance(summary, Mapping):
        raise EventRegistryError("raw universe summary is invalid")
    if RAW_UNIVERSE_ROWS_FIELD not in summary:
        return None
    return _parse_raw_universe_counts(summary[RAW_UNIVERSE_ROWS_FIELD])


def _load_lifecycle_generation_state(
    summary_path: Path,
    *,
    existing: Sequence[Mapping[str, Any]],
    current_active_for_legacy: Mapping[str, Sequence[str]] | None = None,
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Return exact active generations and monotonic per-contract high-water state."""
    derived_high_water = _derived_lifecycle_high_water(existing)
    summary: Mapping[str, Any] | None = None
    if summary_path.is_file():
        try:
            loaded = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EventRegistryError(
                f"LIFECYCLE_GENERATION_STATE_MISSING: summary unreadable: {exc}"
            ) from exc
        if not isinstance(loaded, Mapping):
            raise EventRegistryError(
                "LIFECYCLE_GENERATION_STATE_MISSING: summary root is not an object"
            )
        summary = loaded

    if summary is not None and (
        ACTIVE_LIFECYCLE_GENERATIONS_FIELD in summary
        or LIFECYCLE_GENERATION_HIGH_WATER_FIELD in summary
    ):
        if (
            ACTIVE_LIFECYCLE_GENERATIONS_FIELD not in summary
            or LIFECYCLE_GENERATION_HIGH_WATER_FIELD not in summary
        ):
            raise EventRegistryError(
                "LIFECYCLE_GENERATION_STATE_MISSING: active and high-water state "
                "must be persisted together"
            )
        active = _parse_lifecycle_generation_state(
            summary[ACTIVE_LIFECYCLE_GENERATIONS_FIELD],
            field_name=ACTIVE_LIFECYCLE_GENERATIONS_FIELD,
        )
        high_water = _parse_lifecycle_generation_state(
            summary[LIFECYCLE_GENERATION_HIGH_WATER_FIELD],
            field_name=LIFECYCLE_GENERATION_HIGH_WATER_FIELD,
        )
        active_ids = _parse_active_contract_ids(summary.get(ACTIVE_CONTRACTS_FIELD))
        for venue in active:
            if sorted(active[venue]) != active_ids[venue]:
                raise EventRegistryError(
                    "LIFECYCLE_GENERATION_STATE_MISSING: active ids and generations differ"
                )
            for contract, generation in active[venue].items():
                if high_water[venue].get(contract, -1) != generation:
                    raise EventRegistryError(
                        "LIFECYCLE_GENERATION_STATE_MISSING: active generation must "
                        "equal its high-water generation"
                    )
            for contract, generation in derived_high_water[venue].items():
                if high_water[venue].get(contract, -1) < generation:
                    raise EventRegistryError(
                        "LIFECYCLE_GENERATION_STATE_MISSING: high-water state regressed"
                    )
        return active, high_water

    # One-way compatibility bootstrap for fixtures and pre-v9 receipts.  Every new
    # mutation writes both structured fields, after which they are authoritative.
    if summary is not None and summary.get(ACTIVE_CONTRACTS_FIELD) is not None:
        active_ids = _parse_active_contract_ids(summary[ACTIVE_CONTRACTS_FIELD])
    elif current_active_for_legacy is not None:
        active_ids = {
            adapter.venue: sorted(
                set(current_active_for_legacy.get(adapter.venue, ()))
            )
            for adapter in ADAPTERS
        }
    else:
        active_ids = _active_contract_ids(existing)
    active: dict[str, dict[str, int]] = {
        adapter.venue: {} for adapter in ADAPTERS
    }
    high_water = {
        venue: dict(values) for venue, values in derived_high_water.items()
    }
    for venue, contracts in active_ids.items():
        for contract in contracts:
            generation = high_water[venue].get(contract, 0)
            active[venue][contract] = generation
            high_water[venue][contract] = max(
                high_water[venue].get(contract, -1), generation
            )
    return (
        {venue: dict(sorted(values.items())) for venue, values in sorted(active.items())},
        {
            venue: dict(sorted(values.items()))
            for venue, values in sorted(high_water.items())
        },
    )


def _allocate_current_lifecycle_state(
    current_active: Mapping[str, Sequence[str]],
    *,
    previous_active: Mapping[str, Mapping[str, int]],
    previous_high_water: Mapping[str, Mapping[str, int]],
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    active: dict[str, dict[str, int]] = {}
    high_water: dict[str, dict[str, int]] = {
        adapter.venue: dict(previous_high_water.get(adapter.venue, {}))
        for adapter in ADAPTERS
    }
    for adapter in ADAPTERS:
        venue = adapter.venue
        active[venue] = {}
        for contract in sorted(set(current_active.get(venue, ()))):
            if contract in previous_active.get(venue, {}):
                generation = int(previous_active[venue][contract])
            else:
                generation = int(high_water[venue].get(contract, -1)) + 1
            active[venue][contract] = generation
            high_water[venue][contract] = max(
                int(high_water[venue].get(contract, -1)), generation
            )
    return (
        {venue: dict(sorted(values.items())) for venue, values in sorted(active.items())},
        {
            venue: dict(sorted(values.items()))
            for venue, values in sorted(high_water.items())
        },
    )


def _bind_relevant_lifecycle_generations(
    lifecycle_contracts: Mapping[str, Sequence[str]],
    *,
    active_generations: Mapping[str, Mapping[str, int]],
    previous_active: Mapping[str, Mapping[str, int]],
    lifecycle_high_water: Mapping[str, Mapping[str, int]],
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Bind terminal rows to their just-ended episode without keeping it active."""
    bound = {
        adapter.venue: dict(active_generations.get(adapter.venue, {}))
        for adapter in ADAPTERS
    }
    high_water = {
        adapter.venue: dict(lifecycle_high_water.get(adapter.venue, {}))
        for adapter in ADAPTERS
    }
    for adapter in ADAPTERS:
        venue = adapter.venue
        for contract in sorted(set(lifecycle_contracts.get(venue, ()))):
            if contract in bound[venue]:
                generation = bound[venue][contract]
            elif contract in previous_active.get(venue, {}):
                generation = int(previous_active[venue][contract])
            else:
                generation = int(high_water[venue].get(contract, 0))
            bound[venue][contract] = generation
            high_water[venue][contract] = max(
                int(high_water[venue].get(contract, -1)), generation
            )
    return (
        {venue: dict(sorted(values.items())) for venue, values in sorted(bound.items())},
        {
            venue: dict(sorted(values.items()))
            for venue, values in sorted(high_water.items())
        },
    )


def _summary_active_contracts(
    summary_path: Path,
    *,
    existing: Sequence[Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Read active lifecycle ids for a non-metadata mutation without changing them."""
    active, _high_water = _load_lifecycle_generation_state(
        summary_path, existing=existing
    )
    return {
        venue: sorted(generations)
        for venue, generations in sorted(active.items())
    }


def _summary_active_lifecycle_generations(
    summary_path: Path,
    *,
    existing: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    active, _high_water = _load_lifecycle_generation_state(
        summary_path, existing=existing
    )
    return active


def _summary_lifecycle_generation_high_water(
    summary_path: Path,
    *,
    existing: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    _active, high_water = _load_lifecycle_generation_state(
        summary_path, existing=existing
    )
    return high_water


def _summary_complete_metadata_refresh_received_at(summary_path: Path) -> str:
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EventRegistryError(
            f"complete metadata refresh anchor is unreadable: {exc}"
        ) from exc
    value = (
        summary.get(LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD)
        if isinstance(summary, Mapping)
        else None
    )
    if _parse_explicit_utc(value) is None:
        raise EventRegistryError("complete metadata refresh anchor is invalid")
    return str(value)


def _bind_lifecycle_generations(
    observed: Sequence[Mapping[str, Any]],
    *,
    active_generations: Mapping[str, Mapping[str, int]],
) -> list[dict[str, Any]]:
    bound: list[dict[str, Any]] = []
    for raw in observed:
        entry = dict(raw)
        venue = str(entry.get("venue") or "")
        contract = str(entry.get("premarket_contract_id") or entry.get("symbol") or "")
        try:
            generation = int(active_generations[venue][contract])
        except (KeyError, TypeError, ValueError) as exc:
            raise EventRegistryError(
                f"observed contract has no active lifecycle generation: {venue}:{contract}"
            ) from exc
        entry["lifecycle_generation"] = generation
        entry["episode_id"] = make_episode_id(venue, contract, generation)
        entry["event_id"] = entry["episode_id"]
        entry["stream_id"] = _stream_id(
            episode_id=entry["episode_id"],
            timestamp_kind=str(entry.get("timestamp_kind") or ""),
            instrument_role=str(entry.get("instrument_role") or ""),
            source_class=str(entry.get("source_class") or ""),
            source_identity=str(entry.get("source_identity") or ""),
        )
        bound.append(entry)
    return bound


def load_registry(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or REGISTRY_PATH
    if not path.is_file():
        return []
    return _load_registry_bytes(path.read_bytes(), path=path)


def _load_registry_bytes(raw: bytes, *, path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EventRegistryError(f"{path}: registry is not UTF-8: {exc}") from exc
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError as exc:
            raise EventRegistryError(f"{path}:{number}: unreadable registry line: {exc}") from exc
        entries.append(entry)
    return entries


def latest_by_event(entries: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """The current view: the most recent revision of each event."""
    current: dict[str, dict[str, Any]] = {}
    for entry in entries:
        current[str(entry.get("event_id"))] = dict(entry)
    return current


def _stream_heads(
    entries: Iterable[Mapping[str, Any]],
) -> dict[str, tuple[int, int, dict[str, Any]]]:
    heads: dict[str, tuple[int, int, dict[str, Any]]] = {}
    for position, raw in enumerate(entries):
        entry = dict(raw)
        stream_id = str(entry.get("stream_id") or "").strip()
        if not stream_id:
            continue
        revision = int(entry.get("stream_revision", entry.get("revision", 0)) or 0)
        previous = heads.get(stream_id)
        if previous is None or (revision, position) > (previous[0], previous[1]):
            heads[stream_id] = (revision, position, entry)
    return heads


def materialize_episodes(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Materialize lifecycle episodes without collapsing independent source streams.

    Each timestamp/source pair has its own stream head.  The episode view combines
    those heads only after preserving their provenance.  Conflicting official spot
    timestamps deliberately make the episode ineligible instead of selecting the
    last value observed.
    """
    episodes: dict[str, dict[str, Any]] = {}
    for _revision, _position, head in _stream_heads(entries).values():
        timestamp_kind = str(head.get("timestamp_kind") or "").strip()
        if timestamp_kind not in TIMESTAMP_KINDS:
            continue
        episode_id = str(head.get("episode_id") or head.get("event_id"))
        episode = episodes.setdefault(
            episode_id,
            {
                "event_id": episode_id,
                "episode_id": episode_id,
                "venue": head.get("venue"),
                "symbol": head.get("symbol") or head.get("premarket_contract_id"),
                "premarket_contract_id": head.get("premarket_contract_id")
                or head.get("symbol"),
                "spot_symbol": head.get("spot_symbol"),
                "lifecycle_generation": int(head.get("lifecycle_generation", 0) or 0),
                "premarket_contract_launch_ts": None,
                "official_spot_t0": None,
                "first_trade_ts": None,
                "transition_ts": None,
                "contract_created_ts": None,
                "timestamp_observations": [],
                "official_conflict": False,
                "evidence_use": "DESCRIPTIVE_ONLY",
                "capture_eligible": False,
            },
        )
        if episode.get("venue") != head.get("venue"):
            raise EventRegistryError(f"episode {episode_id} spans multiple venues")
        if not episode.get("spot_symbol") and head.get("spot_symbol"):
            episode["spot_symbol"] = head.get("spot_symbol")

        kind = str(head["timestamp_kind"])
        value = int(head.get("timestamp_ts") or head.get(kind) or 0)
        if value <= 0:
            continue
        episode["timestamp_observations"].append(
            {
                "stream_id": head["stream_id"],
                "stream_revision": int(
                    head.get("stream_revision", head.get("revision", 0)) or 0
                ),
                "timestamp_kind": kind,
                "timestamp_ts": value,
                "instrument_role": head.get("instrument_role"),
                "source_class": head.get("source_class")
                or head.get("t0_source_class"),
                "source_identity": head.get("source_identity"),
                "source_url": head.get("source_url"),
                "received_at_utc": head.get("received_at_utc"),
                "t0_precision_sec": int(head.get("t0_precision_sec", 0) or 0),
                "caveats": list(head.get("caveats") or []),
                "attestation": dict(head.get("attestation") or {}),
                "record_hash": head.get("record_hash") or head.get("entry_hash"),
            }
        )

    for episode in episodes.values():
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for observation in episode["timestamp_observations"]:
            by_kind.setdefault(observation["timestamp_kind"], []).append(observation)
        for kind, observations in by_kind.items():
            values = {int(item["timestamp_ts"]) for item in observations}
            if len(values) == 1:
                episode[kind] = next(iter(values))
            elif kind == TIMESTAMP_OFFICIAL_SPOT_T0:
                episode["official_conflict"] = True

        official_observations = [
            item
            for item in by_kind.get(TIMESTAMP_OFFICIAL_SPOT_T0, [])
            if item.get("source_class") == SOURCE_OFFICIAL_ANNOUNCEMENT
        ]
        official_values = {item["timestamp_ts"] for item in official_observations}
        if len(official_values) > 1:
            episode["official_conflict"] = True
            episode[TIMESTAMP_OFFICIAL_SPOT_T0] = None
        elif len(official_values) == 1 and not episode["official_conflict"]:
            episode[TIMESTAMP_OFFICIAL_SPOT_T0] = next(iter(official_values))
            episode["t0_source_class"] = SOURCE_OFFICIAL_ANNOUNCEMENT
            episode["evidence_use"] = "ACCEPTANCE_ANCHOR"
            episode["capture_eligible"] = True
            official = official_observations[0]
            episode["t0_precision_sec"] = official["t0_precision_sec"]
            episode["caveats"] = list(official["caveats"])
            episode["official_t0_provenance"] = {
                "stream_id": official["stream_id"],
                "stream_revision": official["stream_revision"],
                "record_hash": official["record_hash"],
                "source_class": official["source_class"],
                "source_identity": official["source_identity"],
                "source_url": official["source_url"],
                "received_at_utc": official["received_at_utc"],
                "t0_precision_sec": official["t0_precision_sec"],
                "caveats": list(official["caveats"]),
                "attestation": dict(official["attestation"]),
            }

        episode["timestamp_observations"].sort(
            key=lambda item: (
                item["timestamp_kind"],
                str(item.get("source_class")),
                str(item.get("source_identity")),
            )
        )

    return sorted(
        episodes.values(),
        key=lambda item: (str(item.get("venue")), str(item.get("episode_id"))),
    )


def _record_hash(record: Mapping[str, Any]) -> str:
    return canonical_hash({k: v for k, v in record.items() if k != "record_hash"})


def _entry_hash(entry: Mapping[str, Any]) -> str:
    """Compatibility alias for tooling that still labels a registry row an entry."""
    return _record_hash(entry)


def _observation_fingerprint(observation: Mapping[str, Any]) -> str:
    """Hash semantic stream state while ignoring when the same state was re-read."""
    ignored = {
        "record_hash",
        "record_seq",
        "previous_record_hash",
        "stream_revision",
        "supersedes_record_hash",
        "revision",
        "entry_hash",
        "observed_at_utc",
        "received_at_utc",
    }
    semantic = {k: v for k, v in observation.items() if k not in ignored}
    attestation = semantic.get("attestation")
    if isinstance(attestation, Mapping):
        semantic["attestation"] = {
            key: value
            for key, value in attestation.items()
            if key != "lead_sec_at_attestation"
        }
    return canonical_hash(semantic)


def build_stream_revisions(
    existing: Sequence[Mapping[str, Any]],
    observed: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Bind changed observations to global and per-source append-only hash chains."""
    if existing:
        problems = _verify_records(existing)
        if problems:
            raise EventRegistryError(
                "existing registry lineage is invalid: " + "; ".join(problems)
            )

    stream_heads: dict[str, dict[str, Any]] = {}
    for raw in existing:
        stream_heads[str(raw["stream_id"])] = dict(raw)

    global_head = dict(existing[-1]) if existing else None
    appended: list[dict[str, Any]] = []
    for raw in observed:
        observation = dict(raw)
        stream_id = str(observation.get("stream_id") or "").strip()
        if not stream_id:
            raise EventRegistryError("timestamp observation has no stream_id")
        previous_stream = stream_heads.get(stream_id)
        if previous_stream is not None and _observation_fingerprint(
            previous_stream
        ) == _observation_fingerprint(observation):
            continue

        for field in (
            "record_hash",
            "record_seq",
            "previous_record_hash",
            "stream_revision",
            "supersedes_record_hash",
            "revision",
            "entry_hash",
        ):
            observation.pop(field, None)
        observation["schema"] = REGISTRY_SCHEMA
        observation["record_type"] = "timestamp_observation"
        observation["record_seq"] = (
            int(global_head["record_seq"]) + 1 if global_head is not None else 0
        )
        observation["previous_record_hash"] = (
            global_head.get("record_hash") if global_head is not None else None
        )
        observation["stream_revision"] = (
            int(previous_stream["stream_revision"]) + 1
            if previous_stream is not None
            else 0
        )
        observation["supersedes_record_hash"] = (
            previous_stream.get("record_hash") if previous_stream is not None else None
        )
        observation["record_hash"] = _record_hash(observation)
        appended.append(observation)
        stream_heads[stream_id] = observation
        global_head = observation
    return appended


def merge_observations(
    existing: Sequence[Mapping[str, Any]],
    observed: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Compatibility name for the v2 stream-revision builder."""
    return build_stream_revisions(existing, observed)


def append_entries(
    entries: Sequence[Mapping[str, Any]],
    path: Path | None = None,
    *,
    lock_owner: RegistryLockOwner,
) -> int:
    """Append already lineage-bound records and fsync them to stable storage."""
    path = path or REGISTRY_PATH
    expected_lock_path = (
        REGISTRY_LOCK_PATH
        if path.resolve(strict=False) == REGISTRY_PATH.resolve(strict=False)
        else path.with_suffix(".lock")
    )
    if lock_owner.path.resolve(strict=False) != expected_lock_path.resolve(strict=False):
        raise EventRegistryError("registry lock does not cover the target registry")
    _assert_registry_lock_owner(lock_owner)
    if not entries:
        return 0
    for entry in entries:
        if entry.get("record_hash") != _record_hash(entry):
            raise EventRegistryError("refusing to append a record with an invalid hash")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(json.dumps(dict(entry), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return len(entries)


def _parse_explicit_utc(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() != timezone.utc.utcoffset(None):
        return None
    return moment.astimezone(timezone.utc)


def _normalise_symbol(value: Any) -> str:
    return "".join(character for character in str(value or "").upper() if character.isalnum())


def _normalise_contract_market(venue: str, value: Any) -> str:
    """Map a native perpetual id to the comparable spot market identity.

    OKX appends ``-SWAP`` to the native perpetual id while its spot market does not.
    Bybit and Gate use only separators that ``_normalise_symbol`` already removes.
    Keeping the venue-specific transform here makes the contract/spot check strict
    without rejecting every legitimate OKX mapping.
    """
    text = str(value or "").upper().strip()
    if venue == "okx" and text.endswith("-SWAP"):
        text = text[:-5]
    elif venue == "okx":
        # OKX pre-market instruments are reported as dated FUTURES before their
        # conversion.  The YYMMDD/YYYYMMDD suffix is lifecycle metadata, not part of
        # the spot market identity.
        text = re.sub(r"-\d{6,8}$", "", text)
    return _normalise_symbol(text)


def market_symbols_equivalent(venue: str, contract: Any, spot_symbol: Any) -> bool:
    return bool(_normalise_symbol(spot_symbol)) and (
        _normalise_contract_market(venue, contract) == _normalise_symbol(spot_symbol)
    )


def _parse_quoted_utc(value: Any) -> datetime | None:
    text = " ".join(str(value or "").split())
    for form in (
        "%b %d, %Y, %I:%M%p UTC",
        "%B %d, %Y, %I:%M%p UTC",
        "%b %d, %Y, %H:%M UTC",
        "%B %d, %Y, %H:%M UTC",
        "%Y-%m-%d %H:%M UTC",
    ):
        try:
            return datetime.strptime(text, form).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return _parse_explicit_utc(text)


def _official_record_problems(
    entry: Mapping[str, Any],
    *,
    number: int,
    metadata_episode_ids: set[str],
) -> list[str]:
    """Validate the semantics that make an OFFICIAL row an acceptance anchor."""
    problems: list[str] = []
    prefix = f"line {number}: "
    venue = str(entry.get("venue") or "")
    contract = str(entry.get("premarket_contract_id") or entry.get("symbol") or "")
    spot_symbol = str(entry.get("spot_symbol") or "")
    for field, value in (
        ("venue", venue),
        ("premarket_contract_id", contract),
        ("spot_symbol", spot_symbol),
    ):
        if (
            not value
            or value != value.strip()
            or any(character.isspace() for character in value)
        ):
            problems.append(prefix + f"official {field} is not canonical")
    try:
        generation = int(entry.get("lifecycle_generation", -1))
    except (TypeError, ValueError):
        generation = -1
    expected_episode = ""
    try:
        expected_episode = make_episode_id(venue, contract, generation)
    except (EventRegistryError, ValueError):
        pass
    if entry.get("episode_id") != expected_episode:
        problems.append(prefix + "official episode_id does not match venue/contract/generation")
    if expected_episode not in metadata_episode_ids:
        problems.append(prefix + "official episode has no matching metadata lifecycle record")
    if not market_symbols_equivalent(venue, contract, spot_symbol):
        problems.append(prefix + "official spot_symbol mapping does not match premarket contract")

    source_url = str(entry.get("source_url") or "")
    parsed = urllib.parse.urlsplit(source_url)
    try:
        source_port: int | str | None = parsed.port
    except ValueError:
        source_port = "INVALID"
    official_hosts = config.OFFICIAL_ANNOUNCEMENT_HOSTS.get(venue, ())
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() not in official_hosts
        or parsed.username is not None
        or parsed.password is not None
        or source_port is not None
        or "\\" in source_url
        or _has_forbidden_unicode_controls(source_url)
        or source_url != source_url.strip()
    ):
        problems.append(prefix + "official source_url is not an approved venue announcement host")
    received = _parse_explicit_utc(entry.get("received_at_utc"))
    if received is None:
        problems.append(prefix + "official received_at_utc is not explicit UTC")

    attestation = entry.get("attestation")
    if not isinstance(attestation, Mapping):
        problems.append(prefix + "official attestation object is missing")
        return problems
    if attestation.get("schema") != config.OFFICIAL_ATTESTATION_SCHEMA:
        problems.append(prefix + "official attestation schema is invalid")
    if str(attestation.get("announcement_url") or "") != source_url:
        problems.append(prefix + "attestation announcement_url does not match source_url")
    raw_attested_by = str(attestation.get("attested_by") or "")
    attested_by = raw_attested_by.strip()
    if (
        not attested_by
        or raw_attested_by != attested_by
        or _has_forbidden_unicode_controls(raw_attested_by)
        or entry.get("source_identity") != f"human_attestation:{attested_by}"
    ):
        problems.append(
            prefix
            + "attestation author is not canonical or does not match source_identity"
        )
    announced = _parse_explicit_utc(attestation.get("announced_utc"))
    if announced is None or int(announced.timestamp()) != int(entry.get("timestamp_ts") or 0):
        problems.append(prefix + "attestation announced_utc does not match official timestamp")
    if received is not None and announced is not None:
        actual_lead = int(announced.timestamp()) - int(received.timestamp())
        try:
            declared_lead = int(attestation.get("lead_sec_at_attestation"))
        except (TypeError, ValueError):
            declared_lead = -1
        if actual_lead < config.CAPTURE_WINDOW_BEFORE_SEC:
            problems.append(
                prefix + "official received_at_utc is noncausal or has insufficient lead"
            )
        if declared_lead != actual_lead:
            problems.append(prefix + "attestation lead does not equal t0 minus received_at")
    if int(entry.get("t0_precision_sec", 0) or 0) != 60:
        problems.append(prefix + "official attestation precision must be exactly 60 seconds")
    caveats = tuple(entry.get("caveats") or ())
    if caveats != ("OFFICIAL_T0_READ_BY_A_PERSON_FROM_ANNOUNCEMENT_PROSE",):
        problems.append(prefix + "official attestation caveat is missing or changed")
    quote = str(attestation.get("quoted_sentence") or "")
    quoted_time = str(attestation.get("quoted_time_text") or "")
    quoted_symbol = str(attestation.get("quoted_symbol_text") or "")
    for field, value in (
        ("quoted sentence", quote),
        ("quoted time fragment", quoted_time),
        ("quoted symbol fragment", quoted_symbol),
    ):
        if (
            not value
            or value != value.strip()
            or _has_forbidden_unicode_controls(value)
        ):
            problems.append(prefix + f"attestation {field} is not verbatim canonical text")
    if not quote or not quoted_time or quoted_time not in quote:
        problems.append(prefix + "attestation quoted time fragment is missing from sentence")
    if not quote or not quoted_symbol or quoted_symbol not in quote:
        problems.append(prefix + "attestation quoted symbol fragment is missing from sentence")
    if _normalise_symbol(quoted_symbol) != _normalise_symbol(spot_symbol):
        problems.append(prefix + "attestation quoted symbol does not match spot_symbol")
    quoted_moment = _parse_quoted_utc(quoted_time)
    if (
        quoted_moment is None
        or announced is None
        or int(quoted_moment.timestamp()) != int(announced.timestamp())
    ):
        problems.append(prefix + "attestation quoted time does not match announced_utc")
    return problems


def _verify_records(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    problems: list[str] = []
    metadata_episode_ids: set[str] = set()
    stream_heads: dict[str, Mapping[str, Any]] = {}
    previous_global_hash: str | None = None
    seen_hashes: set[str] = set()
    for index, entry in enumerate(entries):
        number = index + 1
        if entry.get("schema") != REGISTRY_SCHEMA:
            problems.append(f"line {number}: unknown registry schema")
        if entry.get("record_type") != "timestamp_observation":
            problems.append(f"line {number}: unknown record_type")
        if int(entry.get("record_seq", -1)) != index:
            problems.append(
                f"line {number}: record_seq {entry.get('record_seq')} does not follow {index - 1}"
            )
        if entry.get("previous_record_hash") != previous_global_hash:
            problems.append(f"line {number}: previous_record_hash does not match global head")
        claimed_hash = str(entry.get("record_hash") or "")
        if claimed_hash != _record_hash(entry):
            problems.append(f"line {number}: record_hash does not match its content")
        if claimed_hash in seen_hashes:
            problems.append(f"line {number}: duplicate record_hash")
        seen_hashes.add(claimed_hash)

        episode_id = str(entry.get("episode_id") or "").strip()
        if not episode_id:
            problems.append(f"line {number}: missing episode_id")
        try:
            generation = int(entry.get("lifecycle_generation", -1))
            expected_episode_id = make_episode_id(
                str(entry.get("venue") or ""),
                str(entry.get("premarket_contract_id") or entry.get("symbol") or ""),
                generation,
            )
            if episode_id != expected_episode_id:
                problems.append(
                    f"line {number}: episode_id does not match venue/contract/generation"
                )
        except (EventRegistryError, TypeError, ValueError):
            problems.append(f"line {number}: lifecycle_generation or episode identity is invalid")
        timestamp_kind = str(entry.get("timestamp_kind") or "")
        if timestamp_kind not in TIMESTAMP_KINDS:
            problems.append(f"line {number}: unknown timestamp_kind")
        if entry.get("source_class") not in SOURCE_CLASSES:
            problems.append(f"line {number}: unknown source_class")
        if entry.get("instrument_role") not in INSTRUMENT_ROLES:
            problems.append(f"line {number}: unknown instrument_role")
        if (
            entry.get("source_class") == SOURCE_VENUE_INSTRUMENT_METADATA
            and timestamp_kind
            in {
                TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
                TIMESTAMP_CONTRACT_CREATED,
                TIMESTAMP_TRANSITION,
            }
            and episode_id
        ):
            metadata_episode_ids.add(episode_id)
        if (
            timestamp_kind == TIMESTAMP_OFFICIAL_SPOT_T0
            or entry.get("source_class") == SOURCE_OFFICIAL_ANNOUNCEMENT
        ):
            if not (
                timestamp_kind == TIMESTAMP_OFFICIAL_SPOT_T0
                and entry.get("source_class") == SOURCE_OFFICIAL_ANNOUNCEMENT
                and entry.get("instrument_role") == "spot"
                and entry.get("capture_eligible") is True
                and entry.get("evidence_use") == "ACCEPTANCE_ANCHOR"
            ):
                problems.append(f"line {number}: malformed official acceptance anchor")
            problems.extend(
                _official_record_problems(
                    entry,
                    number=number,
                    metadata_episode_ids=metadata_episode_ids,
                )
            )

        stream_id = str(entry.get("stream_id") or "")
        try:
            expected_stream_id = _stream_id(
                episode_id=episode_id,
                timestamp_kind=timestamp_kind,
                instrument_role=str(entry.get("instrument_role") or ""),
                source_class=str(entry.get("source_class") or ""),
                source_identity=str(entry.get("source_identity") or ""),
            )
        except Exception:  # pragma: no cover - values are diagnosed individually
            expected_stream_id = ""
        if stream_id != expected_stream_id:
            problems.append(f"line {number}: stream_id does not match immutable fields")
        previous_stream = stream_heads.get(stream_id)
        expected_stream_revision = (
            int(previous_stream.get("stream_revision", -1)) + 1
            if previous_stream is not None
            else 0
        )
        if int(entry.get("stream_revision", -1)) != expected_stream_revision:
            problems.append(
                f"line {number}: stream_revision {entry.get('stream_revision')} "
                f"does not follow {expected_stream_revision - 1}"
            )
        expected_supersedes = (
            previous_stream.get("record_hash") if previous_stream is not None else None
        )
        if entry.get("supersedes_record_hash") != expected_supersedes:
            problems.append(
                f"line {number}: supersedes_record_hash does not match stream head"
            )
        if stream_id:
            stream_heads[stream_id] = entry
        previous_global_hash = claimed_hash
    return problems


def _verify_registry_snapshot(
    path: Path | None = None,
    *,
    verify_summary: bool = True,
    bootstrap_lock_owner: RegistryLockOwner | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load one registry snapshot and verify its lineage and production receipt."""
    path = path or REGISTRY_PATH
    production_registry = (
        path.resolve(strict=False) == REGISTRY_PATH.resolve(strict=False)
    )
    lock_path = REGISTRY_LOCK_PATH if production_registry else path.with_suffix(".lock")
    problems: list[str] = []
    if bootstrap_lock_owner is None:
        if lock_path.is_file():
            problems.append("registry mutation lock is active; stable snapshot unavailable")
    else:
        try:
            if (
                bootstrap_lock_owner.path.resolve(strict=False)
                != lock_path.resolve(strict=False)
            ):
                raise EventRegistryError("registry lock does not cover this snapshot")
            _assert_registry_lock_owner(bootstrap_lock_owner)
        except EventRegistryError as exc:
            problems.append(f"registry snapshot lock ownership is invalid: {exc}")

    registry_bytes = path.read_bytes() if path.is_file() else b""
    summary_path = path.with_suffix(".summary.json")
    summary_bytes = summary_path.read_bytes() if summary_path.is_file() else b""
    entries = _load_registry_bytes(registry_bytes, path=path)
    problems.extend(_verify_records(entries))
    summary_required = bool(entries) and production_registry
    summary_verified = False
    summary_pending_under_lock = False
    if summary_required and not verify_summary:
        if bootstrap_lock_owner is None:
            problems.append(
                "production registry summary verification cannot be disabled without "
                "the active registry lock"
            )
        else:
            expected_lock_path = REGISTRY_LOCK_PATH
            try:
                if (
                    bootstrap_lock_owner.path.resolve(strict=False)
                    != expected_lock_path.resolve(strict=False)
                ):
                    raise EventRegistryError(
                        "bootstrap lock does not cover the production registry"
                    )
                if bootstrap_lock_owner.plan_hash != trust_root.PLAN_HASH:
                    raise EventRegistryError(
                        "bootstrap lock is not bound to the active PlanOnly"
                    )
                _assert_registry_lock_owner(bootstrap_lock_owner)
                summary_pending_under_lock = True
            except EventRegistryError as exc:
                problems.append(f"production registry summary bootstrap is invalid: {exc}")
    if verify_summary:
        if summary_bytes:
            try:
                summary = json.loads(summary_bytes.decode("utf-8"))
                if not isinstance(summary, Mapping):
                    raise ValueError("summary root is not an object")
                summary_problem_count = len(problems)
                receipt = summary.get("registry")
                if not isinstance(receipt, Mapping):
                    raise ValueError("summary has no registry receipt")
                if production_registry:
                    if summary.get("schema") != REGISTRY_SCHEMA:
                        problems.append("summary schema does not match registry")
                    if summary.get("status") not in {
                        "REFRESH_COMPLETE",
                        "REGISTRY_MUTATION_COMPLETE",
                    }:
                        problems.append("summary status is not a complete registry mutation")
                    if summary.get("complete") is not True:
                        problems.append("summary complete flag is not true")
                    if summary.get("plan_hash") != trust_root.PLAN_HASH:
                        problems.append("summary plan_hash is not the active PlanOnly")
                    mutation_type = str(summary.get("mutation_type") or "metadata_refresh")
                    if mutation_type not in {"metadata_refresh", "official_attestation"}:
                        problems.append("summary mutation_type is invalid")
                    run_identity = (
                        summary.get("mutation_run_id")
                        if mutation_type == "official_attestation"
                        else summary.get("refresh_run_id") or summary.get("mutation_run_id")
                    )
                    if not str(run_identity or "").strip():
                        problems.append("summary mutation run id is missing")
                    resolved_paths_hash = str(summary.get("resolved_paths_hash") or "")
                    if len(resolved_paths_hash) != 64 or any(
                        character not in "0123456789abcdef"
                        for character in resolved_paths_hash
                    ):
                        problems.append("summary resolved_paths_hash is invalid")
                    active_state = summary.get(ACTIVE_CONTRACTS_FIELD)
                    if not isinstance(active_state, Mapping):
                        problems.append("summary active contract state is missing or invalid")
                    else:
                        for adapter in ADAPTERS:
                            values = active_state.get(adapter.venue)
                            if not isinstance(values, list) or not all(
                                isinstance(value, str)
                                and bool(value.strip())
                                and value == value.strip()
                                for value in values
                            ):
                                problems.append(
                                    f"summary active contract state is invalid for {adapter.venue}"
                                )
                    if receipt.get("status") != "REGISTRY_OK":
                        problems.append("summary registry status is not REGISTRY_OK")
                if int(receipt.get("entries", -1)) != len(entries):
                    problems.append("summary entry count does not match registry")
                actual_head = entries[-1].get("record_hash") if entries else None
                if receipt.get("head_record_hash") != actual_head:
                    problems.append("summary head_record_hash does not match registry")
                anchor_present = any(
                    field in summary for field in MUTATION_SUMMARY_LINK_FIELDS
                ) or _mutation_receipt_dir(path).exists()
                if (production_registry and summary_required) or anchor_present:
                    problems.extend(
                        _mutation_receipt_anchor_problems(
                            path,
                            summary=summary,
                            registry_bytes=registry_bytes,
                            entries=entries,
                        )
                    )
                summary_verified = len(problems) == summary_problem_count
            except (OSError, ValueError, TypeError) as exc:
                problems.append(f"summary receipt is unreadable: {type(exc).__name__}: {exc}")
        elif summary_required:
            problems.append("required summary receipt is missing for nonempty production registry")
    registry_bytes_after = path.read_bytes() if path.is_file() else b""
    summary_bytes_after = summary_path.read_bytes() if summary_path.is_file() else b""
    if registry_bytes_after != registry_bytes or summary_bytes_after != summary_bytes:
        problems.append("registry or summary changed during snapshot verification")
    if bootstrap_lock_owner is not None:
        try:
            _assert_registry_lock_owner(bootstrap_lock_owner)
        except EventRegistryError as exc:
            problems.append(f"registry snapshot lock changed during verification: {exc}")
    if bootstrap_lock_owner is None and lock_path.is_file():
        if "registry mutation lock is active; stable snapshot unavailable" not in problems:
            problems.append("registry mutation lock appeared during snapshot verification")
    episodes = materialize_episodes(entries)
    heads = [head[2] for head in _stream_heads(entries).values()]
    report = {
        "status": "REGISTRY_OK" if not problems else "REGISTRY_PROBLEMS",
        "entries": len(entries),
        "events": len(episodes),
        "episodes": len(episodes),
        "head_record_hash": entries[-1].get("record_hash") if entries else None,
        "registry_sha256": (
            hashlib.sha256(registry_bytes).hexdigest() if registry_bytes else None
        ),
        "registry_summary_sha256": (
            hashlib.sha256(summary_bytes).hexdigest()
            if verify_summary and summary_bytes
            else None
        ),
        "problems": problems,
        "summary_required": summary_required,
        "summary_verified": summary_verified,
        "recovery_action": (
            "RESTORE_MATCHING_SUMMARY_OR_QUARANTINE_AND_BOOTSTRAP_NEW_GENERATION"
            if summary_required and not summary_verified and not summary_pending_under_lock
            else None
        ),
        "bootstrap_state": (
            "EMPTY_REGISTRY_BOOTSTRAP"
            if production_registry and not entries
            else "SUMMARY_PENDING_UNDER_LOCK"
            if summary_pending_under_lock
            else "ESTABLISHED"
            if production_registry and summary_verified
            else "RECOVERY_REQUIRED"
            if production_registry
            else "NON_PRODUCTION_VALIDATION"
        ),
        "by_source_class": _count(heads, "source_class"),
        "by_venue": _count(episodes, "venue"),
    }
    return entries, report


def verify_registry(
    path: Path | None = None,
    *,
    verify_summary: bool = True,
    bootstrap_lock_owner: RegistryLockOwner | None = None,
) -> dict[str, Any]:
    """Fail closed on tampering, reorder, fork, or orphan source revisions."""
    _entries, report = _verify_registry_snapshot(
        path,
        verify_summary=verify_summary,
        bootstrap_lock_owner=bootstrap_lock_owner,
    )
    return report


def _count(entries: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        value = str(entry.get(key))
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def events_for_capture(
    registry_path: Path | None = None,
    *,
    now_ts: int,
    source_class: str,
    horizon_sec: int = 24 * 3600,
) -> list[dict[str, Any]]:
    """Events whose t0 is still ahead, within one source class only.

    The source class is a required argument rather than a filter applied afterwards:
    mixing an announcement-derived t0 with a metadata-derived one in the same capture
    set would reintroduce exactly the defect this registry exists to avoid."""
    if registry_path is not None and not isinstance(registry_path, (str, os.PathLike)):
        raise EventRegistryError(
            "VERIFIED_PRODUCTION_REGISTRY_REQUIRED: direct event sequences are not "
            "capture eligible"
        )
    path = Path(registry_path) if registry_path is not None else REGISTRY_PATH
    if path.resolve(strict=False) != REGISTRY_PATH.resolve(strict=False):
        raise EventRegistryError(
            "VERIFIED_PRODUCTION_REGISTRY_REQUIRED: capture selection only reads "
            "the production registry"
        )
    entries, report = _verify_registry_snapshot(path)
    if report["status"] != "REGISTRY_OK" or (
        report["summary_required"] and not report["summary_verified"]
    ):
        raise EventRegistryError(
            "VERIFIED_PRODUCTION_REGISTRY_REQUIRED: "
            + "; ".join(report["problems"] or ["registry summary is not verified"])
        )
    if source_class not in SOURCE_CLASSES:
        raise EventRegistryError(f"unknown source class: {source_class}")
    if source_class != SOURCE_OFFICIAL_ANNOUNCEMENT:
        raise EventRegistryError(
            "capture selection requires OFFICIAL_ANNOUNCEMENT; metadata proxies are descriptive-only"
        )
    try:
        summary = json.loads(
            path.with_suffix(".summary.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise EventRegistryError(
            f"STALE_METADATA_REFRESH: summary freshness anchor is unreadable: {exc}"
        ) from exc
    metadata_refresh_received = _parse_explicit_utc(
        summary.get(LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD)
        if isinstance(summary, Mapping)
        else None
    )
    if metadata_refresh_received is None:
        raise EventRegistryError(
            "STALE_METADATA_REFRESH: complete metadata refresh anchor is missing"
        )
    metadata_age_sec = now_ts - int(metadata_refresh_received.timestamp())
    if not 0 <= metadata_age_sec <= MAX_COMPLETE_METADATA_REFRESH_AGE_SEC:
        raise EventRegistryError(
            "STALE_METADATA_REFRESH: latest complete metadata refresh is "
            f"{metadata_age_sec}s old; maximum is "
            f"{MAX_COMPLETE_METADATA_REFRESH_AGE_SEC}s"
        )
    active_generations, _high_water = _load_lifecycle_generation_state(
        path.with_suffix(".summary.json"),
        existing=entries,
    )
    mutation_receipts, mutation_receipt_problems = _load_mutation_receipt_chain(path)
    if mutation_receipt_problems or not mutation_receipts:
        raise EventRegistryError(
            "VERIFIED_PRODUCTION_REGISTRY_REQUIRED: latest mutation receipt is unavailable"
        )
    latest_mutation_receipt = mutation_receipts[-1]
    registry_authority_state_hash = canonical_hash(
        {
            ACTIVE_LIFECYCLE_GENERATIONS_FIELD: latest_mutation_receipt.get(
                ACTIVE_LIFECYCLE_GENERATIONS_FIELD
            ),
            LIFECYCLE_GENERATION_HIGH_WATER_FIELD: latest_mutation_receipt.get(
                LIFECYCLE_GENERATION_HIGH_WATER_FIELD
            ),
            LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD: (
                latest_mutation_receipt.get(
                    LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD
                )
            ),
            RAW_UNIVERSE_ROWS_FIELD: latest_mutation_receipt.get(
                RAW_UNIVERSE_ROWS_FIELD
            ),
        }
    )
    episodes = materialize_episodes(entries)
    def is_due_t0(value: Any) -> bool:
        t0 = int(value or 0)
        target = t0 - config.CAPTURE_WINDOW_BEFORE_SEC
        return (
            target - config.CAPTURE_LAUNCH_EARLY_GRACE_SEC
            <= now_ts
            <= target + config.CAPTURE_LAUNCH_LATE_GRACE_SEC
            and t0 <= now_ts + horizon_sec
        )

    conflicts = [
        str(episode.get("episode_id"))
        for episode in episodes
        if episode.get("official_conflict") is True
        and any(
            is_due_t0(observation.get("timestamp_ts"))
            for observation in episode.get("timestamp_observations") or []
            if observation.get("timestamp_kind") == TIMESTAMP_OFFICIAL_SPOT_T0
        )
    ]
    if conflicts:
        raise EventRegistryError(
            "OFFICIAL_CONFLICT: conflicting official_spot_t0 heads for "
            + ", ".join(sorted(conflicts))
        )
    upcoming: list[dict[str, Any]] = []
    for entry in episodes:
        provenance = entry.get("official_t0_provenance")
        if not isinstance(provenance, Mapping):
            continue
        venue = str(entry.get("venue") or "")
        contract = str(entry.get("premarket_contract_id") or "")
        active_generation = active_generations.get(venue, {}).get(contract)
        received = _parse_explicit_utc(provenance.get("received_at_utc"))
        if (
            entry.get("t0_source_class") == source_class
            and entry.get("capture_eligible") is True
            and entry.get("evidence_use") == "ACCEPTANCE_ANCHOR"
            and active_generation is not None
            and int(entry.get("lifecycle_generation", -1)) == active_generation
            and is_due_t0(entry.get("official_spot_t0"))
            and received is not None
            and int(received.timestamp()) <= now_ts
        ):
            upcoming.append(entry)
    upcoming.sort(
        key=lambda item: (item["official_spot_t0"], item["venue"], item["symbol"])
    )
    for event in upcoming:
        provenance = dict(event.get("official_t0_provenance") or {})
        event.update({
            "official_record_hash": provenance.get("record_hash"),
            "official_source_url": provenance.get("source_url"),
            "official_source_identity": provenance.get("source_identity"),
            "registry_sha256": report.get("registry_sha256"),
            "registry_summary_sha256": report.get("registry_summary_sha256"),
            "registry_tail_record_hash": report.get("head_record_hash"),
            "mutation_receipt_seq": latest_mutation_receipt.get("mutation_seq"),
            "mutation_receipt_hash": latest_mutation_receipt.get("receipt_hash"),
            "summary_content_sha256": latest_mutation_receipt.get(
                "summary_content_hash"
            ),
            "registry_authority_state_hash": registry_authority_state_hash,
            "plan_id": trust_root.PLAN_ID,
            "plan_hash": trust_root.PLAN_HASH,
        })
    return upcoming


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _mutation_receipt_dir(path: Path) -> Path:
    return path.with_name(path.name + MUTATION_RECEIPT_DIR_SUFFIX)


def _summary_content_hash(summary: Mapping[str, Any]) -> str:
    return canonical_hash(
        {
            key: value
            for key, value in summary.items()
            if key not in MUTATION_SUMMARY_LINK_FIELDS
        }
    )


def _load_mutation_receipt_chain(
    path: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    directory = _mutation_receipt_dir(path)
    if not directory.exists():
        return [], []
    if not directory.is_dir():
        return [], ["mutation receipt anchor path is not a directory"]
    receipts: list[dict[str, Any]] = []
    problems: list[str] = []
    files = sorted(directory.glob("*.json"), key=lambda item: item.name)
    unexpected = sorted(
        item.name for item in directory.iterdir() if not item.is_file() or item.suffix != ".json"
    )
    if unexpected:
        problems.append("mutation receipt anchor contains unexpected entries")
    previous_hash: str | None = None
    for expected_seq, receipt_path in enumerate(files):
        try:
            raw = receipt_path.read_bytes()
            receipt = json.loads(raw.decode("utf-8"))
            if not isinstance(receipt, Mapping):
                raise ValueError("receipt root is not an object")
            receipt = dict(receipt)
        except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
            problems.append(
                f"mutation receipt {receipt_path.name} is unreadable: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        claimed_hash = str(receipt.get("receipt_hash") or "")
        computed_hash = canonical_hash(
            {key: value for key, value in receipt.items() if key != "receipt_hash"}
        )
        expected_name = f"{expected_seq:020d}-{claimed_hash}.json"
        if receipt_path.name != expected_name:
            problems.append(
                f"mutation receipt {receipt_path.name} does not match sequence/hash identity"
            )
        if receipt.get("schema") != MUTATION_RECEIPT_SCHEMA:
            problems.append(f"mutation receipt {receipt_path.name} has the wrong schema")
        if receipt.get("mutation_seq") != expected_seq:
            problems.append(f"mutation receipt {receipt_path.name} breaks sequence order")
        if receipt.get("previous_mutation_receipt_hash") != previous_hash:
            problems.append(f"mutation receipt {receipt_path.name} breaks the hash chain")
        if claimed_hash != computed_hash:
            problems.append(f"mutation receipt {receipt_path.name} hash is invalid")
        if not _is_sha256_text(receipt.get("registry_sha256")):
            problems.append(f"mutation receipt {receipt_path.name} registry hash is invalid")
        if not _is_sha256_text(receipt.get("summary_content_hash")):
            problems.append(f"mutation receipt {receipt_path.name} summary hash is invalid")
        receipts.append(receipt)
        previous_hash = claimed_hash
    return receipts, problems


def _mutation_compare_and_swap_token(path: Path) -> tuple[int, str | None]:
    """Identify the last fully committed mutation before a refresh starts staging."""
    receipts, problems = _load_mutation_receipt_chain(path)
    if problems:
        raise EventRegistryError(
            "refresh base mutation receipt chain is invalid: " + "; ".join(problems)
        )
    return len(receipts), receipts[-1]["receipt_hash"] if receipts else None


def _mutation_receipt_anchor_problems(
    path: Path,
    *,
    summary: Mapping[str, Any],
    registry_bytes: bytes,
    entries: Sequence[Mapping[str, Any]],
) -> list[str]:
    receipts, problems = _load_mutation_receipt_chain(path)
    if not receipts:
        return [*problems, "required immutable mutation receipt anchor is missing"]
    latest = receipts[-1]
    actual_registry_hash = hashlib.sha256(registry_bytes).hexdigest()
    actual_head = entries[-1].get("record_hash") if entries else None
    run_identity = (
        summary.get("mutation_run_id")
        if summary.get("mutation_type") == "official_attestation"
        else summary.get("refresh_run_id") or summary.get("mutation_run_id")
    )
    expected = {
        "registry_path_name": path.name,
        "registry_sha256": actual_registry_hash,
        "registry_entries": len(entries),
        "registry_head_record_hash": actual_head,
        "summary_content_hash": _summary_content_hash(summary),
        "mutation_type": summary.get("mutation_type"),
        "mutation_run_id": run_identity,
        "plan_id": summary.get("plan_id"),
        "plan_hash": summary.get("plan_hash"),
        ACTIVE_CONTRACTS_FIELD: summary.get(ACTIVE_CONTRACTS_FIELD),
        ACTIVE_LIFECYCLE_GENERATIONS_FIELD: summary.get(
            ACTIVE_LIFECYCLE_GENERATIONS_FIELD
        ),
        LIFECYCLE_GENERATION_HIGH_WATER_FIELD: summary.get(
            LIFECYCLE_GENERATION_HIGH_WATER_FIELD
        ),
        LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD: summary.get(
            LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD
        ),
        RAW_UNIVERSE_ROWS_FIELD: summary.get(RAW_UNIVERSE_ROWS_FIELD),
    }
    for field, value in expected.items():
        if latest.get(field) != value:
            problems.append(f"immutable mutation receipt does not match current {field}")
    summary_links = {
        "mutation_receipt_schema": MUTATION_RECEIPT_SCHEMA,
        "mutation_seq": latest.get("mutation_seq"),
        "previous_mutation_receipt_hash": latest.get(
            "previous_mutation_receipt_hash"
        ),
        "mutation_receipt_hash": latest.get("receipt_hash"),
    }
    for field, value in summary_links.items():
        if summary.get(field) != value:
            problems.append(f"summary {field} does not match immutable mutation receipt")
    return problems


def _summary_with_exact_lifecycle_state(
    summary: Mapping[str, Any],
    *,
    registry_entries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a summary with exact active-generation and high-water fields."""
    payload = dict(summary)
    if payload.get("mutation_type") == "metadata_refresh":
        metadata_refresh_received_at = payload.get(
            LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD
        ) or payload.get("refreshed_at_utc")
        if _parse_explicit_utc(metadata_refresh_received_at) is None:
            raise EventRegistryError(
                "complete metadata refresh requires an explicit UTC freshness anchor"
            )
        payload[LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD] = (
            metadata_refresh_received_at
        )
    elif _parse_explicit_utc(
        payload.get(LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD)
    ) is None:
        raise EventRegistryError(
            "non-metadata mutation must preserve the complete metadata refresh anchor"
        )
    if RAW_UNIVERSE_ROWS_FIELD in payload:
        payload[RAW_UNIVERSE_ROWS_FIELD] = _parse_raw_universe_counts(
            payload[RAW_UNIVERSE_ROWS_FIELD]
        )
    elif payload.get("mutation_type") == "metadata_refresh":
        # Compatibility-only bootstrap for historical test receipts. Runtime refresh
        # always supplies full raw API row counts before entering this function.
        counts = {adapter.venue: 0 for adapter in ADAPTERS}
        for entry in registry_entries:
            venue = str(entry.get("venue") or "")
            if venue in counts:
                counts[venue] += 1
        payload[RAW_UNIVERSE_ROWS_FIELD] = dict(sorted(counts.items()))
    else:
        raise EventRegistryError(
            "non-metadata mutation must preserve raw universe row counts"
        )
    active_ids = _parse_active_contract_ids(payload.get(ACTIVE_CONTRACTS_FIELD))
    has_active = ACTIVE_LIFECYCLE_GENERATIONS_FIELD in payload
    has_high_water = LIFECYCLE_GENERATION_HIGH_WATER_FIELD in payload
    if has_active != has_high_water:
        raise EventRegistryError(
            "active lifecycle generation and high-water fields must be written together"
        )
    derived_high_water = _derived_lifecycle_high_water(registry_entries)
    if has_active:
        active = _parse_lifecycle_generation_state(
            payload[ACTIVE_LIFECYCLE_GENERATIONS_FIELD],
            field_name=ACTIVE_LIFECYCLE_GENERATIONS_FIELD,
        )
        high_water = _parse_lifecycle_generation_state(
            payload[LIFECYCLE_GENERATION_HIGH_WATER_FIELD],
            field_name=LIFECYCLE_GENERATION_HIGH_WATER_FIELD,
        )
    else:
        high_water = {
            venue: dict(values)
            for venue, values in derived_high_water.items()
        }
        active = {adapter.venue: {} for adapter in ADAPTERS}
        for venue, contracts in active_ids.items():
            for contract in contracts:
                generation = high_water[venue].get(contract, 0)
                active[venue][contract] = generation
                high_water[venue][contract] = max(
                    high_water[venue].get(contract, -1), generation
                )
    for venue in active:
        if sorted(active[venue]) != active_ids[venue]:
            raise EventRegistryError(
                "active contract ids do not match exact lifecycle generations"
            )
        for contract, generation in active[venue].items():
            if high_water[venue].get(contract, -1) != generation:
                raise EventRegistryError(
                    "active lifecycle generation must equal contract high-water"
                )
        for contract, generation in derived_high_water[venue].items():
            if high_water[venue].get(contract, -1) < generation:
                raise EventRegistryError("lifecycle generation high-water regressed")
    payload[ACTIVE_LIFECYCLE_GENERATIONS_FIELD] = {
        venue: dict(sorted(values.items()))
        for venue, values in sorted(active.items())
    }
    payload[LIFECYCLE_GENERATION_HIGH_WATER_FIELD] = {
        venue: dict(sorted(values.items()))
        for venue, values in sorted(high_water.items())
    }
    return payload


def _write_summary_with_mutation_receipt(
    path: Path,
    summary: Mapping[str, Any],
    *,
    lock_owner: RegistryLockOwner | None,
) -> dict[str, Any]:
    """Write the mutable summary, then seal it with one O_EXCL receipt.

    Production callers must hold the registry mutation lock.  Tests may bootstrap a
    non-production temporary path before rebinding it as their production fixture.
    """
    production_registry = path.resolve(strict=False) == REGISTRY_PATH.resolve(strict=False)
    if lock_owner is None:
        if production_registry:
            raise EventRegistryError("production mutation receipt requires registry lock")
    else:
        expected_lock_path = (
            REGISTRY_LOCK_PATH if production_registry else path.with_suffix(".lock")
        )
        if lock_owner.path.resolve(strict=False) != expected_lock_path.resolve(strict=False):
            raise EventRegistryError("registry lock does not cover mutation receipt")
        _assert_registry_lock_owner(lock_owner)

    receipts, receipt_problems = _load_mutation_receipt_chain(path)
    if receipt_problems:
        raise EventRegistryError(
            "existing mutation receipt chain is invalid: " + "; ".join(receipt_problems)
        )
    mutation_seq = len(receipts)
    previous_hash = receipts[-1]["receipt_hash"] if receipts else None
    summary_payload = {
        key: value for key, value in dict(summary).items() if key not in MUTATION_SUMMARY_LINK_FIELDS
    }
    registry_bytes = path.read_bytes() if path.is_file() else b""
    registry_entries = _load_registry_bytes(registry_bytes, path=path)
    summary_payload = _summary_with_exact_lifecycle_state(
        summary_payload,
        registry_entries=registry_entries,
    )
    record = {
        "schema": MUTATION_RECEIPT_SCHEMA,
        "mutation_seq": mutation_seq,
        "previous_mutation_receipt_hash": previous_hash,
        "registry_path_name": path.name,
        "registry_sha256": hashlib.sha256(registry_bytes).hexdigest(),
        "registry_entries": len(registry_entries),
        "registry_head_record_hash": (
            registry_entries[-1].get("record_hash") if registry_entries else None
        ),
        "summary_content_hash": _summary_content_hash(summary_payload),
        "mutation_type": summary_payload.get("mutation_type"),
        "mutation_run_id": (
            summary_payload.get("mutation_run_id")
            if summary_payload.get("mutation_type") == "official_attestation"
            else summary_payload.get("refresh_run_id")
            or summary_payload.get("mutation_run_id")
        ),
        "plan_id": summary_payload.get("plan_id"),
        "plan_hash": summary_payload.get("plan_hash"),
        ACTIVE_CONTRACTS_FIELD: summary_payload.get(ACTIVE_CONTRACTS_FIELD),
        ACTIVE_LIFECYCLE_GENERATIONS_FIELD: summary_payload.get(
            ACTIVE_LIFECYCLE_GENERATIONS_FIELD
        ),
        LIFECYCLE_GENERATION_HIGH_WATER_FIELD: summary_payload.get(
            LIFECYCLE_GENERATION_HIGH_WATER_FIELD
        ),
        LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD: summary_payload.get(
            LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD
        ),
        RAW_UNIVERSE_ROWS_FIELD: summary_payload.get(RAW_UNIVERSE_ROWS_FIELD),
    }
    record["receipt_hash"] = canonical_hash(record)
    linked_summary = dict(summary_payload)
    linked_summary.update(
        {
            "mutation_receipt_schema": MUTATION_RECEIPT_SCHEMA,
            "mutation_seq": mutation_seq,
            "previous_mutation_receipt_hash": previous_hash,
            "mutation_receipt_hash": record["receipt_hash"],
        }
    )
    _write_json_atomic(path.with_suffix(".summary.json"), linked_summary)

    receipt_dir = _mutation_receipt_dir(path)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / (
        f"{mutation_seq:020d}-{record['receipt_hash']}.json"
    )
    try:
        descriptor = os.open(receipt_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise EventRegistryError(f"mutation receipt already exists: {receipt_path}") from exc
    committed = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        committed = True
    finally:
        if not committed:
            receipt_path.unlink(missing_ok=True)
    return linked_summary


def _snapshot_mutation_files(path: Path) -> dict[str, Any]:
    summary_path = path.with_suffix(".summary.json")
    receipt_dir = _mutation_receipt_dir(path)
    return {
        "registry_exists": path.is_file(),
        "registry_bytes": path.read_bytes() if path.is_file() else b"",
        "summary_exists": summary_path.is_file(),
        "summary_bytes": summary_path.read_bytes() if summary_path.is_file() else b"",
        "receipt_dir_exists": receipt_dir.is_dir(),
        "receipt_names": frozenset(
            item.name for item in receipt_dir.glob("*.json")
        ) if receipt_dir.is_dir() else frozenset(),
    }


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.rollback.tmp"
    )
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _rollback_uncommitted_mutation(path: Path, snapshot: Mapping[str, Any]) -> bool:
    """Restore both mutable files iff no new immutable receipt was committed."""
    receipt_dir = _mutation_receipt_dir(path)
    current_receipts = frozenset(
        item.name for item in receipt_dir.glob("*.json")
    ) if receipt_dir.is_dir() else frozenset()
    if current_receipts - set(snapshot["receipt_names"]):
        return False
    if snapshot["registry_exists"]:
        _write_bytes_atomic(path, bytes(snapshot["registry_bytes"]))
    else:
        path.unlink(missing_ok=True)
    summary_path = path.with_suffix(".summary.json")
    if snapshot["summary_exists"]:
        _write_bytes_atomic(summary_path, bytes(snapshot["summary_bytes"]))
    else:
        summary_path.unlink(missing_ok=True)
    if not snapshot["receipt_dir_exists"] and receipt_dir.is_dir():
        try:
            receipt_dir.rmdir()
        except OSError:
            pass
    return True



def quarantine_registry(
    *,
    run_id: str,
    reason: str,
    path: Path | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Move a registry generation aside, intact, so a new one can be bootstrapped.

    verify_registry has always been able to name this recovery action; until now it was
    only a string in a report, which left the operator to improvise exactly the step
    where improvising is worst. Nothing is deleted: the files are moved under
    docs/registry/quarantine/<timestamp>-<run_id>/ with a receipt recording why, what
    was moved and the SHA-256 of each file as it was moved.

    A registry is quarantined, not discarded, because the reason it stopped verifying -
    a superseded plan, a broken lineage - is itself evidence about how this project
    behaved, and evidence is the one thing here that is never thrown away.
    """
    if not str(run_id).strip():
        raise EventRegistryError("quarantine run_id is required")
    if not str(reason).strip():
        raise EventRegistryError("quarantine reason is required")

    target = path or REGISTRY_PATH
    summary = target.with_suffix(".summary.json")
    receipts = target.with_name(target.name + ".mutation-receipts")
    if not target.is_file():
        raise EventRegistryError(f"no registry to quarantine at {target}")

    # The directory name stays short on purpose. Mutation-receipt filenames are
    # already ~85 characters, and a quarantine prefix built from a full run_id pushed
    # the total past the 260-character Windows path limit - git refused to add the
    # files, which would have made the recovery unrecordable. The full run_id and the
    # reason live in the receipt, where length costs nothing.
    stamp = (now_utc or utc_now_iso()).replace(":", "").replace("-", "")
    tag = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]
    destination = target.parent / "quarantine" / f"{stamp}-{tag}"
    destination.mkdir(parents=True, exist_ok=False)

    moved: list[dict[str, Any]] = []
    for source in (target, summary):
        if not source.exists():
            continue
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        shutil.move(str(source), str(destination / source.name))
        moved.append({"name": source.name, "sha256": digest})

    # The receipt directory is collapsed into one file rather than moved as a tree.
    # A mutation-receipt filename is a twenty-digit sequence plus a 64-character hash;
    # nested under a quarantine directory the path exceeded the 260-character Windows
    # limit on a CI runner whose workspace root is longer than a developer's checkout,
    # and git could not create the files at all. Collapsing keeps every byte and every
    # original name while making the path short wherever it is checked out.
    if receipts.is_dir():
        collapsed = destination / "mutation-receipts.jsonl"
        lines: list[str] = []
        for entry in sorted(receipts.iterdir()):
            if not entry.is_file():
                continue
            raw = entry.read_bytes()
            lines.append(json.dumps({
                "original_name": entry.name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "receipt": json.loads(raw.decode("utf-8")),
            }, ensure_ascii=False, sort_keys=True))
        collapsed.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n"
        )
        shutil.rmtree(receipts)
        moved.append({
            "name": receipts.name,
            "sha256": hashlib.sha256(collapsed.read_bytes()).hexdigest(),
            "collapsed_into": collapsed.name,
            "receipts": len(lines),
        })

    receipt = {
        "schema": "premarket_perp_registry_quarantine_v1",
        "quarantined_at_utc": now_utc or utc_now_iso(),
        "run_id": run_id,
        "reason": reason,
        "moved": moved,
        "quarantine_dir": str(destination),
        "active_plan_id": trust_root.PLAN_ID,
        "active_plan_hash": trust_root.PLAN_HASH,
    }
    (destination / "quarantine-receipt.json").write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return receipt


def refresh(
    *,
    payloads: Mapping[str, Any] | None = None,
    path: Path | None = None,
    observed_at_utc: str | None = None,
    timeout_sec: int = 20,
    run_id: str,
) -> dict[str, Any]:
    """Stage every venue, then atomically append only a complete verified refresh."""
    if not str(run_id).strip():
        raise EventRegistryError("metadata refresh run_id is required")
    try:
        preflight = risk_gate.preflight(write_class="metadata_registry", run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - no failed preflight may reach network
        raise EventRegistryError(f"PREFLIGHT_BLOCKED: {type(exc).__name__}: {exc}") from exc
    if not _preflight_is_exact(
        preflight,
        write_class="metadata_registry",
        run_id=run_id,
        decision="ALLOW_METADATA_REGISTRY",
        action=risk_gate.METADATA_REGISTRY_ACTION,
    ):
        raise EventRegistryError("PREFLIGHT_BLOCKED: metadata preflight is not verified")

    registry_path = path or REGISTRY_PATH
    production_registry = (
        registry_path.resolve(strict=False) == REGISTRY_PATH.resolve(strict=False)
    )
    if payloads is not None and production_registry:
        raise EventRegistryError(
            "INJECTED_PAYLOADS_FORBIDDEN_FOR_PRODUCTION: supplied fixtures require "
            "an explicit non-production path"
        )
    if payloads is None and (path is not None or not production_registry):
        raise EventRegistryError(
            "LIVE_REFRESH_REQUIRES_CANONICAL_PRODUCTION_PATH: omit path for the "
            "gate-authorized public metadata refresh"
        )
    if payloads is None and observed_at_utc is not None:
        raise EventRegistryError(
            "LIVE_REFRESH_OBSERVED_AT_OVERRIDE_FORBIDDEN: completion time is "
            "writer-owned"
        )
    refresh_evidence_class = (
        "PUBLIC_VENUE_METADATA_LIVE"
        if payloads is None
        else "SYNTHETIC_OFFLINE_FIXTURE_ONLY"
    )
    production_eligible = payloads is None and production_registry
    staged_from_mutation = _mutation_compare_and_swap_token(registry_path)

    observed: list[dict[str, Any]] = []
    staged_rows: dict[str, list[Mapping[str, Any]]] = {}
    per_venue: dict[str, int] = {}
    per_venue_pages: dict[str, int] = {}
    truncated: list[str] = []
    errors: dict[str, str] = {}

    for adapter in ADAPTERS:
        try:
            if payloads is not None:
                if adapter.venue not in payloads:
                    raise EventRegistryError("venue payload is missing")
                supplied = payloads[adapter.venue]
                pages = (
                    list(supplied)
                    if isinstance(supplied, list) and adapter.cursor_param
                    else [supplied]
                )
                queue = list(pages)

                def fetch(_adapter, _params, queue=queue):  # noqa: ANN001
                    if not queue:
                        raise EventRegistryError(
                            f"{_adapter.venue} pagination payload ended before its cursor"
                        )
                    payload = queue.pop(0)
                    _validate_payload_shape(_adapter, payload)
                    return payload
            else:
                def fetch(adapter_, params):  # noqa: ANN001
                    payload = public_http.get_json(
                        adapter_.url, params=params, timeout_sec=timeout_sec
                    )
                    _validate_payload_shape(adapter_, payload)
                    return payload

            result = fetch_venue(adapter, fetch)
            if result.truncated:
                truncated.append(adapter.venue)
            staged_rows[adapter.venue] = list(result.rows)
            per_venue_pages[adapter.venue] = result.pages
        except Exception as exc:  # noqa: BLE001 - one bad venue invalidates the stage
            errors[adapter.venue] = f"{type(exc).__name__}: {exc}"

    observed_at_utc = (
        _writer_refresh_completed_at_utc()
        if payloads is None
        else observed_at_utc or utc_now_iso()
    )
    raw_universe_rows = {
        adapter.venue: len(staged_rows.get(adapter.venue, ()))
        for adapter in ADAPTERS
    }
    for venue in ("okx", "gate"):
        if venue in staged_rows and raw_universe_rows[venue] == 0:
            errors[venue] = (
                "EventRegistryError: empty full-universe response cannot be complete"
            )
    adapters_by_venue = {adapter.venue: adapter for adapter in ADAPTERS}
    for venue, rows in staged_rows.items():
        events = normalise_rows(
            adapters_by_venue[venue], rows, observed_at_utc=observed_at_utc
        )
        per_venue[venue] = len(events)
        observed.extend(events)

    if errors or truncated or set(per_venue) != {adapter.venue for adapter in ADAPTERS}:
        return {
            "schema": REGISTRY_SCHEMA,
            "status": "INCOMPLETE_NO_REGISTRY_WRITE",
            "complete": False,
            "refreshed_at_utc": observed_at_utc,
            "observed_events": len(observed),
            "observed_by_venue": per_venue,
            "pages_by_venue": per_venue_pages,
            "truncated_venues": sorted(truncated),
            "venue_errors": dict(sorted(errors.items())),
            "appended_entries": 0,
            "refresh_evidence_class": refresh_evidence_class,
            "production_eligible": production_eligible,
            RAW_UNIVERSE_ROWS_FIELD: dict(sorted(raw_universe_rows.items())),
        }

    current_active = _active_contract_ids_from_rows(staged_rows)
    lifecycle_contracts = _lifecycle_contract_ids_from_rows(staged_rows)
    lock_path = (
        REGISTRY_LOCK_PATH
        if registry_path.resolve(strict=False) == REGISTRY_PATH.resolve(strict=False)
        else registry_path.with_suffix(".lock")
    )
    with registry_lock(
        lock_path,
        run_id=run_id,
        plan_hash=str(preflight["plan_hash"]),
    ) as lock_owner:
        committed_mutation = _mutation_compare_and_swap_token(registry_path)
        if committed_mutation != staged_from_mutation:
            raise EventRegistryError(
                "STALE_REFRESH_STAGE: compare-and-swap base changed while venue "
                "payloads were staged"
            )
        existing, existing_report = _verify_registry_snapshot(
            registry_path,
            bootstrap_lock_owner=lock_owner,
        )
        if existing_report["status"] != "REGISTRY_OK":
            raise EventRegistryError(
                "existing registry lineage is invalid: "
                + "; ".join(existing_report["problems"])
            )
        previous_universe_rows = _summary_raw_universe_counts(
            registry_path.with_suffix(".summary.json")
        )
        if previous_universe_rows is not None:
            for venue in ("okx", "gate"):
                previous_count = previous_universe_rows[venue]
                current_count = raw_universe_rows[venue]
                if (
                    previous_count > 0
                    and current_count / previous_count
                    < MIN_FULL_UNIVERSE_RETENTION_RATIO
                ):
                    raise EventRegistryError(
                        "INCOMPLETE_UNIVERSE_DROP: "
                        f"{venue} raw rows fell from {previous_count} to "
                        f"{current_count}"
                    )
        previous_active, previous_high_water = _load_lifecycle_generation_state(
            registry_path.with_suffix(".summary.json"),
            existing=existing,
            current_active_for_legacy=current_active,
        )
        active_generations, lifecycle_high_water = _allocate_current_lifecycle_state(
            current_active,
            previous_active=previous_active,
            previous_high_water=previous_high_water,
        )
        observation_generations, lifecycle_high_water = (
            _bind_relevant_lifecycle_generations(
                lifecycle_contracts,
                active_generations=active_generations,
                previous_active=previous_active,
                lifecycle_high_water=lifecycle_high_water,
            )
        )
        bound_observed = _bind_lifecycle_generations(
            observed,
            active_generations=observation_generations,
        )
        appended = build_stream_revisions(existing, bound_observed)
        candidate_problems = _verify_records([*existing, *appended])
        if candidate_problems:
            raise EventRegistryError(
                "candidate registry failed verification before append: "
                + "; ".join(candidate_problems)
            )
        try:
            commit_preflight = risk_gate.preflight(
                write_class="metadata_registry", run_id=run_id
            )
        except Exception as exc:  # noqa: BLE001 - commit must fail closed
            raise EventRegistryError(
                f"PREFLIGHT_BLOCKED_AT_COMMIT: {type(exc).__name__}: {exc}"
            ) from exc
        if not _preflight_is_exact(
            commit_preflight,
            write_class="metadata_registry",
            run_id=run_id,
            decision="ALLOW_METADATA_REGISTRY",
            action=risk_gate.METADATA_REGISTRY_ACTION,
        ) or any(
            commit_preflight.get(field) != preflight.get(field)
            for field in ("plan_id", "plan_hash", "resolved_paths_hash")
        ):
            raise EventRegistryError(
                "PREFLIGHT_BLOCKED_AT_COMMIT: write authority changed during staging"
            )
        mutation_snapshot = _snapshot_mutation_files(registry_path)
        try:
            written = append_entries(appended, registry_path, lock_owner=lock_owner)
            _entries_after, report = _verify_registry_snapshot(
                registry_path,
                verify_summary=False,
                bootstrap_lock_owner=lock_owner,
            )
            if report["status"] != "REGISTRY_OK":
                raise EventRegistryError(
                    "registry failed verification after append: "
                    + "; ".join(report["problems"])
                )
            summary = {
                "schema": REGISTRY_SCHEMA,
                "status": "REFRESH_COMPLETE",
                "mutation_type": "metadata_refresh",
                "mutation_run_id": run_id,
                "refresh_run_id": run_id,
                "plan_id": preflight["plan_id"],
                "plan_hash": preflight["plan_hash"],
                "resolved_paths_hash": preflight["resolved_paths_hash"],
                "refreshed_at_utc": observed_at_utc,
                LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD: observed_at_utc,
                "observed_events": len(observed),
                "observed_by_venue": per_venue,
                "pages_by_venue": per_venue_pages,
                "truncated_venues": [],
                "venue_errors": {},
                "complete": True,
                ACTIVE_CONTRACTS_FIELD: current_active,
                ACTIVE_LIFECYCLE_GENERATIONS_FIELD: active_generations,
                LIFECYCLE_GENERATION_HIGH_WATER_FIELD: lifecycle_high_water,
                "refresh_evidence_class": refresh_evidence_class,
                "production_eligible": production_eligible,
                RAW_UNIVERSE_ROWS_FIELD: dict(sorted(raw_universe_rows.items())),
                "appended_entries": written,
                "new_streams": sum(
                    1 for entry in appended if int(entry["stream_revision"]) == 0
                ),
                "revisions": sum(
                    1 for entry in appended if int(entry["stream_revision"]) > 0
                ),
                "registry": report,
            }
            summary = _write_summary_with_mutation_receipt(
                registry_path,
                summary,
                lock_owner=lock_owner,
            )
            _final_entries, final_report = _verify_registry_snapshot(
                registry_path,
                bootstrap_lock_owner=lock_owner,
            )
            if final_report["status"] != "REGISTRY_OK":
                raise EventRegistryError(
                    "registry transaction failed final verification: "
                    + "; ".join(final_report["problems"])
                )
        except BaseException:
            _rollback_uncommitted_mutation(registry_path, mutation_snapshot)
            raise
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Listing-event registry for pre-market perpetuals.")
    parser.add_argument("--refresh", action="store_true",
                        help="read public instrument metadata and append what changed")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--upcoming", action="store_true")
    parser.add_argument("--horizon-hours", type=int, default=24)
    parser.add_argument("--source-class", choices=SOURCE_CLASSES)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--quarantine-registry", action="store_true",
                        help="move the current registry generation aside, intact, "
                             "when verify names that recovery action")
    parser.add_argument("--reason", default="",
                        help="why this generation is being quarantined")
    parser.add_argument("--payloads", default="",
                        help="JSON file of {venue: payload}; refreshes offline")
    args = parser.parse_args(argv)

    if args.verify:
        report = verify_registry()
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["status"] == "REGISTRY_OK" else 1
    if args.upcoming:
        if args.source_class is None:
            parser.error("--source-class is required with --upcoming")
        report = verify_registry()
        if report["status"] != "REGISTRY_OK":
            raise EventRegistryError(
                "registry verification failed: " + "; ".join(report["problems"])
            )
        upcoming = events_for_capture(
            now_ts=int(time.time()),
            source_class=args.source_class,
            horizon_sec=args.horizon_hours * 3600,
        )
        print(json.dumps({
            "status": "UPCOMING",
            "horizon_hours": args.horizon_hours,
            "count": len(upcoming),
            "events": upcoming,
        }, ensure_ascii=False))
        return 0
    if args.quarantine_registry:
        if not args.run_id or not args.reason:
            raise SystemExit("--quarantine-registry requires --run-id and --reason")
        print(json.dumps(
            quarantine_registry(run_id=args.run_id, reason=args.reason),
            ensure_ascii=False,
        ))
        return 0
    if args.refresh:
        payloads = None
        if args.payloads:
            payloads = json.loads(Path(args.payloads).read_text(encoding="utf-8"))
        run_id = args.run_id or (
            "metadata_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )
        print(json.dumps(refresh(payloads=payloads, run_id=run_id), ensure_ascii=False))
        return 0
    raise SystemExit("no action requested")


if __name__ == "__main__":
    raise SystemExit(main())
