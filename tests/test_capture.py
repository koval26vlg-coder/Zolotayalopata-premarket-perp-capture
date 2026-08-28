"""What the capture loop must do when the window, the budget or the operator says stop.

The loop is driven through injected clock, sleep and fetch, so every bound is proved
without a network and without waiting: a spent budget, an expired deadline and a stop
request are ordinary test cases here rather than things believed about the code.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import capture  # noqa: E402
import project_config as config  # noqa: E402
import public_http  # noqa: E402


T0 = 1_800_000_000
CLASS = "VENUE_INSTRUMENT_METADATA"


class FakeClock:
    """Wall time the test controls; sleeping is the only thing that advances it."""

    def __init__(self, start: float) -> None:
        self.now = float(start)

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        self.now += seconds


def job(**overrides):
    fields = {
        "capture_id": "test_capture",
        "venue": "bybit",
        "symbol": "NEWUSDT",
        "t0_ts": T0,
        "t0_source_class": "VENUE_INSTRUMENT_METADATA",
        "t0_precision_sec": 1,
        "caveats": [],
    }
    fields.update(overrides)
    return capture.CaptureJob(**fields)


def rows_in(tmp: Path) -> list[dict]:
    text = (tmp / "samples.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def valid_payload(
    probe: str, symbol: str = "NEWUSDT", timestamp: float = T0
) -> dict:
    milliseconds = int(timestamp * 1000)
    if probe == "trades":
        return {"retCode": 0, "result": {"list": [{
            "execId": "1", "symbol": symbol, "price": "10", "size": "1",
            "time": str(milliseconds),
        }]}}
    if probe == "orderbook":
        return {"retCode": 0, "result": {
            "s": symbol, "b": [["9.9", "1"]], "a": [["10.1", "1"]],
            "ts": milliseconds,
        }}
    return {"retCode": 0, "time": milliseconds, "result": {"list": [{
        "symbol": symbol, "bid1Price": "9.9", "ask1Price": "10.1",
        "markPrice": "10", "indexPrice": "10",
    }]}}


class CaptureHarness(unittest.TestCase):
    """Helpers only - no tests here, so nothing below re-runs them."""

    def tmpdir(self) -> Path:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return Path(holder.name)

    def narrow_window(self, before: int = 60, after: int = 65, burst: int = 10):
        patcher = mock.patch.multiple(
            config,
            CAPTURE_WINDOW_BEFORE_SEC=before,
            CAPTURE_WINDOW_AFTER_SEC=after,
            BURST_HALF_WIDTH_SEC=burst,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_loop(self, tmp: Path, *, latency: float = 0.05, fail=None, **kwargs):
        clock = FakeClock(T0 - config.CAPTURE_WINDOW_BEFORE_SEC)

        def fetch(probe, symbol, timeout_sec):
            clock.advance(latency)
            if fail is not None:
                raise fail
            return valid_payload(probe.probe, symbol, clock.time())

        kwargs.setdefault("fetch", fetch)
        kwargs.setdefault("should_stop", lambda: False)
        manifest = capture._run_capture_core(
            job(),
            capture_dir=tmp,
            clock=clock.time,
            monotonic=clock.monotonic,
            sleep_fn=clock.sleep,
            **kwargs,
        )
        readiness = manifest["replay_readiness"]
        readiness["structural_ready"] = bool(readiness.get("ready"))
        readiness["ready"] = False
        readiness["notes"] = [
            *list(readiness.get("notes") or []),
            "test harness is synthetic/offline-only and cannot support acceptance",
        ]
        manifest["evidence_class"] = "SYNTHETIC_OFFLINE_ONLY"
        manifest["acceptance_capable"] = False
        capture._write_json_exclusive(tmp / "manifest.json", manifest)
        return manifest, clock


class ProbeTableTests(unittest.TestCase):
    def test_every_probe_url_is_declared_in_the_allow_list(self):
        # The collector must not be the place a new endpoint quietly enters the project.
        for probe in capture.PROBE_TABLE:
            with self.subTest(venue=probe.venue, probe=probe.probe):
                self.assertTrue(
                    public_http.endpoint_is_allowed(probe.url),
                    f"{probe.url} is not in ALLOWED_ENDPOINTS",
                )

    def test_each_venue_has_all_three_probes(self):
        for venue in ("bybit", "okx", "gate"):
            with self.subTest(venue=venue):
                kinds = {probe.probe for probe in capture.probes_for(venue)}
                self.assertEqual(kinds, set(capture.PROBES))

    def test_a_probe_never_points_at_another_venues_host(self):
        hosts = {"bybit": "api.bybit.com", "okx": "www.okx.com", "gate": "api.gateio.ws"}
        for probe in capture.PROBE_TABLE:
            with self.subTest(probe=probe.url):
                self.assertIn(hosts[probe.venue], probe.url)

    def test_unknown_venue_has_no_probes_and_cannot_be_captured(self):
        self.assertEqual(capture.probes_for("binance"), ())
        with self.assertRaises(capture.CaptureError):
            capture.run_capture(job(venue="binance"), capture_dir=Path("."))

    def test_orderbook_depth_is_bounded_and_declared(self):
        for probe in capture.PROBE_TABLE:
            if probe.probe == "orderbook":
                params = probe.params_for("X")
                depth = params.get("limit") or params.get("sz")
                self.assertEqual(int(depth), config.ORDERBOOK_DEPTH)


class CadenceTests(unittest.TestCase):
    def test_sampling_tightens_inside_the_burst_window(self):
        for probe in capture.PROBES:
            with self.subTest(probe=probe):
                near = capture.cadence_for(probe, 0.0)
                far = capture.cadence_for(probe, config.BURST_HALF_WIDTH_SEC + 1)
                self.assertLess(near, far)

    def test_burst_applies_symmetrically_before_and_after_t0(self):
        half = config.BURST_HALF_WIDTH_SEC
        self.assertEqual(
            capture.cadence_for("trades", -half), capture.cadence_for("trades", half)
        )
        self.assertEqual(
            capture.cadence_for("trades", -half), config.BURST_CADENCE_SEC["trades"]
        )

    def test_declared_cadence_fits_inside_the_request_budget(self):
        # A cadence the budget cannot fund would stop the capture mid-window, which is
        # the one thing a capture must not do quietly.
        window = config.CAPTURE_WINDOW_BEFORE_SEC + config.CAPTURE_WINDOW_AFTER_SEC
        burst = 2 * config.BURST_HALF_WIDTH_SEC
        worst = 0.0
        for probe in capture.PROBES:
            worst += burst / config.BURST_CADENCE_SEC[probe]
            worst += (window - burst) / config.PROBE_CADENCE_SEC[probe]
        self.assertLess(worst, config.MAX_REQUESTS_PER_CAPTURE)


class LoopBoundTests(CaptureHarness):
    def test_capture_covers_the_window_and_stops_at_its_end(self):
        self.narrow_window()
        tmp = self.tmpdir()
        manifest, _ = self.run_loop(tmp)
        self.assertEqual(manifest["stop_reason"], "window_complete")
        self.assertEqual(manifest["status"], "COMPLETED")
        rows = rows_in(tmp)
        self.assertTrue(rows)
        self.assertGreaterEqual(
            min(row["request_ts"] for row in rows), manifest["window"]["start_ts"]
        )
        self.assertLess(
            max(row["request_ts"] for row in rows), manifest["window"]["end_ts"] + 1
        )

    def test_request_budget_stops_the_capture_and_says_so(self):
        self.narrow_window()
        tmp = self.tmpdir()
        manifest, _ = self.run_loop(tmp, max_requests=7)
        self.assertEqual(manifest["stop_reason"], "max_requests_exceeded")
        self.assertEqual(manifest["status"], "STOPPED_INCOMPLETE")
        self.assertLessEqual(manifest["requests_made"], 7)

    def test_runtime_deadline_stops_the_capture_and_says_so(self):
        self.narrow_window()
        tmp = self.tmpdir()
        manifest, _ = self.run_loop(tmp, max_runtime_sec=5)
        self.assertEqual(manifest["stop_reason"], "max_runtime_sec_exceeded")
        self.assertEqual(manifest["status"], "STOPPED_INCOMPLETE")

    def test_stop_request_is_honoured_inside_the_loop_not_between_captures(self):
        # The spot audit found a stop request that was only read between events, which
        # made it useless during the run it was meant to end.
        state = {"calls": 0}

        def should_stop():
            state["calls"] += 1
            return state["calls"] > 3

        self.narrow_window()
        tmp = self.tmpdir()
        manifest, _ = self.run_loop(tmp, should_stop=should_stop)
        self.assertEqual(manifest["stop_reason"], "stop_requested")
        self.assertEqual(manifest["status"], "STOPPED_INCOMPLETE")
        self.assertLess(manifest["requests_made"], 30)

    def test_an_incomplete_capture_is_never_reported_as_completed(self):
        self.narrow_window()
        tmp = self.tmpdir()
        manifest, _ = self.run_loop(tmp, max_requests=3)
        self.assertNotEqual(manifest["status"], "COMPLETED")
        self.assertIn("stop_reason", manifest)


class SampleRecordTests(CaptureHarness):
    def test_each_sample_records_when_we_asked_not_only_what_came_back(self):
        self.narrow_window()
        tmp = self.tmpdir()
        self.run_loop(tmp, latency=0.2)
        for row in rows_in(tmp):
            self.assertIn("request_ts", row)
            self.assertIn("received_ts", row)
            self.assertGreaterEqual(row["received_ts"], row["request_ts"])
            self.assertAlmostEqual(row["latency_ms"], 200.0, delta=1.0)

    def test_offset_is_relative_to_t0_and_signed(self):
        self.narrow_window()
        tmp = self.tmpdir()
        self.run_loop(tmp)
        rows = rows_in(tmp)
        self.assertTrue(any(row["offset_sec"] < 0 for row in rows))
        self.assertTrue(any(row["offset_sec"] > 0 for row in rows))
        for row in rows:
            self.assertAlmostEqual(row["offset_sec"], row["request_ts"] - T0, places=2)

    def test_a_venue_error_becomes_a_recorded_sample_not_an_abandoned_capture(self):
        # Before t0 the instrument may not exist yet; that is an observation.
        self.narrow_window()
        tmp = self.tmpdir()
        manifest, _ = self.run_loop(tmp, fail=RuntimeError("instrument not found"))
        rows = rows_in(tmp)
        self.assertTrue(rows)
        self.assertTrue(all("error" in row for row in rows))
        self.assertEqual(manifest["stop_reason"], "no_successful_payloads")
        self.assertEqual(manifest["status"], "STOPPED_INCOMPLETE")
        self.assertEqual(sum(manifest["errors_by_probe"].values()), len(rows))

    def test_every_sample_carries_its_capture_identity(self):
        self.narrow_window()
        tmp = self.tmpdir()
        self.run_loop(tmp)
        for row in rows_in(tmp):
            self.assertEqual(row["schema"], capture.SAMPLE_SCHEMA)
            self.assertEqual(row["capture_id"], "test_capture")
            self.assertEqual(row["t0_ts"], T0)


class ManifestTests(CaptureHarness):
    def test_output_sha256_matches_the_bytes_actually_on_disk(self):
        self.narrow_window()
        tmp = self.tmpdir()
        manifest, _ = self.run_loop(tmp)
        digest = hashlib.sha256((tmp / "samples.jsonl").read_bytes()).hexdigest()
        self.assertEqual(manifest["output_sha256"], digest)

    def test_manifest_row_count_matches_the_file(self):
        self.narrow_window()
        tmp = self.tmpdir()
        manifest, _ = self.run_loop(tmp)
        self.assertEqual(manifest["rows_written"], len(rows_in(tmp)))

    def test_manifest_states_that_this_is_polling_not_a_tape(self):
        self.narrow_window()
        tmp = self.tmpdir()
        manifest, _ = self.run_loop(tmp)
        self.assertIn("not a continuous tape", manifest["sampling_method"])

    def test_t0_provenance_travels_with_the_capture(self):
        self.narrow_window()
        tmp = self.tmpdir()
        manifest, _ = self.run_loop(tmp)
        self.assertEqual(manifest["t0_source_class"], "VENUE_INSTRUMENT_METADATA")
        self.assertIn("t0_precision_sec", manifest)
        self.assertIn("t0_caveats", manifest)


class SamplingReportTests(CaptureHarness):
    def test_a_stall_shows_in_the_maximum_even_when_the_median_looks_healthy(self):
        times = [float(i) for i in range(50)] + [80.0]
        report = capture.sampling_report({"trades": times}, t0_ts=0)
        self.assertEqual(report["trades"]["overall"]["median_gap_sec"], 1.0)
        self.assertEqual(report["trades"]["overall"]["max_gap_sec"], 31.0)

    def test_the_window_that_matters_is_reported_separately(self):
        far = [float(i) for i in range(1000, 1100)]
        near = [T0 + i * 0.5 for i in range(-10, 10)]
        report = capture.sampling_report({"trades": far + near}, t0_ts=T0)
        self.assertEqual(report["trades"]["burst"]["samples"], len(near))
        self.assertEqual(report["trades"]["burst"]["median_gap_sec"], 0.5)
        self.assertEqual(report["trades"]["outside_burst"]["samples"], len(far))

    def test_a_probe_that_never_sampled_reports_nothing_rather_than_zero(self):
        report = capture.sampling_report({"trades": []}, t0_ts=T0)
        self.assertEqual(report["trades"]["overall"]["samples"], 0)
        self.assertIsNone(report["trades"]["overall"]["median_gap_sec"])
        self.assertIsNone(report["trades"]["overall"]["max_gap_sec"])

    def test_the_loop_actually_samples_faster_near_t0(self):
        self.narrow_window(before=60, after=30, burst=10)
        tmp = self.tmpdir()
        manifest, _ = self.run_loop(tmp, latency=0.0)
        trades = manifest["sampling"]["trades"]
        self.assertLess(
            trades["burst"]["median_gap_sec"], trades["outside_burst"]["median_gap_sec"]
        )

    def test_the_two_regimes_are_never_collapsed_into_one_number(self):
        # An overall median averages a deliberate burst with a deliberate background
        # and describes neither, so both must be reported in their own right.
        self.narrow_window()
        tmp = self.tmpdir()
        manifest, _ = self.run_loop(tmp)
        for probe, stats in manifest["sampling"].items():
            with self.subTest(probe=probe):
                self.assertEqual(set(stats), {"overall", "burst", "outside_burst"})
                self.assertEqual(
                    stats["overall"]["samples"],
                    stats["burst"]["samples"] + stats["outside_burst"]["samples"],
                )


class ReplayReadinessTests(CaptureHarness):
    """Finishing the loop and being able to answer the question are different facts."""

    def test_a_capture_with_no_samples_is_never_ready(self):
        verdict = capture.replay_readiness([], t0_ts=T0)
        self.assertFalse(verdict["ready"])
        self.assertIn("no samples were collected at all", verdict["notes"])

    def test_a_capture_that_began_after_t0_has_no_baseline_and_says_so(self):
        samples = [T0 + i for i in range(1, 400)]
        verdict = capture.replay_readiness(samples, t0_ts=T0)
        self.assertFalse(verdict["ready"])
        self.assertTrue(any("no pre-listing baseline" in note for note in verdict["notes"]))
        self.assertLess(verdict["covered_before_t0_sec"], 0)

    def test_partial_burst_coverage_is_reported_not_rounded_up(self):
        half = config.BURST_HALF_WIDTH_SEC
        samples = [T0 - half / 2 + i for i in range(int(half))]
        verdict = capture.replay_readiness(samples, t0_ts=T0)
        self.assertFalse(verdict["ready"])
        self.assertFalse(verdict["covers_full_burst_window"])

    def test_full_coverage_on_both_sides_is_ready(self):
        half = config.BURST_HALF_WIDTH_SEC
        samples = [T0 - half - 5, T0, T0 + half + 5]
        verdict = capture.replay_readiness(samples, t0_ts=T0)
        self.assertTrue(verdict["ready"])
        self.assertTrue(verdict["covers_full_burst_window"])

    def test_a_loop_that_ran_its_whole_window_still_reports_readiness_separately(self):
        self.narrow_window()
        tmp = self.tmpdir()
        manifest, _ = self.run_loop(tmp)
        self.assertEqual(manifest["status"], "COMPLETED")
        self.assertIn("replay_readiness", manifest)
        self.assertFalse(manifest["replay_readiness"]["ready"])
        self.assertTrue(manifest["replay_readiness"]["structural_ready"])
        self.assertEqual(manifest["evidence_class"], "SYNTHETIC_OFFLINE_ONLY")

    def test_a_capture_launched_too_late_completes_its_loop_but_is_not_ready(self):
        # The defect this exists to catch: the loop ends normally, the status says
        # COMPLETED, and half the seconds the hypothesis is about were never sampled.
        self.narrow_window(before=60, after=30, burst=10)
        tmp = self.tmpdir()
        clock = FakeClock(T0 - 3)  # launched three seconds before t0

        manifest = capture._run_capture_core(
            job(), capture_dir=tmp, clock=clock.time, monotonic=clock.monotonic,
            sleep_fn=clock.sleep, should_stop=lambda: False,
            fetch=lambda probe, symbol, timeout: valid_payload(
                probe.probe, symbol, clock.time()
            ),
        )
        self.assertEqual(manifest["status"], "COMPLETED")
        self.assertFalse(manifest["replay_readiness"]["ready"])
        self.assertTrue(
            any("pre-t0 coverage" in note
                for note in manifest["replay_readiness"]["notes"])
        )


class VenueMetadataTests(CaptureHarness):
    """The venue's own instrument times are recorded, and never promoted to t0.

    Measured on 2026-08-22: OKX reported listTime 2026-09-09 for JP225-USDT-SWAP when
    asked for every SWAP instrument, and 2026-08-07 for the same instrument at the same
    moment when asked with instId. Thirty-three days apart from one endpoint. That is
    worth recording in a manifest and worth never trusting as the capture's anchor."""

    def _fetch(self, values):
        calls = iter(values)

        def fetch(url, params):
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            if "bybit" in url:
                return {"result": {"list": [
                    {"symbol": "NEWUSDT", "launchTime": str(value * 1000)}]}}
            if "okx" in url:
                return {"data": [{"instId": "NEWUSDT", "listTime": str(value * 1000)}]}
            return [{"name": "NEWUSDT", "create_time": value}]

        return fetch

    def test_the_observation_is_marked_descriptive_and_never_gates(self):
        verdict = capture.observe_venue_metadata(job(), fetch=self._fetch([T0]))
        self.assertEqual(verdict["role"], "descriptive_only")
        self.assertNotIn("blockers", verdict)

    def test_a_venue_consistent_with_the_capture_anchor_reads_clean(self):
        verdict = capture.observe_venue_metadata(job(), fetch=self._fetch([T0]))
        self.assertTrue(verdict["venue_metadata_is_self_consistent"])
        self.assertEqual(verdict["distance_from_capture_t0_sec"], 0)

    def test_two_query_shapes_that_disagree_are_recorded(self):
        # The live OKX case, reproduced.
        verdict = capture.observe_venue_metadata(
            job(venue="okx"), fetch=self._fetch([T0, T0 + 33 * 24 * 3600])
        )
        self.assertFalse(verdict["venue_metadata_is_self_consistent"])
        self.assertEqual(verdict["venue_spread_sec"], 33 * 24 * 3600)
        self.assertTrue(any("query shapes" in note for note in verdict["notes"]))

    def test_distance_from_the_anchor_is_reported_as_a_different_class_not_a_fix(self):
        verdict = capture.observe_venue_metadata(job(), fetch=self._fetch([T0 + 7200]))
        self.assertEqual(verdict["distance_from_capture_t0_sec"], 7200)
        self.assertTrue(
            any("different source class" in note for note in verdict["notes"])
        )

    def test_a_venue_that_reports_nothing_is_recorded_rather_than_fatal(self):
        verdict = capture.observe_venue_metadata(
            job(), fetch=self._fetch([RuntimeError("down")])
        )
        self.assertIsNone(verdict["distance_from_capture_t0_sec"])
        self.assertFalse(verdict["venue_metadata_is_self_consistent"])

    def test_the_manifest_carries_what_the_venue_said(self):
        self.narrow_window()
        tmp = self.tmpdir()
        observed = {"role": "descriptive_only", "venue_spread_sec": 0}
        manifest, _ = self.run_loop(tmp, venue_metadata=observed)
        self.assertEqual(manifest["venue_metadata_observed"], observed)


