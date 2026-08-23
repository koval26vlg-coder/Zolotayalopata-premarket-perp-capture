"""Activation-hardening contract for capture evidence and capture authority.

These tests deliberately sit apart from the older capture tests.  They describe the
minimum evidence that must exist before a REST poll may be called replay-ready, and
the context a one-shot capture token must be unable to shed between preflight and
consumption.  They are offline: clocks, venue payloads, gates and HTTP are injected.
"""

from __future__ import annotations

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
import risk_gate  # noqa: E402


T0 = 1_800_000_000
EVENT_ID = "episode-7f64d7"
SOURCE_CLASS = "OFFICIAL_ANNOUNCEMENT"
CAPTURE_ACTION = "capture one official event in a bounded visible terminal"


class FakeClock:
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


def valid_payload(probe: str, timestamp: float = T0) -> dict:
    """Smallest venue-shaped Bybit payload accepted as successful evidence."""
    millis = str(int(timestamp * 1000))
    if probe == "trades":
        return {
            "retCode": 0,
            "result": {
                "list": [{
                    "execId": f"trade-{millis}",
                    "symbol": "NEWUSDT",
                    "price": "10.0",
                    "size": "1.0",
                    "time": millis,
                }]
            },
        }
    if probe == "orderbook":
        return {
            "retCode": 0,
            "result": {
                "s": "NEWUSDT",
                "b": [["9.9", "2.0"]],
                "a": [["10.1", "2.0"]],
                "ts": int(timestamp * 1000),
            },
        }
    if probe == "ticker":
        return {
            "retCode": 0,
            "time": int(timestamp * 1000),
            "result": {
                "list": [{
                    "symbol": "NEWUSDT",
                    "bid1Price": "9.9",
                    "ask1Price": "10.1",
                    "markPrice": "10.0",
                    "indexPrice": "10.0",
                }]
            },
        }
    raise AssertionError(f"unexpected probe: {probe}")


def sample(probe: str, offset: float, *, causal: bool = True) -> dict:
    request_ts = T0 + offset
    received_ts = request_ts + 0.05 if causal else request_ts - 0.05
    return {
        "schema": capture.SAMPLE_SCHEMA,
        "capture_id": "capture-v6-test",
        "venue": "bybit",
        "symbol": "NEWUSDT",
        "probe": probe,
        "t0_ts": T0,
        "request_ts": request_ts,
        "received_ts": received_ts,
        "offset_sec": offset,
        "latency_ms": (received_ts - request_ts) * 1000,
        "payload": valid_payload(probe, received_ts),
    }


def dense_success_samples() -> list[dict]:
    records: list[dict] = []
    for probe in capture.PROBES:
        cadence = 0.5 if probe != "ticker" else 2.0
        offset = -float(config.BURST_HALF_WIDTH_SEC)
        while offset <= float(config.BURST_HALF_WIDTH_SEC) + 1e-9:
            records.append(sample(probe, round(offset, 3)))
            offset += cadence
    return records


LINEAGE = {
    "episode_id": EVENT_ID,
    "venue": "bybit",
    "premarket_contract_id": "NEWUSDT",
    "spot_symbol": "NEWUSDT",
    "official_spot_t0": T0,
    "t0_source_class": SOURCE_CLASS,
    "t0_precision_sec": 1,
    "official_record_hash": "a" * 64,
    "official_source_url": "https://announcements.bybit.com/en/article/newusdt-listing",
    "official_source_identity": "announcements.bybit.com",
    "registry_sha256": "b" * 64,
    "registry_tail_record_hash": "d" * 64,
    "mutation_receipt_seq": 0,
    "mutation_receipt_hash": "1" * 64,
    "summary_content_sha256": "2" * 64,
    "registry_authority_state_hash": "3" * 64,
    "plan_id": "premarket_perp_capture_20260823_v6",
    "plan_hash": "e" * 64,
    "asset_class": capture.registry.ASSET_CLASS_CRYPTO_TOKEN,
    "issuer_namespace": "crypto_asset",
    "issuer_id": "NEW",
    "asset_identity_hash": "f" * 64,
}


def official_event() -> dict:
    return {
        "episode_id": EVENT_ID,
        "event_id": EVENT_ID,
        "venue": "bybit",
        "symbol": "NEWUSDT",
        "official_spot_t0": T0,
        "t0_source_class": SOURCE_CLASS,
        "t0_precision_sec": 1,
        "caveats": [],
        "capture_eligible": True,
        "evidence_use": "ACCEPTANCE_ANCHOR",
        **LINEAGE,
    }


