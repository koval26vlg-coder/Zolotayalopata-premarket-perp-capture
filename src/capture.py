"""One bounded, visible capture around a single listing t0.

What this is, stated plainly because it decides whether the replay means anything:
**a poll, not a tape.** REST endpoints answer at the instants we ask. Between two
samples the market did things we did not see, and for a hypothesis about +5, +15 and
+60 seconds that gap is the whole question. So the cadence is declared up front, the
achieved cadence is measured, and the gaps are published in the manifest. A capture
that called itself continuous while sampling twice a second would be the same kind of
claim the spot monitor's audit spent its length taking apart.

Everything the audit taught is carried over rather than rediscovered: rows are fsynced
before the manifest that authenticates them is written, the manifest carries
output_sha256, the deadline and the stop request are checked inside the loop rather
than between events, nothing is truncated silently, and the run ends with a committed
evidence receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import event_registry as registry
import project_config as config
import public_http
import risk_gate
from canonical_hash import canonical_hash
from global_market_writer_claim import (
    claim_global_market_writer,
    release_global_market_writer,
)


CAPTURE_SCHEMA = "premarket_perp_capture_v1"
SAMPLE_SCHEMA = "premarket_perp_capture_sample_v1"
PROBES = ("trades", "orderbook", "ticker")


class CaptureError(RuntimeError):
    pass


# --------------------------------------------------------------------- venue probes


@dataclass(frozen=True)
class Probe:
    venue: str
    probe: str
    url: str
    params_for: Callable[[str], dict[str, Any]]


def _bybit_symbol(symbol: str) -> str:
    return symbol.upper()


PROBE_TABLE: tuple[Probe, ...] = (
    Probe("bybit", "trades", "https://api.bybit.com/v5/market/recent-trade",
          lambda s: {"category": "linear", "symbol": _bybit_symbol(s), "limit": 200}),
    Probe("bybit", "orderbook", "https://api.bybit.com/v5/market/orderbook",
          lambda s: {"category": "linear", "symbol": _bybit_symbol(s),
                     "limit": config.ORDERBOOK_DEPTH}),
    Probe("bybit", "ticker", "https://api.bybit.com/v5/market/tickers",
          lambda s: {"category": "linear", "symbol": _bybit_symbol(s)}),
    Probe("okx", "trades", "https://www.okx.com/api/v5/market/trades",
          lambda s: {"instId": s, "limit": 100}),
    Probe("okx", "orderbook", "https://www.okx.com/api/v5/market/books",
          lambda s: {"instId": s, "sz": config.ORDERBOOK_DEPTH}),
    # Measured, not assumed: /market/tickers ignores instId and returns every SWAP
    # instrument, so the singular endpoint is the one that samples this instrument.
    Probe("okx", "ticker", "https://www.okx.com/api/v5/market/ticker",
          lambda s: {"instId": s}),
    Probe("gate", "trades", "https://api.gateio.ws/api/v4/futures/usdt/trades",
          lambda s: {"contract": s, "limit": 100}),
    Probe("gate", "orderbook", "https://api.gateio.ws/api/v4/futures/usdt/order_book",
          lambda s: {"contract": s, "limit": config.ORDERBOOK_DEPTH}),
    Probe("gate", "ticker", "https://api.gateio.ws/api/v4/futures/usdt/tickers",
          lambda s: {"contract": s}),
)


def probes_for(venue: str) -> tuple[Probe, ...]:
    return tuple(probe for probe in PROBE_TABLE if probe.venue == venue)


def cadence_for(probe: str, offset_sec: float) -> float:
    """Tighten sampling near t0, which is where the budget is worth spending."""
    if abs(offset_sec) <= config.BURST_HALF_WIDTH_SEC:
        return config.BURST_CADENCE_SEC[probe]
    return config.PROBE_CADENCE_SEC[probe]


# ------------------------------------------------------------------- t0 confirmation

# The registry's t0 was true when it was written. A pre-market listing can be moved,
# and a venue can disagree with itself: measured on 2026-08-22, OKX returned
# listTime 2026-09-09 for JP225-USDT-SWAP when asked for every SWAP instrument and
# listTime 2026-08-07 for the same instrument at the same moment when asked with
# instId - a 33-day spread in this project's primary datum. So t0 is re-read from the
# venue immediately before the loop, in every query shape known to differ, and a
# disagreement stops the capture instead of aiming it at a moment nothing happens at.
@dataclass(frozen=True)
class T0Source:
    label: str
    url: str
    params_for: Callable[[str], dict[str, Any]]
    rows_path: tuple[str, ...]
    symbol_field: str
    t0_field: str
    unit: str


T0_SOURCES: dict[str, tuple[T0Source, ...]] = {
    "bybit": (
        T0Source("instruments-info/by-symbol",
                 "https://api.bybit.com/v5/market/instruments-info",
                 lambda s: {"category": "linear", "symbol": s},
                 ("result", "list"), "symbol", "launchTime", "ms"),
    ),
    "okx": (
        T0Source("instruments/bulk", "https://www.okx.com/api/v5/public/instruments",
                 lambda s: {"instType": "SWAP"},
                 ("data",), "instId", "listTime", "ms"),
        T0Source("instruments/by-instId", "https://www.okx.com/api/v5/public/instruments",
                 lambda s: {"instType": "SWAP", "instId": s},
                 ("data",), "instId", "listTime", "ms"),
    ),
    "gate": (
        T0Source("contracts/bulk", "https://api.gateio.ws/api/v4/futures/usdt/contracts",
                 lambda s: {}, (), "name", "create_time", "s"),
    ),
}

T0_DISAGREEMENT_TOLERANCE_SEC = 60


def _dig(payload: Any, path: Sequence[str]) -> Any:
    for key in path:
        if not isinstance(payload, Mapping):
            return None
        payload = payload.get(key)
    return payload


def confirm_t0(
    job: CaptureJob,
    *,
    fetch: Callable[[str, Mapping[str, Any]], Any] | None = None,
    tolerance_sec: int = T0_DISAGREEMENT_TOLERANCE_SEC,
) -> dict[str, Any]:
    """Re-read t0 from the venue in every query shape, and compare with the registry."""
    fetch = fetch or (lambda url, params: public_http.get_json(url, params=params))
    observations: list[dict[str, Any]] = []
    for source in T0_SOURCES.get(job.venue, ()):
        record: dict[str, Any] = {"query": source.label, "t0_ts": None}
        try:
            rows = _dig(fetch(source.url, source.params_for(job.symbol)), source.rows_path)
            match = next(
                (row for row in (rows or []) if row.get(source.symbol_field) == job.symbol),
                None,
            )
            if match is None:
                record["error"] = "instrument not present in this response"
            else:
                raw = int(match[source.t0_field])
                record["t0_ts"] = raw // 1000 if source.unit == "ms" else raw
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"{type(exc).__name__}: {exc}"
        observations.append(record)

    seen = [obs["t0_ts"] for obs in observations if obs["t0_ts"] is not None]
    spread = (max(seen) - min(seen)) if len(seen) > 1 else 0
    drift = min((abs(value - job.t0_ts) for value in seen), default=None)
    tolerance = max(tolerance_sec, job.t0_precision_sec)

    blockers: list[str] = []
    if not seen:
        blockers.append("the venue did not report a t0 for this instrument")
    if spread > tolerance:
        blockers.append(
            f"the venue reports t0 values {spread}s apart across query shapes; "
            "capturing would aim at a moment one of them says is wrong"
        )
    if drift is not None and drift > tolerance:
        blockers.append(
            f"the venue's t0 has moved {drift}s from the registry value; "
            "refresh the registry before capturing"
        )
    return {
        "registry_t0_ts": job.t0_ts,
        "observations": observations,
        "venue_spread_sec": spread,
        "drift_from_registry_sec": drift,
        "tolerance_sec": tolerance,
        "confirmed": not blockers,
        "blockers": blockers,
    }


# --------------------------------------------------------------------------- capture


@dataclass
class CaptureJob:
    capture_id: str
    venue: str
    symbol: str
    t0_ts: int
    t0_source_class: str
    t0_precision_sec: int
    caveats: list[str] = field(default_factory=list)


def job_from_event(event: Mapping[str, Any], *, capture_id: str) -> CaptureJob:
    return CaptureJob(
        capture_id=capture_id,
        venue=str(event["venue"]),
        symbol=str(event["symbol"]),
        t0_ts=int(event["t0_ts"]),
        t0_source_class=str(event["t0_source_class"]),
        t0_precision_sec=int(event.get("t0_precision_sec") or 0),
        caveats=list(event.get("caveats") or []),
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _default_fetch(probe: Probe, symbol: str, timeout_sec: int) -> Any:
    return public_http.get_json(
        probe.url, params=probe.params_for(symbol), timeout_sec=timeout_sec,
        max_retries=1,   # a capture cannot afford a long retry: the moment passes
    )


def run_capture(
    job: CaptureJob,
    *,
    capture_dir: Path,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    fetch: Callable[[Probe, str, int], Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
    timeout_sec: int = 10,
    max_requests: int | None = None,
    max_runtime_sec: int | None = None,
    t0_confirmation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Sample one instrument across the window around its t0.

    Every knob a test needs - the clock, sleeping, fetching, the stop signal - is
    injected, so the loop's behaviour under a spent budget, an expired deadline or a
    stop request is provable without a network or a wait."""
    fetch = fetch or _default_fetch
    should_stop = should_stop or config.STOP_REQUEST_PATH.is_file
    max_requests = config.MAX_REQUESTS_PER_CAPTURE if max_requests is None else max_requests
    max_runtime_sec = (
        config.MAX_CAPTURE_RUNTIME_SEC if max_runtime_sec is None else max_runtime_sec
    )

    probes = probes_for(job.venue)
    if not probes:
        raise CaptureError(f"no probes defined for venue: {job.venue}")

    window_start = job.t0_ts - config.CAPTURE_WINDOW_BEFORE_SEC
    window_end = job.t0_ts + config.CAPTURE_WINDOW_AFTER_SEC
    deadline = monotonic() + max_runtime_sec

    capture_dir.mkdir(parents=True, exist_ok=True)
    samples_path = capture_dir / "samples.jsonl"
    started_at_utc = utc_now_iso()

    next_due: dict[str, float] = {probe.probe: window_start for probe in probes}
    per_probe_times: dict[str, list[float]] = {probe.probe: [] for probe in probes}
    per_probe_errors: dict[str, int] = {probe.probe: 0 for probe in probes}
    requests_made = 0
    rows_written = 0
    stop_reason = "window_complete"

    with samples_path.open("w", encoding="utf-8") as handle:
        while True:
            now = clock()
            if now >= window_end:
                break
            if monotonic() > deadline:
                stop_reason = "max_runtime_sec_exceeded"
                break
            if should_stop():
                stop_reason = "stop_requested"
                break
            if requests_made >= max_requests:
                stop_reason = "max_requests_exceeded"
                break

            due = [probe for probe in probes if now >= next_due[probe.probe]]
            if not due:
                soonest = min(next_due[probe.probe] for probe in probes)
                # Never sleep past the end of the window or past a stop check.
                sleep_fn(max(0.0, min(soonest - now, 0.25)))
                continue

            for probe in due:
                if requests_made >= max_requests:
                    stop_reason = "max_requests_exceeded"
                    break
                request_ts = clock()
                offset = request_ts - job.t0_ts
                record: dict[str, Any] = {
                    "schema": SAMPLE_SCHEMA,
                    "capture_id": job.capture_id,
                    "venue": job.venue,
                    "symbol": job.symbol,
                    "probe": probe.probe,
                    "t0_ts": job.t0_ts,
                    "request_ts": round(request_ts, 3),
                    "offset_sec": round(offset, 3),
                }
                try:
                    payload = fetch(probe, job.symbol, timeout_sec)
                    received_ts = clock()
                    record["received_ts"] = round(received_ts, 3)
                    record["latency_ms"] = round((received_ts - request_ts) * 1000, 1)
                    record["payload"] = payload
                except Exception as exc:  # noqa: BLE001
                    received_ts = clock()
                    record["received_ts"] = round(received_ts, 3)
                    record["latency_ms"] = round((received_ts - request_ts) * 1000, 1)
                    # Before t0 an instrument may not exist yet, and a venue may rate
                    # limit us. Both are observations about the capture, not reasons to
                    # abandon it - but they are never silently dropped either.
                    record["error"] = f"{type(exc).__name__}: {exc}"
                    per_probe_errors[probe.probe] += 1
                requests_made += 1
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                rows_written += 1
                per_probe_times[probe.probe].append(request_ts)
                next_due[probe.probe] = request_ts + cadence_for(probe.probe, offset)
            if stop_reason == "max_requests_exceeded":
                break
        # The samples are the only durable evidence: on the platter before the manifest
        # that vouches for them exists.
        handle.flush()
        os.fsync(handle.fileno())

    sampled = sorted(t for times in per_probe_times.values() for t in times)
    manifest = {
        "schema": CAPTURE_SCHEMA,
        "capture_id": job.capture_id,
        # Two different facts, deliberately not collapsed into one word. The loop can
        # run to the end of its window and still produce data that cannot answer the
        # question - a capture launched four minutes before t0 covers half the burst.
        # Reporting only "COMPLETED" would describe the process and be read as a
        # verdict on the data.
        "status": "COMPLETED" if stop_reason == "window_complete" else "STOPPED_INCOMPLETE",
        "stop_reason": stop_reason,
        "replay_readiness": replay_readiness(sampled, t0_ts=job.t0_ts),
        "venue": job.venue,
        "symbol": job.symbol,
        "t0_ts": job.t0_ts,
        "t0_source_class": job.t0_source_class,
        "t0_precision_sec": job.t0_precision_sec,
        "t0_caveats": job.caveats,
        "t0_confirmation": dict(t0_confirmation) if t0_confirmation else None,
        "window": {"start_ts": window_start, "end_ts": window_end,
                   "before_sec": config.CAPTURE_WINDOW_BEFORE_SEC,
                   "after_sec": config.CAPTURE_WINDOW_AFTER_SEC},
        "started_at_utc": started_at_utc,
        "finished_at_utc": utc_now_iso(),
        "requests_made": requests_made,
        "max_requests": max_requests,
        "rows_written": rows_written,
        "errors_by_probe": per_probe_errors,
        "output_sha256": _sha256_file(samples_path),
        # The honest part: what the sampling actually achieved, not what was intended.
        "sampling": sampling_report(per_probe_times, t0_ts=job.t0_ts),
        "sampling_method": (
            "REST polling: each sample is the venue's answer at our request instant, "
            "not a continuous tape; gaps between samples are unobserved"
        ),
    }
    manifest_path = capture_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