class CaptureEligibilityTests(CaptureHarness):
    """Only an official announcement can anchor a capture, and nothing populates one.

    That is the point rather than a gap: venue metadata is a proxy, and a proxy that
    can silently become the anchor is how a capture ends up aimed at a moment the
    market never had."""

    EPISODE = {
        "episode_id": "bybit:NEWUSDT",
        "event_id": "bybit:NEWUSDT",
        "venue": "bybit",
        "listing_venue": "bybit",
        "symbol": "NEWUSDT",
        "premarket_contract_id": "NEWUSDT",
        "spot_symbol": "NEWUSDT",
        "official_spot_t0": T0,
        "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
        "t0_precision_sec": 1,
        "capture_eligible": True,
        "evidence_use": "ACCEPTANCE_ANCHOR",
        "official_record_hash": "a" * 64,
        "official_source_url": "https://announcements.bybit.com/en/article/new-listing",
        "official_source_identity": "human_attestation:test",
        "registry_sha256": "b" * 64,
        "registry_tail_record_hash": "d" * 64,
        "mutation_receipt_seq": 0,
        "mutation_receipt_hash": "1" * 64,
        "summary_content_sha256": "2" * 64,
        "registry_authority_state_hash": "3" * 64,
        "plan_id": "plan-test",
        "plan_hash": "e" * 64,
        "asset_class": capture.registry.ASSET_CLASS_CRYPTO_TOKEN,
        "issuer_namespace": "crypto_asset",
        "issuer_id": "NEW",
        "asset_identity_hash": "f" * 64,
    }

    def test_metadata_derived_events_are_refused_as_capture_anchors(self):
        with self.assertRaises(capture.registry.EventRegistryError):
            capture.registry.events_for_capture(
                now_ts=T0 - 60,
                source_class=capture.registry.SOURCE_VENUE_INSTRUMENT_METADATA,
            )

    def test_an_event_that_is_not_eligible_cannot_be_captured(self):
        with mock.patch.object(capture.risk_gate, "load_and_verify_plan",
                               return_value={"plan_id": "plan-test",
                                             "plan_hash": "e" * 64}), \
             mock.patch.object(capture.registry, "events_for_capture", return_value=[]):
            with self.assertRaisesRegex(capture.CaptureError, "capture-eligible"):
                capture.capture_event(
                    run_id="r1", capture_token="t", event_id="bybit:X",
                    source_class="OFFICIAL_ANNOUNCEMENT",
                )

    def test_ineligible_precision_is_rejected_before_token_claim_and_network(self):
        with mock.patch.object(
            capture.risk_gate,
            "load_and_verify_plan",
            return_value={"plan_id": "plan-test", "plan_hash": "e" * 64},
        ), mock.patch.object(
            capture.registry, "events_for_capture", return_value=[]
        ), mock.patch.object(
            capture.risk_gate, "consume_capture_token"
        ) as consume, mock.patch.object(
            capture, "claim_global_market_writer"
        ) as claim, mock.patch.object(
            capture, "observe_venue_metadata"
        ) as network:
            with self.assertRaisesRegex(capture.CaptureError, "capture-eligible"):
                capture.capture_event(
                    run_id="minute-precision",
                    capture_token="unused",
                    event_id="bybit:NEWUSDT",
                    source_class="OFFICIAL_ANNOUNCEMENT",
                )

        consume.assert_not_called()
        claim.assert_not_called()
        network.assert_not_called()

    def test_an_existing_capture_directory_is_never_overwritten(self):
        root = self.tmpdir()
        run_record = self.tmpdir() / "capture-run.json"
        (root / "r1").mkdir()
        with mock.patch.object(config, "CAPTURE_ROOT", root), \
             mock.patch.object(config, "RUN_RECORD_PATH", run_record), \
             mock.patch.object(capture.risk_gate, "consume_capture_token", return_value={}), \
             mock.patch.object(capture.risk_gate, "load_and_verify_plan",
                               return_value={"plan_id": "plan-test",
                                             "plan_hash": "e" * 64}), \
             mock.patch.object(capture.risk_gate, "read_shared_gate",
                               return_value={"open": True,
                                             "status": "READY_FOR_POSTPROCESS"}), \
             mock.patch.object(capture.registry, "events_for_capture",
                               return_value=[self.EPISODE]), \
             mock.patch.object(capture, "claim_global_market_writer",
                               return_value={"owner_pid": 1,
                                             "ownership_token": "x"}), \
             mock.patch.object(capture, "release_global_market_writer",
                               return_value=None):
            with self.assertRaisesRegex(capture.CaptureError, "already exists"):
                capture.capture_event(
                    run_id="r1", capture_token="t", event_id="bybit:NEWUSDT",
                    source_class="OFFICIAL_ANNOUNCEMENT",
                )

    def test_the_capture_anchors_on_the_official_spot_t0(self):
        seen = {}
        root = self.tmpdir()
        run_record = self.tmpdir() / "capture-run.json"
        with mock.patch.object(config, "CAPTURE_ROOT", root), \
             mock.patch.object(config, "RUN_RECORD_PATH", run_record), \
             mock.patch.object(capture.risk_gate, "consume_capture_token", return_value={}), \
             mock.patch.object(capture.risk_gate, "load_and_verify_plan",
                               return_value={"plan_id": "plan-test", "plan_hash": "e" * 64}), \
             mock.patch.object(capture.risk_gate, "read_shared_gate",
                               return_value={"open": True,
                                             "status": "READY_FOR_POSTPROCESS"}), \
             mock.patch.object(capture.registry, "events_for_capture",
                               return_value=[self.EPISODE]), \
             mock.patch.object(capture, "observe_venue_metadata", return_value={}), \
             mock.patch.object(capture, "claim_global_market_writer",
                               return_value={"owner_pid": 1, "ownership_token": "x"}), \
             mock.patch.object(capture, "release_global_market_writer", return_value=None), \
             mock.patch.object(config, "EVIDENCE_DIR", self.tmpdir()), \
             mock.patch.object(
                 capture,
                 "_build_capture_receipt_from_committed_manifest",
                 return_value={"capture_id": "r1", "receipt_hash": "a" * 64},
             ), \
             mock.patch.object(capture, "_run_capture_core",
                               side_effect=lambda job_, **k: seen.update(
                                   {"t0": job_.t0_ts, "class": job_.t0_source_class}
                               ) or {"capture_id": "r1", "status": "COMPLETED"}):
            capture.capture_event(
                run_id="r1", capture_token="t", event_id="bybit:NEWUSDT",
                source_class="OFFICIAL_ANNOUNCEMENT",
            )
        self.assertEqual(seen["t0"], T0)
        self.assertEqual(seen["class"], "OFFICIAL_ANNOUNCEMENT")

    def test_the_claim_is_released_even_when_the_capture_fails(self):
        released = []
        root = self.tmpdir()
        run_record = self.tmpdir() / "capture-run.json"
        with mock.patch.object(config, "CAPTURE_ROOT", root), \
             mock.patch.object(config, "RUN_RECORD_PATH", run_record), \
             mock.patch.object(capture.risk_gate, "consume_capture_token", return_value={}), \
             mock.patch.object(capture.risk_gate, "load_and_verify_plan",
                               return_value={"plan_id": "plan-test", "plan_hash": "e" * 64}), \
             mock.patch.object(capture.risk_gate, "read_shared_gate",
                               return_value={"open": True,
                                             "status": "READY_FOR_POSTPROCESS"}), \
             mock.patch.object(capture.registry, "events_for_capture",
                               return_value=[self.EPISODE]), \
             mock.patch.object(capture, "observe_venue_metadata", return_value={}), \
             mock.patch.object(capture, "claim_global_market_writer",
                               return_value={"owner_pid": 1, "ownership_token": "x"}), \
             mock.patch.object(capture, "release_global_market_writer",
                               side_effect=lambda *a, **k: released.append("released")), \
             mock.patch.object(capture, "_run_capture_core", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                capture.capture_event(
                    run_id="r1", capture_token="t", event_id="bybit:NEWUSDT",
                    source_class="OFFICIAL_ANNOUNCEMENT",
                )
        self.assertEqual(released, ["released"])


class EvidenceTests(CaptureHarness):
    def test_receipt_binds_the_capture_to_the_bytes_it_produced(self):
        self.narrow_window()
        tmp = self.tmpdir()
        manifest, _ = self.run_loop(tmp)
        receipt = capture._build_capture_receipt_from_committed_manifest(manifest, tmp)
        self.assertEqual(receipt["output_sha256"], manifest["output_sha256"])
        self.assertTrue(receipt["manifest_sha256"])
        self.assertTrue(receipt["receipt_hash"])
        # The receipt is what survives; readiness must survive with it.
        self.assertEqual(receipt["replay_readiness"], manifest["replay_readiness"])

    def test_rewriting_a_receipt_with_different_content_is_refused(self):
        self.narrow_window()
        tmp = self.tmpdir()
        manifest, _ = self.run_loop(tmp)
        first = capture._build_capture_receipt_from_committed_manifest(manifest, tmp)
        second = capture._build_capture_receipt_from_committed_manifest(manifest, tmp)
        self.assertEqual(first, second)
        altered = dict(manifest, rows_written=manifest["rows_written"] + 1)
        with self.assertRaises(capture.CaptureError):
            capture._build_capture_receipt_from_committed_manifest(altered, tmp)


class EntrypointTests(CaptureHarness):
    EVENT = {
        "venue": "bybit",
        "symbol": "NEWUSDT",
        "t0_ts": T0,
        "t0_source_class": "VENUE_INSTRUMENT_METADATA",
        "t0_precision_sec": 60,
    }

    def test_capture_requires_a_token(self):
        with mock.patch.object(capture.risk_gate, "load_and_verify_plan",
                               return_value={"plan_id": "plan-test",
                                             "plan_hash": "e" * 64}), \
             mock.patch.object(capture, "_select_capture_job", return_value=job()), \
             mock.patch.object(
                 capture.risk_gate,
                 "consume_capture_token",
                 side_effect=capture.risk_gate.RiskGateError("no token"),
             ):
            with self.assertRaises(capture.risk_gate.RiskGateError):
                capture.capture_event(run_id="r1", capture_token="t", event_id="bybit:X",
                                      source_class=CLASS)

    def test_cli_refuses_a_capture_without_run_id_token_and_event(self):
        with self.assertRaises(SystemExit):
            capture.main(["--capture"])

    def test_plan_echo_publishes_the_bounds_without_running_anything(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(capture.main(["--plan-echo"]), 0)
        echoed = json.loads(buffer.getvalue())
        self.assertEqual(echoed["max_requests"], config.MAX_REQUESTS_PER_CAPTURE)
        self.assertEqual(sorted(echoed["venues"]), ["bybit", "gate", "okx"])


class WriteClassTests(unittest.TestCase):
    def test_capture_is_the_write_class_that_needs_claim_and_token(self):
        rules = config.WRITE_CLASSES["market_data_capture"]
        self.assertTrue(rules["exclusive_writer_claim"])
        self.assertTrue(rules["capture_token"])

    def test_the_collector_is_bound_by_the_plan(self):
        bound = dict(config.BOUND_RUNTIME_FILES)
        self.assertEqual(bound["capture"], "src/capture.py")
        self.assertEqual(
            bound["global_market_writer_claim"], "src/global_market_writer_claim.py"
        )


if __name__ == "__main__":
    unittest.main()
