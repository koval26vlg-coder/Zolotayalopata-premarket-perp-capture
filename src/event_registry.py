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
import secrets
import socket
import time
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


REGISTRY_SCHEMA = "premarket_perp_event_registry_v2"
REGISTRY_V1_PATH = config.PROJECT_ROOT / "docs/registry/listing-events.jsonl"
REGISTRY_PATH = config.PROJECT_ROOT / "docs/registry/listing-events-v2.jsonl"
REGISTRY_SUMMARY_PATH = config.PROJECT_ROOT / "docs/registry/listing-events-v2.summary.json"
REGISTRY_LOCK_PATH = config.PROJECT_ROOT / "docs/registry/listing-events-v2.lock"

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
        t0_precision_sec=1,
        rows_path=("result", "list"),
        cursor_path=("result", "nextPageCursor"),
        cursor_param="cursor",
    ),
    VenueAdapter(
        venue="okx",
        url="https://www.okx.com/api/v5/public/instruments",
        params={"instType": "FUTURES"},
        symbol_field="instId",
        t0_field="listTime",
        t0_unit="ms",
        t0_semantics="venue-declared instrument listing time",
        t0_precision_sec=1,
        rows_path=("data",),
    ),
    VenueAdapter(
        venue="gate",
        url="https://api.gateio.ws/api/v4/futures/usdt/contracts",
        params={},
        symbol_field="name",
        t0_field="create_time",
        t0_unit="s",
        # Gate publishes when the contract object was created, which is not necessarily
        # when trading opened. Recording that here is the difference between a caveat
        # a reader can act on and a number that quietly means something else.
        t0_semantics="contract creation time, not necessarily trading start",
        t0_precision_sec=60,
        caveats=("CONTRACT_CREATION_NOT_TRADING_START",),
        rows_path=(),
    ),
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
    """Require the venue's public pre-market discriminator, not symbol guesswork."""
    if adapter.venue == "bybit":
        return (
            str(row.get("status") or "") == "PreLaunch"
            and row.get("isPreListing") is True
            and str(row.get("contractType") or "") == "LinearPerpetual"
        )
    if adapter.venue == "okx":
        rule_type = str(row.get("ruleType") or "")
        return str(row.get("instType") or "") == "FUTURES" and (
            rule_type == "pre_market"
            or (
                rule_type == "xperp"
                and _to_seconds(row.get("preMktSwTime"), "ms") is not None
            )
        )
    if adapter.venue == "gate":
        return str(row.get("status") or "").lower() == "prelaunch"
    return False


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
    adapter: VenueAdapter, rows: Sequence[Mapping[str, Any]], *, observed_at_utc: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
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
        is_contract_creation = adapter.venue == "gate"
        timestamp_kind = (
            TIMESTAMP_CONTRACT_CREATED
            if is_contract_creation
            else TIMESTAMP_PREMARKET_CONTRACT_LAUNCH
        )
        observation = make_timestamp_observation(
            episode_id=make_episode_id(adapter.venue, symbol),
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
                    source_class=SOURCE_OBSERVED_LIFECYCLE,
                    source_identity="okx:instrument_metadata:preMktSwTime",
                    source_url=adapter.url,
                    received_at_utc=observed_at_utc,
                    precision_sec=1,
                    caveats=("VENUE_METADATA_TRANSITION_TIMESTAMP",),
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


def load_registry(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or REGISTRY_PATH
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
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
    return canonical_hash({k: v for k, v in observation.items() if k not in ignored})


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


def _verify_records(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    problems: list[str] = []
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
        timestamp_kind = str(entry.get("timestamp_kind") or "")
        if timestamp_kind not in TIMESTAMP_KINDS:
            problems.append(f"line {number}: unknown timestamp_kind")
        if entry.get("source_class") not in SOURCE_CLASSES:
            problems.append(f"line {number}: unknown source_class")
        if entry.get("instrument_role") not in INSTRUMENT_ROLES:
            problems.append(f"line {number}: unknown instrument_role")

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
    entries = load_registry(path)
    problems = _verify_records(entries)
    production_registry = (
        path.resolve(strict=False) == REGISTRY_PATH.resolve(strict=False)
    )
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
        summary_path = path.with_suffix(".summary.json")
        if summary_path.is_file():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                if not isinstance(summary, Mapping):
                    raise ValueError("summary root is not an object")
                summary_problem_count = len(problems)
                receipt = summary.get("registry")
                if not isinstance(receipt, Mapping):
                    raise ValueError("summary has no registry receipt")
                if production_registry:
                    if summary.get("schema") != REGISTRY_SCHEMA:
                        problems.append("summary schema does not match registry")
                    if summary.get("status") != "REFRESH_COMPLETE":
                        problems.append("summary status is not REFRESH_COMPLETE")
                    if summary.get("complete") is not True:
                        problems.append("summary complete flag is not true")
                    if summary.get("plan_hash") != trust_root.PLAN_HASH:
                        problems.append("summary plan_hash is not the active PlanOnly")
                    if not str(summary.get("refresh_run_id") or "").strip():
                        problems.append("summary refresh_run_id is missing")
                    resolved_paths_hash = str(summary.get("resolved_paths_hash") or "")
                    if len(resolved_paths_hash) != 64 or any(
                        character not in "0123456789abcdef"
                        for character in resolved_paths_hash
                    ):
                        problems.append("summary resolved_paths_hash is invalid")
                    if receipt.get("status") != "REGISTRY_OK":
                        problems.append("summary registry status is not REGISTRY_OK")
                if int(receipt.get("entries", -1)) != len(entries):
                    problems.append("summary entry count does not match registry")
                actual_head = entries[-1].get("record_hash") if entries else None
                if receipt.get("head_record_hash") != actual_head:
                    problems.append("summary head_record_hash does not match registry")
                summary_verified = len(problems) == summary_problem_count
            except (OSError, ValueError, TypeError) as exc:
                problems.append(f"summary receipt is unreadable: {type(exc).__name__}: {exc}")
        elif summary_required:
            problems.append("required summary receipt is missing for nonempty production registry")
    episodes = materialize_episodes(entries)
    heads = [head[2] for head in _stream_heads(entries).values()]
    report = {
        "status": "REGISTRY_OK" if not problems else "REGISTRY_PROBLEMS",
        "entries": len(entries),
        "events": len(episodes),
        "episodes": len(episodes),
        "head_record_hash": entries[-1].get("record_hash") if entries else None,
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
    episodes = materialize_episodes(entries)
    conflicts = [
        str(episode.get("episode_id"))
        for episode in episodes
        if episode.get("official_conflict") is True
    ]
    if conflicts:
        raise EventRegistryError(
            "OFFICIAL_CONFLICT: conflicting official_spot_t0 heads for "
            + ", ".join(sorted(conflicts))
        )
    upcoming = [
        entry for entry in episodes
        if entry.get("t0_source_class") == source_class
        and entry.get("capture_eligible") is True
        and entry.get("evidence_use") == "ACCEPTANCE_ANCHOR"
        and now_ts <= int(entry.get("official_spot_t0") or 0) <= now_ts + horizon_sec
    ]
    upcoming.sort(
        key=lambda item: (item["official_spot_t0"], item["venue"], item["symbol"])
    )
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
    if not (
        isinstance(preflight, Mapping)
        and preflight.get("schema") == risk_gate.PREFLIGHT_RESULT_SCHEMA
        and preflight.get("ok") is True
        and preflight.get("verified") is True
        and preflight.get("decision") == "ALLOW_METADATA_REGISTRY"
        and preflight.get("write_class") == "metadata_registry"
        and preflight.get("run_id") == run_id
        and str(preflight.get("plan_id") or "").strip() != ""
        and len(str(preflight.get("plan_hash") or "")) == 64
        and len(str(preflight.get("resolved_paths_hash") or "")) == 64
    ):
        raise EventRegistryError("PREFLIGHT_BLOCKED: metadata preflight is not verified")

    observed_at_utc = observed_at_utc or utc_now_iso()
    observed: list[dict[str, Any]] = []
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
            events = normalise_rows(
                adapter, result.rows, observed_at_utc=observed_at_utc
            )
            per_venue[adapter.venue] = len(events)
            per_venue_pages[adapter.venue] = result.pages
            observed.extend(events)
        except Exception as exc:  # noqa: BLE001 - one bad venue invalidates the stage
            errors[adapter.venue] = f"{type(exc).__name__}: {exc}"

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
        }

    registry_path = path or REGISTRY_PATH
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
        existing = load_registry(registry_path)
        existing_report = verify_registry(registry_path)
        if existing_report["status"] != "REGISTRY_OK":
            raise EventRegistryError(
                "existing registry lineage is invalid: "
                + "; ".join(existing_report["problems"])
            )
        appended = build_stream_revisions(existing, observed)
        written = append_entries(appended, registry_path, lock_owner=lock_owner)
        report = verify_registry(
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
            "refresh_run_id": run_id,
            "plan_hash": preflight["plan_hash"],
            "resolved_paths_hash": preflight["resolved_paths_hash"],
            "refreshed_at_utc": observed_at_utc,
            "observed_events": len(observed),
            "observed_by_venue": per_venue,
            "pages_by_venue": per_venue_pages,
            "truncated_venues": [],
            "venue_errors": {},
            "complete": True,
            "appended_entries": written,
            "new_streams": sum(
                1 for entry in appended if int(entry["stream_revision"]) == 0
            ),
            "revisions": sum(
                1 for entry in appended if int(entry["stream_revision"]) > 0
            ),
            "registry": report,
        }
        _write_json_atomic(registry_path.with_suffix(".summary.json"), summary)
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
