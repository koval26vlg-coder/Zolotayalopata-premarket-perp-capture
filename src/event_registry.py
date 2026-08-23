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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import project_config as config
import public_http
from canonical_hash import canonical_hash


REGISTRY_SCHEMA = "premarket_perp_event_registry_v1"
REGISTRY_PATH = config.PROJECT_ROOT / "docs/registry/listing-events.jsonl"
REGISTRY_SUMMARY_PATH = config.PROJECT_ROOT / "docs/registry/listing-events.summary.json"

# How a t0 was learned. These are never mixed inside one analysis, and the ordering
# here is deliberate: metadata is what we have, an announcement is what we would prefer.
SOURCE_OFFICIAL_ANNOUNCEMENT = "OFFICIAL_ANNOUNCEMENT"
SOURCE_VENUE_INSTRUMENT_METADATA = "VENUE_INSTRUMENT_METADATA"
SOURCE_CLASSES = (SOURCE_OFFICIAL_ANNOUNCEMENT, SOURCE_VENUE_INSTRUMENT_METADATA)


class EventRegistryError(RuntimeError):
    pass


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
        params={"category": "linear"},
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
        params={"instType": "SWAP"},
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


def event_id(venue: str, symbol: str) -> str:
    return f"{venue}:{symbol}"


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
        if pages >= adapter.max_pages:
            return VenueFetch(rows=rows, pages=pages, truncated=True)


def normalise_rows(
    adapter: VenueAdapter, rows: Sequence[Mapping[str, Any]], *, observed_at_utc: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get(adapter.symbol_field) or "").strip()
        if not symbol:
            continue
        t0_ts = _to_seconds(row.get(adapter.t0_field), adapter.t0_unit)
        if t0_ts is None:
            # A missing launch time is not a zero: the event is simply not usable yet.
            continue
        events.append({
            "event_id": event_id(adapter.venue, symbol),
            "venue": adapter.venue,
            "symbol": symbol,
            "t0_ts": t0_ts,
            "t0_source_class": SOURCE_VENUE_INSTRUMENT_METADATA,
            "t0_source_field": adapter.t0_field,
            "t0_semantics": adapter.t0_semantics,
            "t0_precision_sec": adapter.t0_precision_sec,
            "caveats": list(adapter.caveats),
            "source_url": adapter.url,
            "observed_at_utc": observed_at_utc,
        })
    events.sort(key=lambda item: (item["t0_ts"], item["venue"], item["symbol"]))
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


def chain_key(entry: Mapping[str, Any]) -> tuple[str, str]:
    """What a revision chain belongs to: an event AS SEEN BY one source class.

    Keying on event_id alone put an announcement-derived t0 and a metadata-derived t0
    for the same instrument into one chain, where each overwrote the other and the
    revision numbers interleaved. The classes are meant never to mix; sharing an
    identity was the one place they did."""
    return (str(entry.get("event_id")), str(entry.get("t0_source_class")))


def latest_by_event(
    entries: Iterable[Mapping[str, Any]], *, source_class: str | None = None
) -> dict[str, dict[str, Any]]:
    """The current view: the most recent revision of each event, within one class.

    Passing source_class is how a caller gets a view it can reason about. Without it
    every class is returned, keyed by event id alone, and two classes for the same
    instrument would collapse into whichever came last - so that form is only for
    reporting, never for choosing what to capture."""
    current: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if source_class is not None and entry.get("t0_source_class") != source_class:
            continue
        current[str(entry.get("event_id"))] = dict(entry)
    return current


