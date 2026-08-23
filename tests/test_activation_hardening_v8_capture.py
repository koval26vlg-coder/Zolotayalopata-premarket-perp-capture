"""Offline regressions for the final capture activation boundary.

These tests never open a socket.  Injected fetch functions are deterministic
fixtures; the only filesystem writes are inside TemporaryDirectory instances.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import capture  # noqa: E402
import project_config as config  # noqa: E402


T0 = 1_800_000_000


class FakeClock:
    def __init__(self, start: float) -> None:
        self.now = float(start)

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


def job(capture_id: str = "capture-v8-test") -> capture.CaptureJob:
    return capture.CaptureJob(
        capture_id=capture_id,
        venue="bybit",
        symbol="NEWUSDT",
        t0_ts=T0,
        t0_source_class="OFFICIAL_ANNOUNCEMENT",
        t0_precision_sec=1,
    )


def bybit_payload(probe: str, timestamp: float) -> dict:
    milliseconds = int(timestamp * 1000)
    if probe == "trades":
        return {
            "retCode": 0,
            "result": {"list": [{
                "execId": f"trade-{milliseconds}",
                "symbol": "NEWUSDT",
                "price": "10",
                "size": "1",
                "time": str(milliseconds),
            }]},
        }
    if probe == "orderbook":
        return {
            "retCode": 0,
            "result": {
                "s": "NEWUSDT",
                "b": [["9.9", "1"]],
                "a": [["10.1", "1"]],
                "ts": milliseconds,
            },
        }
    if probe == "ticker":
        return {
            "retCode": 0,
            "time": milliseconds,
            "result": {"list": [{
                "symbol": "NEWUSDT",
                "bid1Price": "9.9",
                "ask1Price": "10.1",
                "markPrice": "10",
                "indexPrice": "10",
            }]},
        }
    raise AssertionError(probe)


def bybit_fixture_transport(timestamp: float) -> capture.SyntheticFixtureTransport:
    return capture.SyntheticFixtureTransport({
        probe: bybit_payload(probe, timestamp) for probe in capture.PROBES
    })


def gate_payload(probe: str, timestamp: float) -> object:
    milliseconds = int(timestamp * 1000)
    if probe == "trades":
        return [{
            "contract": "TARGET_USDT",
            "id": f"trade-{milliseconds}",
            "price": "10",
            "size": "1",
            "create_time_ms": milliseconds,
        }]
    if probe == "orderbook":
        return {
            "bids": [["9.9", "1"]],
            "asks": [["10.1", "1"]],
            "current": milliseconds,
        }
    if probe == "ticker":
        return [{
            "contract": "TARGET_USDT",
            "highest_bid": "9.9",
            "lowest_ask": "10.1",
            "mark_price": "10",
            "index_price": "10",
            "timestamp": milliseconds,
        }]
    raise AssertionError(probe)


class TempHarness(unittest.TestCase):
    def tmpdir(self) -> Path:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return Path(holder.name)

    def short_window(self) -> None:
        patcher = mock.patch.multiple(
            config,
            CAPTURE_WINDOW_BEFORE_SEC=1,
            CAPTURE_WINDOW_AFTER_SEC=2,
            BURST_HALF_WIDTH_SEC=1,
            PRIMARY_EXIT_OFFSETS_SEC=(0,),
            PROBE_CADENCE_SEC={"trades": 0.5, "orderbook": 0.5, "ticker": 0.5},
            BURST_CADENCE_SEC={"trades": 0.5, "orderbook": 0.5, "ticker": 0.5},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_public(self, directory: Path) -> dict:
        clock = FakeClock(T0 - config.CAPTURE_WINDOW_BEFORE_SEC)
        return capture.run_capture(
            job(),
            capture_dir=directory,
            clock=clock.time,
            monotonic=clock.monotonic,
            sleep_fn=clock.sleep,
            should_stop=lambda: False,
            fetch=bybit_fixture_transport(clock.time()),
        )


class PublicCaptureBoundaryTests(TempHarness):
    def test_project_live_fetch_helper_is_not_exposed(self):
        self.assertFalse(
            hasattr(capture, "_default_fetch"),
            "a module-level live HTTP helper bypasses capture_event authority",
        )

    def test_metadata_observation_has_no_ungated_live_fetch_default(self):
        self.assertFalse(
            hasattr(capture, "_metadata_fetch"),
            "module-level metadata HTTP helper bypasses capture_event authority",
        )
        request = mock.Mock(return_value={})
        with mock.patch.object(capture.public_http, "get_json", request):
            with self.assertRaises(TypeError):
                capture.observe_venue_metadata(job())
        request.assert_not_called()

    def test_public_run_capture_refuses_the_plan_bound_capture_root(self):
        self.short_window()
        root = self.tmpdir() / "plan-bound-captures"
        fetch = mock.Mock(side_effect=AssertionError("fetch must not run"))
        clock = FakeClock(T0 - 1)
        with mock.patch.object(config, "CAPTURE_ROOT", root):
            with self.assertRaisesRegex(capture.CaptureError, "PlanOnly|bound|root"):
                capture.run_capture(
                    job(),
                    capture_dir=root / "run-v8",
                    clock=clock.time,
                    monotonic=clock.monotonic,
                    sleep_fn=clock.sleep,
                    should_stop=lambda: False,
                    fetch=fetch,
                )
        fetch.assert_not_called()
        self.assertFalse(root.exists())

    def test_public_run_capture_is_synthetic_and_never_acceptance_ready(self):
        self.short_window()
        root = self.tmpdir()
        with mock.patch.object(config, "CAPTURE_ROOT", root / "plan-bound"):
            manifest = self.run_public(root / "offline-fixture")
        self.assertEqual(manifest["evidence_class"], "SYNTHETIC_OFFLINE_ONLY")
        self.assertFalse(manifest["replay_readiness"]["ready"])
        self.assertTrue(manifest["replay_readiness"]["structural_ready"])

    def test_public_synthetic_entrypoint_rejects_an_arbitrary_fetch_callable(self):
        self.short_window()
        root = self.tmpdir()
        network = mock.Mock(return_value=bybit_payload("trades", T0))
        clock = FakeClock(T0 - config.CAPTURE_WINDOW_BEFORE_SEC)
        with mock.patch.object(config, "CAPTURE_ROOT", root / "plan-bound"):
            with self.assertRaisesRegex(
                capture.CaptureError, "(?i:synthetic|fixture|transport)"
            ):
                capture.run_capture(
                    job(),
                    capture_dir=root / "offline-fixture",
                    clock=clock.time,
                    monotonic=clock.monotonic,
                    sleep_fn=clock.sleep,
                    should_stop=lambda: False,
                    fetch=network,
                )
        network.assert_not_called()
        self.assertFalse((root / "offline-fixture").exists())

    def test_internal_loop_has_no_acceptance_switch_and_commits_no_manifest(self):
        self.assertNotIn(
            "acceptance_capable",
            inspect.signature(capture._run_capture_core).parameters,
        )
        self.short_window()
        directory = self.tmpdir() / "internal-loop"
        clock = FakeClock(T0 - config.CAPTURE_WINDOW_BEFORE_SEC)
        with self.assertRaisesRegex(TypeError, "acceptance_capable"):
            capture._run_capture_core(
                job(),
                capture_dir=directory,
                fetch=lambda *_args: {},
                acceptance_capable=True,
            )
        draft = capture._run_capture_core(
            job(),
            capture_dir=directory,
            clock=clock.time,
            monotonic=clock.monotonic,
            sleep_fn=clock.sleep,
            should_stop=lambda: False,
            fetch=lambda probe, _symbol, _timeout: bybit_payload(
                probe.probe, clock.time()
            ),
        )
        self.assertNotIn("evidence_class", draft)
        self.assertFalse((directory / "manifest.json").exists())


class GateOrderbookIdentityTests(unittest.TestCase):
    def test_gate_orderbook_without_echoed_contract_has_no_identity(self):
        payload = {
            "bids": [["9.9", "1"]],
            "asks": [["10.1", "1"]],
            "current": T0 * 1000,
        }
        with self.assertRaisesRegex(capture.CaptureError, "identity|contract|request"):
            capture.validate_probe_payload(
                "gate",
                "orderbook",
                payload,
                "TARGET_USDT",
                received_ts=T0,
            )

    def test_gate_orderbook_without_contract_accepts_only_exact_request_binding(self):
        probe = next(
            item
            for item in capture.probes_for("gate")
            if item.probe == "orderbook"
        )
        identity = capture.request_identity_for(probe, "TARGET_USDT")
        payload = {
            "bids": [["9.9", "1"]],
            "asks": [["10.1", "1"]],
            "current": T0 * 1000,
        }
        exchange_ts = capture.validate_probe_payload(
            "gate",
            "orderbook",
            payload,
            "TARGET_USDT",
            received_ts=T0,
            **identity,
        )
        self.assertEqual(exchange_ts, float(T0))

        altered = dict(identity)
        altered["request_params"] = {
            **identity["request_params"],
            "contract": "OTHER_USDT",
        }
        with self.assertRaisesRegex(capture.CaptureError, "identity|contract|request"):
            capture.validate_probe_payload(
                "gate",
                "orderbook",
                payload,
                "TARGET_USDT",
                received_ts=T0,
                **altered,
            )


class MultiTradePayloadTests(unittest.TestCase):
    def test_okx_accepts_multiple_same_instrument_trades_and_rejects_mixed_rows(self):
        rows = [
            {
                "instId": "NEW-USDT-SWAP",
                "tradeId": f"trade-{index}",
                "px": "10",
                "sz": "1",
                "ts": str((T0 - index) * 1000),
            }
            for index in range(2)
        ]
        exchange_ts = capture.validate_probe_payload(
            "okx",
            "trades",
            {"code": "0", "data": rows},
            "NEW-USDT-SWAP",
            received_ts=T0,
        )
        self.assertEqual(exchange_ts, float(T0))

        mixed = [*rows, {**rows[0], "instId": "OTHER-USDT-SWAP"}]
        with self.assertRaisesRegex(capture.CaptureError, "instrument|symbol|instId"):
            capture.validate_probe_payload(
                "okx",
                "trades",
                {"code": "0", "data": mixed},
                "NEW-USDT-SWAP",
                received_ts=T0,
            )

    def test_gate_accepts_multiple_same_contract_trades_and_rejects_mixed_rows(self):
        rows = [
            {
                "contract": "TARGET_USDT",
                "id": f"trade-{index}",
                "price": "10",
                "size": "1",
                "create_time_ms": (T0 - index) * 1000,
            }
            for index in range(2)
        ]
        exchange_ts = capture.validate_probe_payload(
            "gate",
            "trades",
            rows,
            "TARGET_USDT",
            received_ts=T0,
        )
        self.assertEqual(exchange_ts, float(T0))

        mixed = [*rows, {**rows[0], "contract": "OTHER_USDT"}]
        with self.assertRaisesRegex(capture.CaptureError, "instrument|contract|matching"):
            capture.validate_probe_payload(
                "gate",
                "trades",
                mixed,
                "TARGET_USDT",
                received_ts=T0,
            )


class BoundRequestRecordingTests(TempHarness):
    def test_sample_identity_is_the_exact_request_used_by_fetch(self):
        self.short_window()
        root = self.tmpdir()
        directory = root / "offline-gate-fixture"
        clock = FakeClock(T0 - config.CAPTURE_WINDOW_BEFORE_SEC)
        actual_orderbook_params: list[dict] = []

        gate_job = capture.CaptureJob(
            capture_id="capture-v8-gate",
            venue="gate",
            symbol="TARGET_USDT",
            t0_ts=T0,
            t0_source_class="OFFICIAL_ANNOUNCEMENT",
            t0_precision_sec=1,
        )

        def fetch(probe, symbol, _timeout):
            if probe.probe == "orderbook":
                config.ORDERBOOK_DEPTH = 999
                actual_orderbook_params.append(probe.params_for(symbol))
            return gate_payload(probe.probe, clock.time())

        with mock.patch.object(config, "CAPTURE_ROOT", root / "plan-bound"), \
             mock.patch.object(config, "ORDERBOOK_DEPTH", 20):
            capture._run_capture_core(
                gate_job,
                capture_dir=directory,
                clock=clock.time,
                monotonic=clock.monotonic,
                sleep_fn=clock.sleep,
                should_stop=lambda: False,
                fetch=fetch,
            )

        records = [
            json.loads(line)
            for line in (directory / "samples.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        orderbook_record = next(
            row
            for row in records
            if row["probe"] == "orderbook"
        )
        recorded = orderbook_record["request_params"]
        self.assertEqual(actual_orderbook_params[0], recorded)
        self.assertNotIn("error", orderbook_record)


class ExclusiveArtifactTests(TempHarness):
    def test_no_public_writer_can_commit_a_production_evidence_receipt(self):
        self.assertFalse(
            hasattr(capture, "write_capture_receipt"),
            "production evidence receipt commit must stay inside capture_event",
        )

    def test_existing_manifest_is_never_replaced(self):
        self.short_window()
        root = self.tmpdir()
        directory = root / "offline-fixture"
        directory.mkdir()
        manifest_path = directory / "manifest.json"
        sentinel = b'{"immutable":true}\n'
        manifest_path.write_bytes(sentinel)
        with mock.patch.object(config, "CAPTURE_ROOT", root / "plan-bound"):
            with self.assertRaisesRegex(capture.CaptureError, "manifest|exist|exclusive"):
                self.run_public(directory)
        self.assertEqual(manifest_path.read_bytes(), sentinel)
        self.assertFalse((directory / "samples.jsonl").exists())

    def test_receipt_refuses_samples_changed_after_manifest(self):
        root = self.tmpdir()
        capture_dir = root / "capture"
        capture_dir.mkdir()
        original = b'{"sample":1}\n'
        samples_path = capture_dir / "samples.jsonl"
        samples_path.write_bytes(original)
        manifest = {
            "capture_id": "capture-v8-receipt",
            "status": "COMPLETED",
            "stop_reason": "window_complete",
            "venue": "bybit",
            "symbol": "NEWUSDT",
            "t0_ts": T0,
            "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
            "finished_at_utc": "2027-01-15T00:00:00Z",
            "rows_written": 1,
            "requests_made": 1,
            "output_sha256": hashlib.sha256(original).hexdigest(),
            "sampling": {},
            "replay_readiness": {"ready": True, "notes": []},
            "lineage": {},
        }
        (capture_dir / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        samples_path.write_bytes(b'{"sample":"tampered"}\n')
        with mock.patch.object(config, "EVIDENCE_DIR", root / "evidence"):
            with self.assertRaisesRegex(capture.CaptureError, "samples|output_sha256"):
                capture._build_capture_receipt_from_committed_manifest(
                    manifest, capture_dir
                )
        self.assertFalse((root / "evidence" / "capture-v8-receipt.json").exists())


class FreshDueRevalidationTests(TempHarness):
    def test_capture_does_not_reach_network_after_due_window_expires(self):
        root = self.tmpdir()
        current_plan = {"plan_id": "plan-v9-test", "plan_hash": "e" * 64}
        selection_times: list[int] = []

        def select_job(**kwargs):
            now_ts = int(kwargs["selection_now_ts"])
            selection_times.append(now_ts)
            if now_ts > 105:
                raise capture.CaptureError("capture due window expired")
            return job(capture_id="fresh-due-v9")

        network = mock.Mock(side_effect=AssertionError("network must not start"))
        released: list[str] = []
        with mock.patch.object(config, "CAPTURE_ROOT", root / "captures"), \
             mock.patch.object(config, "RUN_RECORD_PATH", root / "capture-run.json"), \
             mock.patch.object(config, "STOP_REQUEST_PATH", root / "stop.json"), \
             mock.patch.object(capture.time, "time", side_effect=[100, 104, 106]), \
             mock.patch.object(capture, "_select_capture_job", side_effect=select_job), \
             mock.patch.object(capture.risk_gate, "load_and_verify_plan",
                               return_value=current_plan), \
             mock.patch.object(capture.risk_gate, "consume_capture_token", return_value={}), \
             mock.patch.object(capture.risk_gate, "read_shared_gate", return_value={
                 "open": True, "status": "READY_FOR_POSTPROCESS",
             }), \
             mock.patch.object(capture, "claim_global_market_writer", return_value={
                 "owner_pid": 1, "ownership_token": "claim",
             }), \
             mock.patch.object(capture, "release_global_market_writer",
                               side_effect=lambda *args, **kwargs: released.append(
                                   str(kwargs["final_status"])
                               )), \
             mock.patch.object(capture, "observe_venue_metadata", network), \
             mock.patch.object(capture.public_http, "get_json", network):
            with self.assertRaisesRegex(capture.CaptureError, "due window expired"):
                capture.capture_event(
                    run_id="fresh-due-v9",
                    capture_token="token",
                    event_id="event-v9",
                    source_class="OFFICIAL_ANNOUNCEMENT",
                )

        self.assertEqual(selection_times, [100, 104, 106])
        network.assert_not_called()
        self.assertEqual(released, ["FAILED_EXCEPTION"])
        self.assertFalse((root / "captures" / "fresh-due-v9").exists())

    def test_old_run_does_not_delete_a_stop_request_created_after_claim_release(self):
        root = self.tmpdir()
        stop_path = root / "stop.json"
        current_plan = {"plan_id": "plan-v9-test", "plan_hash": "e" * 64}
        capture_job = job(capture_id="stop-owner-v9")

        def release_then_new_stop(*_args, **_kwargs):
            stop_path.write_text("new-run-stop\n", encoding="utf-8")

        with mock.patch.object(config, "CAPTURE_ROOT", root / "captures"), \
             mock.patch.object(config, "EVIDENCE_DIR", root / "evidence"), \
             mock.patch.object(config, "RUN_RECORD_PATH", root / "capture-run.json"), \
             mock.patch.object(config, "STOP_REQUEST_PATH", stop_path), \
             mock.patch.object(capture, "_select_capture_job", return_value=capture_job), \
             mock.patch.object(capture.risk_gate, "load_and_verify_plan",
                               return_value=current_plan), \
             mock.patch.object(capture.risk_gate, "consume_capture_token", return_value={}), \
             mock.patch.object(capture.risk_gate, "read_shared_gate", return_value={
                 "open": True, "status": "READY_FOR_POSTPROCESS",
             }), \
             mock.patch.object(capture, "claim_global_market_writer", return_value={
                 "owner_pid": 1, "ownership_token": "claim",
             }), \
             mock.patch.object(capture, "release_global_market_writer",
                               side_effect=release_then_new_stop), \
             mock.patch.object(capture, "observe_venue_metadata", return_value={}), \
             mock.patch.object(capture, "_run_capture_core", return_value={
                 "capture_id": "stop-owner-v9",
                 "status": "COMPLETED",
                 "replay_readiness": {"ready": False, "notes": []},
             }), \
             mock.patch.object(
                 capture,
                 "_build_capture_receipt_from_committed_manifest",
                 return_value={"capture_id": "stop-owner-v9", "receipt_hash": "b" * 64},
             ):
            capture.capture_event(
                run_id="stop-owner-v9",
                capture_token="token",
                event_id="event-v9",
                source_class="OFFICIAL_ANNOUNCEMENT",
            )

        self.assertTrue(stop_path.is_file())
        self.assertEqual(stop_path.read_text(encoding="utf-8"), "new-run-stop\n")


class PerRequestBoundaryTests(TempHarness):
    def test_metadata_requests_honor_boundary_between_okx_endpoints(self):
        okx_job = capture.CaptureJob(
            capture_id="metadata-boundary-v9",
            venue="okx",
            symbol="NEW-USDT-SWAP",
            t0_ts=T0,
            t0_source_class="OFFICIAL_ANNOUNCEMENT",
            t0_precision_sec=1,
        )
        calls: list[str] = []

        def fetch(url, _params):
            calls.append(url)
            return {"code": "0", "data": []}

        def boundary():
            return "stop_requested" if calls else None

        with self.assertRaisesRegex(capture.CaptureError, "stop_requested"):
            capture.observe_venue_metadata(
                okx_job,
                fetch=fetch,
                boundary_check=boundary,
            )
        self.assertEqual(len(calls), 1)

    def test_slow_first_probe_cannot_start_the_rest_of_a_due_batch_after_window_end(self):
        self.short_window()
        directory = self.tmpdir() / "slow-batch"
        clock = FakeClock(T0 - config.CAPTURE_WINDOW_BEFORE_SEC)
        calls: list[str] = []

        def slow_fetch(probe, _symbol, _timeout):
            calls.append(probe.probe)
            clock.now += (
                config.CAPTURE_WINDOW_BEFORE_SEC
                + config.CAPTURE_WINDOW_AFTER_SEC
                + 1
            )
            return bybit_payload(probe.probe, clock.time())

        draft = capture._run_capture_core(
            job(capture_id="slow-batch-v9"),
            capture_dir=directory,
            clock=clock.time,
            monotonic=clock.monotonic,
            sleep_fn=clock.sleep,
            should_stop=lambda: False,
            fetch=slow_fetch,
        )

        self.assertEqual(calls, ["trades"])
        self.assertEqual(draft["poll_requests_made"], 1)
        records = [
            json.loads(line)
            for line in (directory / "samples.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(records), 1)
        self.assertIn("error", records[0])

    def test_failed_samples_remain_in_replay_readiness_denominator(self):
        self.short_window()
        directory = self.tmpdir() / "error-denominator"
        clock = FakeClock(T0 - config.CAPTURE_WINDOW_BEFORE_SEC)

        def partly_invalid(probe, _symbol, _timeout):
            if probe.probe == "trades":
                return {"retCode": 0, "result": {}}
            return bybit_payload(probe.probe, clock.time())

        draft = capture._run_capture_core(
            job(capture_id="error-denominator-v9"),
            capture_dir=directory,
            clock=clock.time,
            monotonic=clock.monotonic,
            sleep_fn=clock.sleep,
            should_stop=lambda: False,
            fetch=partly_invalid,
        )

        recorded_errors = sum(draft["errors_by_probe"].values())
        self.assertGreater(recorded_errors, 0)
        self.assertEqual(
            draft["replay_readiness"]["invalid_samples"], recorded_errors
        )


class CausalExitEvidenceTests(unittest.TestCase):
    def test_only_post_target_samples_can_support_a_fixed_exit(self):
        offsets = (-1.0, -0.5, -0.1, 0.6, 1.0)
        records: list[dict] = []
        for probe in capture.PROBES:
            for offset in offsets:
                received_ts = T0 + offset
                records.append({
                    "schema": capture.SAMPLE_SCHEMA,
                    "capture_id": "causal-exit-v9",
                    "venue": "bybit",
                    "symbol": "NEWUSDT",
                    "probe": probe,
                    "t0_ts": T0,
                    "request_ts": received_ts - 0.01,
                    "received_ts": received_ts,
                    "offset_sec": offset,
                    "payload": bybit_payload(probe, received_ts),
                })

        with mock.patch.multiple(
            config,
            BURST_HALF_WIDTH_SEC=1,
            PRIMARY_EXIT_OFFSETS_SEC=(0,),
            BURST_CADENCE_SEC={"trades": 0.5, "orderbook": 0.5, "ticker": 0.5},
            PROBE_CADENCE_SEC={"trades": 0.5, "orderbook": 0.5, "ticker": 0.5},
        ):
            verdict = capture.replay_readiness(
                records,
                t0_ts=T0,
                t0_precision_sec=1,
                required_probes=capture.PROBES,
            )

        self.assertEqual(verdict["available_exit_offsets_sec"], [])
        self.assertTrue(any("exit" in note for note in verdict["notes"]))


class SamplingClockTests(TempHarness):
    def test_manifest_sampling_gaps_are_measured_on_received_ts(self):
        self.short_window()
        directory = self.tmpdir() / "received-clock"
        clock = FakeClock(T0 - config.CAPTURE_WINDOW_BEFORE_SEC)
        latency_by_probe = {"trades": 0.05, "orderbook": 0.15, "ticker": 0.25}
        calls_by_probe = {probe: 0 for probe in capture.PROBES}

        def delayed_fetch(probe, _symbol, _timeout):
            calls_by_probe[probe.probe] += 1
            clock.now += latency_by_probe[probe.probe] * calls_by_probe[probe.probe]
            return bybit_payload(probe.probe, clock.time())

        draft = capture._run_capture_core(
            job(capture_id="received-clock-v9"),
            capture_dir=directory,
            clock=clock.time,
            monotonic=clock.monotonic,
            sleep_fn=clock.sleep,
            should_stop=lambda: False,
            fetch=delayed_fetch,
        )
        records = [
            json.loads(line)
            for line in (directory / "samples.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        for probe in capture.PROBES:
            received = [
                float(row["received_ts"])
                for row in records
                if row["probe"] == probe and "error" not in row
            ]
            self.assertEqual(
                draft["sampling"][probe]["overall"], capture._gap_stats(received)
            )
        self.assertEqual(draft["sampling_clock"], "received_ts")


if __name__ == "__main__":
    unittest.main()