def replay_readiness(sampled: Sequence[float], *, t0_ts: int) -> dict[str, Any]:
    """Whether these samples can support the question the project exists to ask.

    Separate from the loop's own outcome on purpose. A capture started late finishes
    its window normally and reports COMPLETED while covering only half the seconds the
    hypothesis is about; the operator has to be told that in the same breath, not left
    to infer it from timestamps."""
    notes: list[str] = []
    if not sampled:
        return {"ready": False, "notes": ["no samples were collected at all"],
                "covered_before_t0_sec": None, "covered_after_t0_sec": None,
                "covers_full_burst_window": False}

    before = round(t0_ts - sampled[0], 3)
    after = round(sampled[-1] - t0_ts, 3)
    half = config.BURST_HALF_WIDTH_SEC
    covers_burst = before >= half and after >= half
    if before <= 0:
        notes.append("capture began at or after t0: there is no pre-listing baseline")
    elif before < half:
        notes.append(
            f"only {before}s of pre-t0 coverage, less than the {half}s burst window"
        )
    if after < half:
        notes.append(
            f"only {after}s of post-t0 coverage, less than the {half}s burst window"
        )
    return {
        "ready": covers_burst and not notes,
        "covered_before_t0_sec": before,
        "covered_after_t0_sec": after,
        "covers_full_burst_window": covers_burst,
        "notes": notes,
    }


