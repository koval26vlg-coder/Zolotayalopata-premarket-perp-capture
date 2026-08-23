"""Offline replay of the exit hypothesis over captured public data.

The hypothesis is: be long before the listing, exit at t0, +5s, +15s or +60s. This
module answers it from bytes already on disk. It opens no socket, takes no claim, and
belongs to none of the three write classes.

The one decision that shapes everything here: **between two samples the price was not
observed.** A REST poll returns the book at the instants we asked, so a horizon that
falls between two snapshots has a range of admissible prices, not a value. Reporting a
single number - the nearest sample, an interpolation, the last trade - would turn the
gap into a fact, and the gap is precisely what a 0.5-second cadence cannot close.

So every price here is an interval, every return is an interval, and each one carries
the observation gap that produced it. An interval wide enough to contain both profit
and loss is the honest answer when the data cannot distinguish them.

Two further deliberate limits:

* Exits are priced against the **bid**, because selling a long means hitting a bid, and
  entries against the **ask**. A mid price is not a price anyone trades at.
* Only the top of book is used, and the visible size is reported beside it. Any deeper
  fill assumption would be a model of an order that was never placed.

Nothing here produces an ACCEPT or a REJECT. The risk contract records
acceptance_decision as NONE_CAPTURE_ONLY, and a replay is a description.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import project_config as config


REPLAY_SCHEMA = "premarket_perp_replay_v1"
DEFAULT_HORIZONS_SEC = (0, 5, 15, 60)

# How far a bracketing sample may sit from a horizon before the answer stops being an
# observation and becomes a guess with error bars. The burst cadence for the book is
# 0.5s, so this is four samples: comfortably loose, and loud when exceeded.
BRACKET_TOLERANCE_SEC = 2.0

# How long before t0 the entry is priced. The capture window opens 30 minutes early;
# the entry is taken from the last book strictly before t0 within this lead.
DEFAULT_ENTRY_LEAD_SEC = 60


class ReplayError(RuntimeError):
    pass


# ------------------------------------------------------------------ reading a capture


def load_capture(capture_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read a capture, refusing bytes its own manifest does not vouch for."""
    manifest_path = capture_dir / "manifest.json"
    samples_path = capture_dir / "samples.jsonl"
    if not manifest_path.is_file():
        raise ReplayError(f"no manifest in {capture_dir}")
    if not samples_path.is_file():
        raise ReplayError(f"no samples in {capture_dir}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw = samples_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    recorded = str(manifest.get("output_sha256") or "")
    if digest != recorded:
        raise ReplayError(
            "samples do not match the manifest that authenticates them: "
            f"file {digest[:16]}, manifest {recorded[:16]}"
        )

    samples = [
        json.loads(line)
        for line in raw.decode("utf-8").splitlines()
        if line.strip()
    ]
    return manifest, samples


# --------------------------------------------------------------------- book extraction


def _as_pair(row: Any) -> tuple[float, float] | None:
    """A depth level, in whichever shape the venue writes it."""
    if isinstance(row, Mapping):
        price, size = row.get("p"), row.get("s")
    elif isinstance(row, (list, tuple)) and len(row) >= 2:
        price, size = row[0], row[1]
    else:
        return None
    try:
        return float(price), float(size)
    except (TypeError, ValueError):
        return None


def top_of_book(venue: str, payload: Any) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Best bid and best ask, each as (price, visible size)."""
    if venue == "bybit":
        result = (payload or {}).get("result") if isinstance(payload, Mapping) else None
        bids, asks = (result or {}).get("b"), (result or {}).get("a")
    elif venue == "okx":
        rows = (payload or {}).get("data") if isinstance(payload, Mapping) else None
        row = rows[0] if isinstance(rows, list) and rows else {}
        bids, asks = row.get("bids"), row.get("asks")
    elif venue == "gate":
        bids = payload.get("bids") if isinstance(payload, Mapping) else None
        asks = payload.get("asks") if isinstance(payload, Mapping) else None
    else:
        raise ReplayError(f"no book layout known for venue: {venue}")

    if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
        return None
    best_bid, best_ask = _as_pair(bids[0]), _as_pair(asks[0])
    if best_bid is None or best_ask is None:
        return None
    if best_bid[0] <= 0 or best_ask[0] <= 0 or best_ask[0] < best_bid[0]:
        # A crossed or nonsensical book is not a price; dropping it silently would let
        # it set a bound.
        return None
    return best_bid, best_ask


@dataclass(frozen=True)
class BookSample:
    exchange_ts: float
    bid_px: float
    bid_sz: float
    ask_px: float
    ask_sz: float


def book_series(samples: Iterable[Mapping[str, Any]], venue: str) -> list[BookSample]:
    """Every usable orderbook snapshot, ordered by the venue's own clock.

    The venue timestamp is the index, not our request time: it says when the market was
    in this state, while our clock only says when we asked."""
    series: list[BookSample] = []
    for sample in samples:
        if sample.get("probe") != "orderbook" or sample.get("error"):
            continue
        exchange_ts = sample.get("exchange_ts")
        if exchange_ts is None:
            continue
        book = top_of_book(venue, sample.get("payload"))
        if book is None:
            continue
        (bid_px, bid_sz), (ask_px, ask_sz) = book
        series.append(
            BookSample(float(exchange_ts), bid_px, bid_sz, ask_px, ask_sz)
        )
    series.sort(key=lambda item: item.exchange_ts)
    return series


# ------------------------------------------------------------------------- bracketing


@dataclass(frozen=True)
class Bracket:
    before: BookSample | None
    after: BookSample | None
    gap_before_sec: float | None
    gap_after_sec: float | None

    @property
    def straddled(self) -> bool:
        return self.before is not None and self.after is not None

    @property
    def tight(self) -> bool:
        return (
            self.straddled
            and (self.gap_before_sec or 0) <= BRACKET_TOLERANCE_SEC
            and (self.gap_after_sec or 0) <= BRACKET_TOLERANCE_SEC
        )


def bracket_at(series: Sequence[BookSample], at_ts: float) -> Bracket:
    """The last sample at or before the instant, and the first at or after it."""
    stamps = [item.exchange_ts for item in series]
    index = bisect.bisect_right(stamps, at_ts)
    before = series[index - 1] if index > 0 else None
    after = series[index] if index < len(series) else None
    if after is not None and before is not None and after.exchange_ts == before.exchange_ts:
        after = series[index] if index < len(series) else None
    return Bracket(
        before=before,
        after=after,
        gap_before_sec=round(at_ts - before.exchange_ts, 3) if before else None,
        gap_after_sec=round(after.exchange_ts - at_ts, 3) if after else None,
    )


@dataclass(frozen=True)
class PriceBound:
    low: float | None
    high: float | None
    observed: bool
    note: str = ""
    gap_before_sec: float | None = None
    gap_after_sec: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "low": self.low,
            "high": self.high,
            "observed": self.observed,
            "note": self.note,
            "gap_before_sec": self.gap_before_sec,
            "gap_after_sec": self.gap_after_sec,
        }


def bound_from_bracket(bracket: Bracket, side: str) -> PriceBound:
    """The admissible range for a price at an instant nobody sampled.

    With a sample on each side the price is bounded by the two observations - not
    because the market moved monotonically between them, but because those are the only
    two prices anyone actually saw. With a sample on one side only, the bound is open
    and says so."""
    pick = (lambda s: s.bid_px) if side == "bid" else (lambda s: s.ask_px)
    if bracket.straddled:
        values = [pick(bracket.before), pick(bracket.after)]
        return PriceBound(
            low=min(values),
            high=max(values),
            observed=True,
            note="bounded by the samples either side" if not bracket.tight
            else "bracketed within tolerance",
            gap_before_sec=bracket.gap_before_sec,
            gap_after_sec=bracket.gap_after_sec,
        )
    if bracket.before is not None:
        return PriceBound(
            low=None, high=None, observed=False,
            note="capture ended before this horizon; nothing was sampled after it",
            gap_before_sec=bracket.gap_before_sec,
        )
    if bracket.after is not None:
        return PriceBound(
            low=None, high=None, observed=False,
            note="capture began after this horizon; nothing was sampled before it",
            gap_after_sec=bracket.gap_after_sec,
        )
    return PriceBound(low=None, high=None, observed=False,
                      note="no usable orderbook samples at all")


# ----------------------------------------------------------------------------- replay


def bounded_return(entry: PriceBound, exit_: PriceBound) -> dict[str, Any]:
    """The return interval for a long, worst case against best case.

    Worst is the lowest exit against the highest entry, best the reverse. Reporting a
    midpoint would invent a number the data never contained."""
    if not entry.observed or not exit_.observed:
        return {"low": None, "high": None, "computable": False}
    if not entry.high or entry.high <= 0 or not entry.low or entry.low <= 0:
        return {"low": None, "high": None, "computable": False}
    return {
        "low": round(exit_.low / entry.high - 1.0, 6),
        "high": round(exit_.high / entry.low - 1.0, 6),
        "computable": True,
    }


def replay_capture(
    capture_dir: Path,
    *,
    horizons_sec: Sequence[int] = DEFAULT_HORIZONS_SEC,
    entry_lead_sec: int = DEFAULT_ENTRY_LEAD_SEC,
) -> dict[str, Any]:
    manifest, samples = load_capture(capture_dir)
    venue = str(manifest.get("venue") or "")
    t0_ts = float(manifest.get("t0_ts") or 0)
    if not venue or not t0_ts:
        raise ReplayError("manifest carries no venue or t0")

    series = book_series(samples, venue)
    entry_bracket = bracket_at(series, t0_ts - entry_lead_sec)
    entry = bound_from_bracket(entry_bracket, "ask")

    horizons: list[dict[str, Any]] = []
    for offset in horizons_sec:
        at_ts = t0_ts + offset
        exit_bracket = bracket_at(series, at_ts)
        exit_bound = bound_from_bracket(exit_bracket, "bid")
        horizons.append({
            "offset_sec": offset,
            "exit_price": exit_bound.as_dict(),
            "return": bounded_return(entry, exit_bound),
            "well_observed": exit_bracket.tight,
            "visible_size_at_exit": (
                min(exit_bracket.before.bid_sz, exit_bracket.after.bid_sz)
                if exit_bracket.straddled else None
            ),
        })

    computable = [h for h in horizons if h["return"]["computable"]]
    return {
        "schema": REPLAY_SCHEMA,
        "capture_id": manifest.get("capture_id"),
        "venue": venue,
        "symbol": manifest.get("symbol"),
        "t0_ts": int(t0_ts),
        "t0_source_class": manifest.get("t0_source_class"),
        "book_samples_used": len(series),
        "entry": {
            "priced_at_sec_before_t0": entry_lead_sec,
            "side": "ask",
            "price": entry.as_dict(),
            "visible_size": (
                min(entry_bracket.before.ask_sz, entry_bracket.after.ask_sz)
                if entry_bracket.straddled else None
            ),
        },
        "horizons": horizons,
        "horizons_computable": len(computable),
        "horizons_requested": len(horizons),
        "method": (
            "prices are intervals bounded by the samples either side of each instant; "
            "exits priced against the bid and entries against the ask, top of book "
            "only, with visible size reported beside them"
        ),
        "acceptance_decision": "NONE_REPLAY_IS_DESCRIPTIVE",
        "capture_replay_readiness": manifest.get("replay_readiness"),
    }


def format_report(report: Mapping[str, Any]) -> str:
    lines = [
        f"replay {report['capture_id']}  {report['venue']} {report['symbol']}",
        f"  book samples used: {report['book_samples_used']}",
    ]
    entry = report["entry"]["price"]
    if entry["observed"]:
        lines.append(
            f"  entry (ask, {report['entry']['priced_at_sec_before_t0']}s before t0): "
            f"{entry['low']} .. {entry['high']}"
        )
    else:
        lines.append(f"  entry: not observed - {entry['note']}")
    for horizon in report["horizons"]:
        returns = horizon["return"]
        if returns["computable"]:
            flag = "" if horizon["well_observed"] else "   [gap wider than tolerance]"
            lines.append(
                f"  t0+{horizon['offset_sec']:>3}s  return {returns['low']:+.4%} .. "
                f"{returns['high']:+.4%}{flag}"
            )
        else:
            lines.append(
                f"  t0+{horizon['offset_sec']:>3}s  not computable - "
                f"{horizon['exit_price']['note']}"
            )
    lines.append("  no acceptance decision is produced from this")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay the exit hypothesis over a capture already on disk.",
    )
    parser.add_argument("--replay", metavar="CAPTURE_DIR", default="")
    parser.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS_SEC))
    parser.add_argument("--entry-lead-sec", type=int, default=DEFAULT_ENTRY_LEAD_SEC)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--list", action="store_true",
                        help="list captures available to replay")
    args = parser.parse_args(argv)

    if args.list:
        root = config.CAPTURE_ROOT
        captures = sorted(p.name for p in root.glob("*/") if (p / "manifest.json").is_file()) \
            if root.is_dir() else []
        print(json.dumps({"capture_root": str(root), "captures": captures},
                         ensure_ascii=False))
        return 0

    if not args.replay:
        raise SystemExit("no action requested")
    horizons = tuple(int(part) for part in args.horizons.split(",") if part.strip())
    report = replay_capture(
        Path(args.replay), horizons_sec=horizons, entry_lead_sec=args.entry_lead_sec
    )
    print(json.dumps(report, ensure_ascii=False) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