def merge_observations(
    existing: Sequence[Mapping[str, Any]],
    observed: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Append what is new or changed; never rewrite what was recorded before.

    A venue that moves a launch time produces a new revision carrying the previous
    value. Overwriting instead would leave a capture pointing at a t0 that no longer
    exists anywhere, with nothing to show it had moved."""
    current = latest_by_event(existing)
    revision_of: dict[str, int] = {
        key: int(entry.get("revision") or 0) for key, entry in current.items()
    }
    appended: list[dict[str, Any]] = []
    for event in observed:
        key = str(event["event_id"])
        previous = current.get(key)
        if previous is None:
            entry = dict(event)
            entry["revision"] = 0
            appended.append(entry)
            continue
        same = (
            int(previous.get("t0_ts") or 0) == int(event["t0_ts"])
            and previous.get("t0_source_class") == event["t0_source_class"]
            and previous.get("t0_source_field") == event["t0_source_field"]
        )
        if same:
            continue
        entry = dict(event)
        entry["revision"] = revision_of.get(key, 0) + 1
        entry["supersedes"] = {
            "t0_ts": previous.get("t0_ts"),
            "t0_source_class": previous.get("t0_source_class"),
            "observed_at_utc": previous.get("observed_at_utc"),
        }
        appended.append(entry)
    return appended


def _entry_hash(entry: Mapping[str, Any]) -> str:
    return canonical_hash({k: v for k, v in entry.items() if k != "entry_hash"})


def append_entries(entries: Sequence[Mapping[str, Any]], path: Path | None = None) -> int:
    path = path or REGISTRY_PATH
    if not entries:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            stamped = dict(entry)
            stamped["entry_hash"] = _entry_hash(stamped)
            handle.write(json.dumps(stamped, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return len(entries)


def verify_registry(path: Path | None = None) -> dict[str, Any]:
    """Every line must still hash to what it claims, and revisions must be ordered."""
    path = path or REGISTRY_PATH
    entries = load_registry(path)
    problems: list[str] = []
    seen_revision: dict[tuple[str, str], int] = {}
    seen_t0: dict[tuple[str, str], int] = {}
    for number, entry in enumerate(entries, 1):
        if entry.get("entry_hash") != _entry_hash(entry):
            problems.append(f"line {number}: entry_hash does not match its content")
        key = chain_key(entry)
        revision = int(entry.get("revision") or 0)
        if key not in seen_revision:
            # A chain that starts at 7 is a chain with six missing revisions. Accepting
            # it meant a truncated history verified exactly like a complete one.
            if revision != 0:
                problems.append(
                    f"line {number}: {key[0]} first appears at revision {revision}; "
                    f"a chain with no history must start at 0"
                )
        elif revision != seen_revision[key] + 1:
            problems.append(
                f"line {number}: {key[0]} revision {revision} does not follow "
                f"{seen_revision[key]}"
            )
        # supersedes is the whole point of appending instead of overwriting: it must
        # name the value that was actually there, not merely be present.
        superseded = entry.get("supersedes")
        if revision == 0:
            if superseded is not None:
                problems.append(f"line {number}: revision 0 cannot supersede anything")
        elif key in seen_t0 and superseded != seen_t0[key]:
            problems.append(
                f"line {number}: supersedes says {superseded!r} but the previous "
                f"revision held {seen_t0[key]!r}"
            )
        seen_revision[key] = revision
        if entry.get("t0_ts") is not None:
            seen_t0[key] = int(entry["t0_ts"])
        if entry.get("t0_source_class") not in SOURCE_CLASSES:
            problems.append(f"line {number}: unknown t0_source_class")
    current = latest_by_event(entries)
    return {
        "status": "REGISTRY_OK" if not problems else "REGISTRY_PROBLEMS",
        "entries": len(entries),
        "events": len(current),
        "problems": problems,
        "by_source_class": _count(current.values(), "t0_source_class"),
        "by_venue": _count(current.values(), "venue"),
    }


def _count(entries: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        value = str(entry.get(key))
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def events_for_capture(
    entries: Sequence[Mapping[str, Any]],
    *,
    now_ts: int,
    source_class: str,
    horizon_sec: int = 24 * 3600,
) -> list[dict[str, Any]]:
    """Events whose t0 is still ahead, within one source class only.

    The source class is a required argument rather than a filter applied afterwards:
    mixing an announcement-derived t0 with a metadata-derived one in the same capture
    set would reintroduce exactly the defect this registry exists to avoid. It carried
    a default until an audit pointed out that a documented requirement with a default
    is a suggestion, and the CLI was quietly taking the default."""
    if source_class not in SOURCE_CLASSES:
        raise EventRegistryError(f"unknown source class: {source_class}")
    upcoming = [
        entry for entry in latest_by_event(entries, source_class=source_class).values()
        if entry.get("t0_source_class") == source_class
        and now_ts <= int(entry.get("t0_ts") or 0) <= now_ts + horizon_sec
    ]
    upcoming.sort(key=lambda item: (item["t0_ts"], item["venue"], item["symbol"]))
    return upcoming


def enforce_metadata_write_class() -> dict[str, Any]:
    """Run the checks WRITE_CLASSES["metadata_registry"] says this write requires.

    They were declared in the plan and enforced nowhere: a refresh wrote to the
    registry with no plan verification and no capability scan, which made the write
    class a description of intent rather than a gate. The class asks for the plan and
    the capability scan (not the exclusive claim, which belongs to a capture); this
    runs exactly that, so the declaration and the behaviour cannot drift apart."""
    import risk_gate  # local: keeps the module importable without the gate for tooling

    rules = config.WRITE_CLASSES["metadata_registry"]
    receipt: dict[str, Any] = {"write_class": "metadata_registry"}
    if rules.get("plan_and_capability_scan"):
        receipt["plan_hash"] = risk_gate.load_and_verify_plan()["plan_hash"]
        receipt["capability_scan"] = risk_gate.run_capability_scan()["status"]
    return receipt


def refresh(
    *,
    payloads: Mapping[str, Any] | None = None,
    path: Path | None = None,
    observed_at_utc: str | None = None,
    timeout_sec: int = 20,
) -> dict[str, Any]:
    """Read each venue's public instrument metadata and append what changed.

    `payloads` supplies responses directly, so tests and CI exercise the whole path
    without touching the network."""
    preflight = enforce_metadata_write_class()
    observed_at_utc = observed_at_utc or utc_now_iso()
    observed: list[dict[str, Any]] = []
    per_venue: dict[str, int] = {}
    per_venue_pages: dict[str, int] = {}
    truncated: list[str] = []

    for adapter in ADAPTERS:
        if payloads is not None:
            if adapter.venue not in payloads:
                continue
            # A venue may be supplied as one payload or as a list of pages.
            supplied = payloads[adapter.venue]
            pages = list(supplied) if isinstance(supplied, list) and adapter.cursor_param \
                else [supplied]
            queue = list(pages)

            def fetch(_adapter, _params, queue=queue):  # noqa: ANN001
                return queue.pop(0) if queue else {}
        else:
            def fetch(adapter_, params):  # noqa: ANN001
                return public_http.get_json(
                    adapter_.url, params=params, timeout_sec=timeout_sec
                )

        result = fetch_venue(adapter, fetch)
        if result.truncated:
            truncated.append(adapter.venue)
        events = normalise_rows(adapter, result.rows, observed_at_utc=observed_at_utc)
        per_venue[adapter.venue] = len(events)
        per_venue_pages[adapter.venue] = result.pages
        observed.extend(events)

    existing = load_registry(path)
    appended = merge_observations(existing, observed)
    written = append_entries(appended, path)
    summary = {
        "schema": REGISTRY_SCHEMA,
        "preflight": preflight,
        "refreshed_at_utc": observed_at_utc,
        "observed_events": len(observed),
        "observed_by_venue": per_venue,
        "pages_by_venue": per_venue_pages,
        # Never silent: a venue whose cursor was still live when the page cap hit
        # produced a partial universe, and a partial universe that looks complete is
        # how a capture ends up missing the listing it was built for.
        "truncated_venues": truncated,
        "complete": not truncated,
        "appended_entries": written,
        "new_events": sum(1 for entry in appended if int(entry.get("revision") or 0) == 0),
        "revisions": sum(1 for entry in appended if int(entry.get("revision") or 0) > 0),
        "registry": verify_registry(path),
    }
    summary_path = (path or REGISTRY_PATH).with_suffix(".summary.json")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Listing-event registry for pre-market perpetuals.")
    parser.add_argument("--refresh", action="store_true",
                        help="read public instrument metadata and append what changed")
    parser.add_argument("--verify", action="store_true")
    # No default: which class of t0 a capture set is drawn from is the caller's
    # decision to state, and the CLI used to take it silently.
    parser.add_argument("--source-class", choices=sorted(SOURCE_CLASSES),
                        help="required with --upcoming: which t0 source class to draw from")
    parser.add_argument("--upcoming", action="store_true")
    parser.add_argument("--horizon-hours", type=int, default=24)
    parser.add_argument("--payloads", default="",
                        help="JSON file of {venue: payload}; refreshes offline")
    args = parser.parse_args(argv)

    if args.verify:
        report = verify_registry()
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["status"] == "REGISTRY_OK" else 1
    if args.upcoming and not args.source_class:
        raise SystemExit("--upcoming requires --source-class: see docs/decisions/002")
    if args.upcoming:
        upcoming = events_for_capture(
            load_registry(), now_ts=int(time.time()),
            source_class=args.source_class,
            horizon_sec=args.horizon_hours * 3600,
        )
        print(json.dumps({
            "status": "UPCOMING",
            "t0_source_class": args.source_class,
            "horizon_hours": args.horizon_hours,
            "count": len(upcoming),
            "events": upcoming,
        }, ensure_ascii=False))
        return 0
    if args.refresh:
        payloads = None
        if args.payloads:
            payloads = json.loads(Path(args.payloads).read_text(encoding="utf-8"))
        print(json.dumps(refresh(payloads=payloads), ensure_ascii=False))
        return 0
    raise SystemExit("no action requested")


if __name__ == "__main__":
    raise SystemExit(main())