def _gap_stats(times: Sequence[float]) -> dict[str, Any]:
    ordered = sorted(times)
    gaps = [round(b - a, 3) for a, b in zip(ordered, ordered[1:])]
    return {
        "samples": len(ordered),
        # A mean flatters a capture that stalled once for thirty seconds, and a
        # thirty-second stall across t0 is exactly the failure that would invalidate
        # the replay. The maximum is the number that can disqualify a capture.
        "median_gap_sec": round(statistics.median(gaps), 3) if gaps else None,
        "max_gap_sec": max(gaps) if gaps else None,
    }


def sampling_report(
    per_probe_times: Mapping[str, Sequence[float]], *, t0_ts: int
) -> dict[str, Any]:
    """Measured cadence per probe, split by the two regimes the loop actually runs.

    Reporting one median across the whole window would average a deliberate 0.5s burst
    with a deliberate 3s background and produce a number describing neither. The split
    is not presentation: near t0 is where the hypothesis lives, so the burst carries its
    own sample count and its own worst gap, and the background is reported apart from
    it rather than diluting it."""
    report: dict[str, Any] = {}
    for probe, times in per_probe_times.items():
        burst = [t for t in times if abs(t - t0_ts) <= config.BURST_HALF_WIDTH_SEC]
        outside = [t for t in times if abs(t - t0_ts) > config.BURST_HALF_WIDTH_SEC]
        report[probe] = {
            "overall": _gap_stats(times),
            "burst": _gap_stats(burst),
            "outside_burst": _gap_stats(outside),
        }
    return report