class CaptureHarness(unittest.TestCase):
    def tmpdir(self) -> Path:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return Path(holder.name)

    def short_window(self):
        patcher = mock.patch.multiple(
            config,
            CAPTURE_WINDOW_BEFORE_SEC=3,
            CAPTURE_WINDOW_AFTER_SEC=3,
            BURST_HALF_WIDTH_SEC=2,
            PROBE_CADENCE_SEC={"trades": 1.0, "orderbook": 1.0, "ticker": 1.0},
            BURST_CADENCE_SEC={"trades": 0.5, "orderbook": 0.5, "ticker": 0.5},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_loop(self, fetch, *, max_requests=None):
        self.short_window()
        directory = self.tmpdir()
        clock = FakeClock(T0 - config.CAPTURE_WINDOW_BEFORE_SEC)

        def timed_fetch(probe, symbol, timeout_sec):
            clock.advance(0.01)
            return fetch(probe, symbol, timeout_sec)

        manifest = capture._run_capture_core(
            capture.job_from_event(official_event(), capture_id="capture-v6-test"),
            capture_dir=directory,
            clock=clock.time,
            monotonic=clock.monotonic,
            sleep_fn=clock.sleep,
            should_stop=lambda: False,
            fetch=timed_fetch,
            max_requests=max_requests,
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
        capture._write_json_exclusive(directory / "manifest.json", manifest)
        return manifest, directory


class ReplayEvidenceTests(CaptureHarness):
    def test_one_hundred_percent_failed_requests_are_incomplete_and_not_ready(self):
        def fail(_probe, _symbol, _timeout):
            raise RuntimeError("venue unavailable")

        manifest, _directory = self.run_loop(fail)
        self.assertEqual(manifest["status"], "STOPPED_INCOMPLETE")
        self.assertFalse(manifest["replay_readiness"]["ready"])
        self.assertEqual(manifest["successful_payloads"], 0)
        self.assertEqual(
            sum(manifest["errors_by_probe"].values()), manifest["requests_made"]
        )

    def test_structurally_invalid_payloads_do_not_become_successful_samples(self):
        manifest, _directory = self.run_loop(
            lambda _probe, _symbol, _timeout: {"retCode": 0, "result": {}}
        )
        self.assertEqual(manifest["status"], "STOPPED_INCOMPLETE")
        self.assertFalse(manifest["replay_readiness"]["ready"])
        self.assertEqual(manifest["successful_payloads"], 0)

    def test_two_sparse_points_on_each_side_are_not_replay_ready(self):
        records = [
            sample(probe, offset)
            for probe in capture.PROBES
            for offset in (-config.BURST_HALF_WIDTH_SEC - 1,
                           config.BURST_HALF_WIDTH_SEC + 1)
        ]
        verdict = capture.replay_readiness(
            records, t0_ts=T0, required_probes=capture.PROBES
        )
        self.assertFalse(verdict["ready"])
        self.assertTrue(any("gap" in note.lower() for note in verdict["notes"]))

    def test_every_required_probe_and_all_fixed_exit_offsets_make_evidence_ready(self):
        verdict = capture.replay_readiness(
            dense_success_samples(), t0_ts=T0, required_probes=capture.PROBES
        )
        self.assertTrue(verdict["ready"], verdict["notes"])
        self.assertTrue(verdict["entry_available"])
        self.assertEqual(verdict["required_entry_lead_sec"], 60)
        self.assertEqual(verdict["required_exit_offsets_sec"], [0, 5, 15, 60])
        self.assertEqual(verdict["available_exit_offsets_sec"], [0, 5, 15, 60])
        self.assertEqual(
            sorted(verdict["successful_probes"]), sorted(capture.PROBES)
        )

    def test_missing_a_fixed_exit_offset_blocks_readiness(self):
        records = [
            row for row in dense_success_samples()
            if not (13 <= row["offset_sec"] <= 17)
        ]
        verdict = capture.replay_readiness(
            records, t0_ts=T0, required_probes=capture.PROBES
        )
        self.assertFalse(verdict["ready"])
        self.assertNotIn(15, verdict["available_exit_offsets_sec"])

    def test_missing_fixed_entry_target_blocks_readiness(self):
        records = [
            row
            for row in dense_success_samples()
            if row["offset_sec"] != -config.PRIMARY_ENTRY_LEAD_SEC
        ]
        records.extend(
            sample(probe, -config.PRIMARY_ENTRY_LEAD_SEC - 1)
            for probe in capture.PROBES
        )

        verdict = capture.replay_readiness(
            records, t0_ts=T0, required_probes=capture.PROBES
        )

        self.assertFalse(verdict["ready"])
        self.assertFalse(verdict["entry_available"])
        self.assertTrue(any("entry" in note for note in verdict["notes"]))

    def test_noncausal_received_timestamps_cannot_support_readiness(self):
        records = [
            sample(probe, offset, causal=False)
            for probe in capture.PROBES
            for offset in (-config.BURST_HALF_WIDTH_SEC, 0, 5, 15, 60,
                           config.BURST_HALF_WIDTH_SEC)
        ]
        verdict = capture.replay_readiness(
            records, t0_ts=T0, required_probes=capture.PROBES
        )
        self.assertFalse(verdict["ready"])
        self.assertGreater(verdict["noncausal_samples"], 0)


class CaptureLineageTests(CaptureHarness):
    def test_capture_job_preserves_the_full_event_and_plan_lineage(self):
        job = capture.job_from_event(official_event(), capture_id="capture-v6-test")
        self.assertEqual(job.lineage, LINEAGE)

    def test_manifest_and_receipt_preserve_identical_lineage(self):
        manifest, directory = self.run_loop(
            lambda probe, _symbol, _timeout: valid_payload(probe.probe)
        )
        self.assertEqual(manifest["lineage"], LINEAGE)
        receipt = capture._build_capture_receipt_from_committed_manifest(
            manifest, directory
        )
        self.assertEqual(receipt["lineage"], LINEAGE)
        self.assertEqual(receipt["episode_id"], EVENT_ID)
        self.assertEqual(receipt["official_record_hash"], LINEAGE["official_record_hash"])
        self.assertEqual(receipt["registry_sha256"], LINEAGE["registry_sha256"])
        self.assertEqual(receipt["plan_id"], LINEAGE["plan_id"])
        self.assertEqual(receipt["plan_hash"], LINEAGE["plan_hash"])


class BoundCaptureArgumentsTests(CaptureHarness):
    def test_run_id_cannot_escape_the_plan_bound_capture_root(self):
        with mock.patch.object(
            capture.risk_gate,
            "consume_capture_token",
            side_effect=AssertionError("unsafe run id must fail before token use"),
        ):
            with self.assertRaisesRegex(capture.CaptureError, "run_id"):
                capture.capture_event(
                    run_id="../outside",
                    capture_token="token",
                    event_id=EVENT_ID,
                    source_class=SOURCE_CLASS,
                )

    def test_request_and_runtime_limits_cannot_exceed_the_plan_programmatically(self):
        self.short_window()
        directory = self.tmpdir()
        clock = FakeClock(T0 - config.CAPTURE_WINDOW_BEFORE_SEC)
        job = capture.job_from_event(official_event(), capture_id="capture-v6-test")
        common = {
            "capture_dir": directory,
            "clock": clock.time,
            "monotonic": clock.monotonic,
            "sleep_fn": clock.sleep,
            "should_stop": lambda: False,
            "fetch": lambda probe, symbol, timeout: valid_payload(probe.probe),
        }
        with self.assertRaisesRegex(capture.CaptureError, "max_requests"):
            capture._run_capture_core(
                job, max_requests=config.MAX_REQUESTS_PER_CAPTURE + 1, **common
            )
        self.assertFalse(directory.exists() and any(directory.iterdir()))

        directory = self.tmpdir()
        common["capture_dir"] = directory
        with self.assertRaisesRegex(capture.CaptureError, "max_runtime"):
            capture._run_capture_core(
                job, max_runtime_sec=config.MAX_CAPTURE_RUNTIME_SEC + 1, **common
            )
        self.assertFalse(directory.exists() and any(directory.iterdir()))

    def test_capture_root_cannot_be_redirected_programmatically(self):
        alternate_root = self.tmpdir()
        with mock.patch.object(capture.risk_gate, "consume_capture_token", return_value={}), \
             mock.patch.object(capture.registry, "events_for_capture",
                               return_value=[official_event()]), \
             mock.patch.object(capture, "observe_venue_metadata",
                               side_effect=AssertionError("must reject before network")):
            with self.assertRaisesRegex(capture.CaptureError, "capture_root"):
                capture.capture_event(
                    run_id="run-a",
                    capture_token="token",
                    event_id=EVENT_ID,
                    source_class=SOURCE_CLASS,
                    capture_root=alternate_root,
                )

    def test_capture_rechecks_registry_lineage_against_the_current_plan(self):
        root = self.tmpdir()
        stale = official_event()
        stale["plan_hash"] = "0" * 64
        with mock.patch.object(config, "CAPTURE_ROOT", root), \
             mock.patch.object(capture.risk_gate, "consume_capture_token", return_value={}), \
             mock.patch.object(capture.registry, "events_for_capture", return_value=[stale]), \
             mock.patch.object(capture.risk_gate, "load_and_verify_plan", return_value={
                 "plan_id": LINEAGE["plan_id"], "plan_hash": LINEAGE["plan_hash"],
             }), \
             mock.patch.object(
                 capture,
                 "claim_global_market_writer",
                 side_effect=AssertionError("stale lineage must fail before claim"),
             ):
            with self.assertRaisesRegex(capture.CaptureError, "lineage plan_hash"):
                capture.capture_event(
                    run_id="run-lineage-check",
                    capture_token="token",
                    event_id=EVENT_ID,
                    source_class=SOURCE_CLASS,
                )

    def test_capture_module_exposes_no_direct_live_fetch_helper(self):
        self.assertFalse(hasattr(capture, "_default_fetch"))

    def test_manifest_transport_attempt_count_matches_the_enforced_budget(self):
        calls = 0

        def fetch(probe, _symbol, _timeout):
            nonlocal calls
            calls += 1
            return valid_payload(probe.probe)

        manifest, _directory = self.run_loop(fetch, max_requests=7)
        self.assertEqual(calls, 7)
        self.assertEqual(manifest["requests_made"], 7)
        self.assertEqual(manifest["transport_attempts"], 7)
        self.assertLessEqual(manifest["transport_attempts"], manifest["max_requests"])


class CapabilityTokenBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.token_path = Path(holder.name) / "capture-token.json"
        patcher = mock.patch.object(config, "CAPTURE_TOKEN_PATH", self.token_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def verified_preflight(self) -> dict:
        paths_hash = risk_gate.canonical_hash(risk_gate.resolved_config())
        return {
            "schema": risk_gate.PREFLIGHT_RESULT_SCHEMA,
            "ok": True,
            "verified": True,
            "decision": "ALLOW_VISIBLE_CAPTURE",
            "write_class": "market_data_capture",
            "action": CAPTURE_ACTION,
            "run_id": "run-a",
            "event_id": EVENT_ID,
            "source_class": SOURCE_CLASS,
            "plan_id": LINEAGE["plan_id"],
            "plan_hash": LINEAGE["plan_hash"],
            "resolved_paths_hash": paths_hash,
            "gate_status": "READY_FOR_POSTPROCESS",
            "capability_scan": {
                "status": "CAPABILITY_SCAN_CLEAN",
                "report_hash": "1" * 64,
            },
        }

    def authorised_plan(self) -> dict:
        return {
            "plan_id": LINEAGE["plan_id"],
            "plan_hash": LINEAGE["plan_hash"],
            "status": "CAPTURE_IMPLEMENTATION_AUDIT_GREEN",
        }

    def mint_authorised(self) -> dict:
        with mock.patch.object(risk_gate, "load_and_verify_plan",
                               return_value=self.authorised_plan()), \
             mock.patch.object(risk_gate, "verify_plan_write_authorization",
                               return_value={"write_class": "market_data_capture"}), \
             mock.patch.object(risk_gate, "verify_resolved_path_bindings", return_value={}):
            return risk_gate.mint_capture_token(
                "run-a",
                event_id=EVENT_ID,
                source_class=SOURCE_CLASS,
                verified_preflight=self.verified_preflight(),
            )

    def test_token_binds_every_preflight_dimension_used_by_capture(self):
        payload = self.mint_authorised()
        paths_hash = risk_gate.canonical_hash(risk_gate.resolved_config())
        expected = {
            "write_class": "market_data_capture",
            "action": CAPTURE_ACTION,
            "plan_id": LINEAGE["plan_id"],
            "plan_hash": LINEAGE["plan_hash"],
            "resolved_paths_hash": paths_hash,
            "event_id": EVENT_ID,
            "source_class": SOURCE_CLASS,
            "gate_status": "READY_FOR_POSTPROCESS",
            "capability_scan_status": "CAPABILITY_SCAN_CLEAN",
            "capability_scan_hash": "1" * 64,
        }
        for key, value in expected.items():
            with self.subTest(field=key):
                self.assertEqual(payload[key], value)

    def test_current_no_capture_plan_cannot_be_bypassed_by_direct_mint(self):
        plan = risk_gate.load_and_verify_plan()
        self.assertIn(
            plan["status"],
            {
                "AWAIT_CAPTURE_IMPLEMENTATION_AUDIT_NO_CAPTURE",
                "CAPTURE_IMPLEMENTATION_AUDIT_GREEN_NO_CAPTURE",
                risk_gate.REGISTRY_QUARANTINE_PLAN_STATUS,
            },
        )
        forged = self.verified_preflight()
        forged["plan_id"] = plan["plan_id"]
        forged["plan_hash"] = plan["plan_hash"]
        with self.assertRaisesRegex(risk_gate.RiskGateError, "authorize|capture"):
            risk_gate.mint_capture_token(
                "run-a",
                event_id=EVENT_ID,
                source_class=SOURCE_CLASS,
                verified_preflight=forged,
            )

    def test_token_is_bound_to_the_event_and_source_class(self):
        payload = self.mint_authorised()
        with self.assertRaises(risk_gate.RiskGateError):
            risk_gate.consume_capture_token(
                token=payload["token"],
                run_id="run-a",
                event_id="another-event",
                source_class=SOURCE_CLASS,
            )

    def test_consume_rechecks_the_live_gate(self):
        payload = self.mint_authorised()
        with mock.patch.object(risk_gate, "load_and_verify_plan",
                               return_value=self.authorised_plan()), \
             mock.patch.object(risk_gate, "verify_plan_write_authorization",
                               return_value={"write_class": "market_data_capture"}), \
             mock.patch.object(risk_gate, "verify_resolved_path_bindings", return_value={}), \
             mock.patch.object(risk_gate, "run_capability_scan", return_value={
                 "status": "CAPABILITY_SCAN_CLEAN", "report_hash": "1" * 64,
             }), \
             mock.patch.object(risk_gate, "read_shared_gate", return_value={
                 "open": False, "status": "RUNNING", "detail": "busy",
             }), \
             mock.patch.object(risk_gate, "inspect_claim",
                               return_value={"blocks": False}), \
             mock.patch.object(risk_gate, "inspect_run_record",
                               return_value={"blocks": False}):
            with self.assertRaisesRegex(risk_gate.RiskGateError, "gate|RUNNING"):
                risk_gate.consume_capture_token(
                    token=payload["token"], run_id="run-a", event_id=EVENT_ID,
                    source_class=SOURCE_CLASS,
                )

    def test_consume_rechecks_current_plan_and_capability_scan(self):
        payload = self.mint_authorised()
        drifted_plan = dict(self.authorised_plan(), plan_hash="2" * 64)
        with mock.patch.object(risk_gate, "load_and_verify_plan",
                               return_value=drifted_plan):
            with self.assertRaisesRegex(risk_gate.RiskGateError, "plan"):
                risk_gate.consume_capture_token(
                    token=payload["token"], run_id="run-a", event_id=EVENT_ID,
                    source_class=SOURCE_CLASS,
                )

        payload = self.mint_authorised()
        with mock.patch.object(risk_gate, "load_and_verify_plan",
                               return_value=self.authorised_plan()), \
             mock.patch.object(risk_gate, "verify_plan_write_authorization",
                               return_value={"write_class": "market_data_capture"}), \
             mock.patch.object(risk_gate, "verify_resolved_path_bindings", return_value={}), \
             mock.patch.object(risk_gate, "run_capability_scan", return_value={
                 "status": "CAPABILITY_SCAN_VIOLATION", "report_hash": "bad",
             }):
            with self.assertRaisesRegex(risk_gate.RiskGateError, "capability"):
                risk_gate.consume_capture_token(
                    token=payload["token"], run_id="run-a", event_id=EVENT_ID,
                    source_class=SOURCE_CLASS,
                )


if __name__ == "__main__":
    unittest.main()