# ------------------------------------------------------------------------ evidence


def write_capture_receipt(manifest: Mapping[str, Any], capture_dir: Path) -> Path:
    """A committed record that this capture produced these bytes."""
    receipt = {
        "schema": "premarket_perp_capture_receipt_v1",
        "capture_id": manifest["capture_id"],
        "status": manifest["status"],
        "stop_reason": manifest["stop_reason"],
        "venue": manifest["venue"],
        "symbol": manifest["symbol"],
        "t0_ts": manifest["t0_ts"],
        "t0_source_class": manifest["t0_source_class"],
        "finished_at_utc": manifest["finished_at_utc"],
        "rows_written": manifest["rows_written"],
        "requests_made": manifest["requests_made"],
        "output_sha256": manifest["output_sha256"],
        "manifest_sha256": _sha256_file(capture_dir / "manifest.json"),
        "sampling": manifest["sampling"],
        "replay_readiness": manifest["replay_readiness"],
        "t0_confirmation": manifest.get("t0_confirmation"),
    }
    receipt["receipt_hash"] = canonical_hash(receipt)
    path = config.EVIDENCE_DIR / f"{manifest['capture_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("receipt_hash") != receipt["receipt_hash"]:
            raise CaptureError(f"evidence receipt already exists and differs: {path}")
        return path
    path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# ----------------------------------------------------------------------- entrypoint


def capture_event(
    *,
    run_id: str,
    capture_token: str,
    event_id: str,
    capture_root: Path | None = None,
    accept_t0_disagreement: bool = False,
    confirm_fetch: Callable[[str, Mapping[str, Any]], Any] | None = None,
    **run_kwargs: Any,
) -> dict[str, Any]:
    """Take the token, take the claim, capture, release. In that order.

    The token proves a preflight consulted the gate, the plan and the capability scan;
    the claim proves nothing else in the workspace is writing market data at the same
    time. Neither is a formality: this is the only code here that reaches a live market
    while it is moving."""
    risk_gate.consume_capture_token(token=capture_token, run_id=run_id)

    entries = registry.load_registry()
    event = registry.latest_by_event(entries).get(event_id)
    if event is None:
        raise CaptureError(f"event not in the registry: {event_id}")

    job = job_from_event(event, capture_id=run_id)
    capture_root = capture_root or config.CAPTURE_ROOT
    capture_dir = capture_root / run_id
    if capture_dir.exists():
        raise CaptureError(f"capture directory already exists: {capture_dir}")

    # Before the claim, because a capture aimed at the wrong moment should not hold the
    # workspace's single writer slot while it collects nothing.
    confirmation = confirm_t0(job, fetch=confirm_fetch)
    if not confirmation["confirmed"] and not accept_t0_disagreement:
        raise CaptureError(
            "t0 not confirmed: " + "; ".join(confirmation["blockers"])
            + " (pass accept_t0_disagreement to capture anyway)"
        )

    claim = claim_global_market_writer(
        config.SHARED_WRITER_CLAIM_PATH,
        run_id=f"premarket_perp_capture__{run_id}",
        owner_pid=os.getpid(),
        owner_kind="premarket_perp_capture",
        plan_hash=risk_gate.load_and_verify_plan()["plan_hash"],
        output_namespace=capture_dir,
    )
    try:
        manifest = run_capture(
            job, capture_dir=capture_dir, t0_confirmation=confirmation, **run_kwargs
        )
    finally:
        release_global_market_writer(
            config.SHARED_WRITER_CLAIM_PATH,
            run_id=f"premarket_perp_capture__{run_id}",
            owner_pid=int(claim["owner_pid"]),
            ownership_token=str(claim["ownership_token"]),
            final_status="premarket_perp_capture",
            archive_dir=config.CLAIM_ARCHIVE_DIR,
        )
        config.STOP_REQUEST_PATH.unlink(missing_ok=True)

    write_capture_receipt(manifest, capture_dir)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One bounded visible capture around a listing t0.")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--capture-token", default="")
    parser.add_argument("--event-id", default="")
    parser.add_argument("--accept-t0-disagreement", action="store_true",
                        help="capture even though the venue and the registry disagree "
                             "about t0; recorded in the manifest either way")
    parser.add_argument("--plan-echo", action="store_true",
                        help="print the capture bounds this build would honour")
    args = parser.parse_args(argv)

    if args.plan_echo:
        print(json.dumps({
            "window_before_sec": config.CAPTURE_WINDOW_BEFORE_SEC,
            "window_after_sec": config.CAPTURE_WINDOW_AFTER_SEC,
            "max_runtime_sec": config.MAX_CAPTURE_RUNTIME_SEC,
            "max_requests": config.MAX_REQUESTS_PER_CAPTURE,
            "cadence_sec": config.PROBE_CADENCE_SEC,
            "burst_cadence_sec": config.BURST_CADENCE_SEC,
            "burst_half_width_sec": config.BURST_HALF_WIDTH_SEC,
            "probes": sorted({probe.probe for probe in PROBE_TABLE}),
            "venues": sorted({probe.venue for probe in PROBE_TABLE}),
        }, ensure_ascii=False))
        return 0
    if not args.capture:
        raise SystemExit("no action requested")
    if not (args.run_id and args.capture_token and args.event_id):
        raise SystemExit("--capture requires --run-id, --capture-token and --event-id")
    manifest = capture_event(
        run_id=args.run_id, capture_token=args.capture_token, event_id=args.event_id,
        accept_t0_disagreement=args.accept_t0_disagreement,
    )
    print(json.dumps({
        "status": manifest["status"],
        "capture_id": manifest["capture_id"],
        "stop_reason": manifest["stop_reason"],
        "rows_written": manifest["rows_written"],
        "requests_made": manifest["requests_made"],
        "replay_readiness": manifest["replay_readiness"],
        "sampling": manifest["sampling"],
    }, ensure_ascii=False))
    # A capture that finished its loop but cannot answer the question is not a success,
    # and a script that only checks the exit code must not be told otherwise.
    ready = bool(manifest["replay_readiness"]["ready"])
    return 0 if manifest["status"] == "COMPLETED" and ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
