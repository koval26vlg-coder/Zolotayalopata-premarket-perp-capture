"""Offline RED cases for the second capture activation-hardening checkpoint.

The v6 implementation made the intended entrypoint substantially stricter.  These
tests close the remaining gaps between that entrypoint and the lower-level functions:
no helper may silently become a live collector, terminal evidence failures must remain
fail-closed, and a dense sequence of stale venue responses is not a market history.
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
EVENT_ID = "episode-v7-test"
SOURCE_CLASS = "OFFICIAL_ANNOUNCEMENT"
PLAN_ID = "premarket_perp_capture_test_v7"
PLAN_HASH = "e" * 64

LINEAGE = {
    "episode_id": EVENT_ID,
    "venue": "bybit",
    "premarket_contract_id": "NEWUSDT",
    "spot_symbol": "NEWUSDT",
    "official_spot_t0": T0,
    "t0_source_class": SOURCE_CLASS,
    "official_record_hash": "a" * 64,
    "official_source_url": "https://announcements.bybit.com/en/article/newusdt-listing",
    "official_source_identity": "human_attestation:reviewer",
    "registry_sha256": "b" * 64,
    "registry_tail_record_hash": "d" * 64,
    "mutation_receipt_seq": 0,
    "mutation_receipt_hash": "1" * 64,
    "summary_content_sha256": "2" * 64,
    "registry_authority_state_hash": "3" * 64,
    "plan_id": PLAN_ID,
    "plan_hash": PLAN_HASH,
    "asset_class": capture.registry.ASSET_CLASS_CRYPTO_TOKEN,
    "issuer_namespace": "crypto_asset",
    "issuer_id": "NEW",
    "asset_identity_hash": "f" * 64,
}


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


def official_event(*, precision_sec: int = 1) -> dict:
    return {
        **LINEAGE,
        "episode_id": EVENT_ID,
        "event_id": EVENT_ID,
        "venue": "bybit",
        "symbol": "NEWUSDT",
        "official_spot_t0": T0,
        "t0_source_class": SOURCE_CLASS,
        "t0_precision_sec": precision_sec,
        "caveats": [],
        "capture_eligible": True,
        "evidence_use": "ACCEPTANCE_ANCHOR",
    }


def capture_job(*, capture_id: str = "capture-v7-test", precision_sec: int = 1):
    return capture.job_from_event(
        official_event(precision_sec=precision_sec), capture_id=capture_id
    )


def bybit_payload(probe: str, exchange_ts: float) -> dict:
    milliseconds = int(exchange_ts * 1000)
    if probe == "trades":
        return {
            "retCode": 0,
            "time": milliseconds,
            "result": {"list": [{
                "execId": f"trade-{milliseconds}",
                "symbol": "NEWUSDT",
                "price": "10.0",
                "size": "1.0",
                "time": str(milliseconds),
            }]},
        }
    if probe == "orderbook":
        return {
            "retCode": 0,
            "time": milliseconds,
            "result": {
                "s": "NEWUSDT",
                "b": [["9.9", "1.0"]],
                "a": [["10.1", "1.0"]],
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
                "markPrice": "10.0",
                "indexPrice": "10.0",
            }]},
        }
    raise AssertionError(probe)


def okx_payload(probe: str, *, symbol: str, exchange_ts: float) -> dict:
    milliseconds = str(int(exchange_ts * 1000))
    if probe == "trades":
        row = {
            "instId": symbol,
            "tradeId": f"trade-{milliseconds}",
            "px": "10.0",
            "sz": "1.0",
            "side": "buy",
            "ts": milliseconds,
        }
    elif probe == "orderbook":
        row = {
            "instId": symbol,
            "bids": [["9.9", "1.0", "0", "1"]],
            "asks": [["10.1", "1.0", "0", "1"]],
            "ts": milliseconds,
        }
    elif probe == "ticker":
        row = {
            "instId": symbol,
            "bidPx": "9.9",
            "askPx": "10.1",
            "last": "10.0",
            "idxPx": "10.0",
            "ts": milliseconds,
        }
    else:
        raise AssertionError(probe)
    return {"code": "0", "data": [row]}


def sample(
    *, venue: str, symbol: str, probe: str, offset: float, payload: dict
) -> dict:
    request_ts = T0 + offset
    received_ts = request_ts + 0.05
    return {
        "schema": capture.SAMPLE_SCHEMA,
        "capture_id": "capture-v7-test",
        "venue": venue,
        "symbol": symbol,
        "probe": probe,
        "t0_ts": T0,
        "request_ts": request_ts,
        "received_ts": received_ts,
        "offset_sec": offset,
        "latency_ms": 50.0,
        "payload": payload,
    }


def dense_bybit_samples() -> list[dict]:
    records: list[dict] = []
    for probe in capture.PROBES:
        cadence = 0.5 if probe != "ticker" else 2.0
        offset = -float(config.BURST_HALF_WIDTH_SEC)
        while offset <= float(config.BURST_HALF_WIDTH_SEC) + 1e-9:
            request_ts = T0 + offset
            records.append(sample(
                venue="bybit",
                symbol="NEWUSDT",
                probe=probe,
                offset=round(offset, 3),
                payload=bybit_payload(probe, request_ts),
            ))
            offset += cadence
    return records


def dense_okx_samples(*, payload_symbol: str, stale_sec: int = 0) -> list[dict]:
    records: list[dict] = []
    requested_symbol = "NEW-USDT-SWAP"
    for probe in capture.PROBES:
        cadence = 0.5 if probe != "ticker" else 2.0
        offset = -float(config.BURST_HALF_WIDTH_SEC)
        while offset <= float(config.BURST_HALF_WIDTH_SEC) + 1e-9:
            request_ts = T0 + offset
            records.append(sample(
                venue="okx",
                symbol=requested_symbol,
                probe=probe,
                offset=round(offset, 3),
                payload=okx_payload(
                    probe,
                    symbol=payload_symbol,
                    exchange_ts=request_ts - stale_sec,
                ),
            ))
            offset += cadence
    return records


class TempHarness(unittest.TestCase):
    def tmpdir(self) -> Path:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return Path(holder.name)

    def short_window(self):
        patcher = mock.patch.multiple(
            config,
            CAPTURE_WINDOW_BEFORE_SEC=2,
            CAPTURE_WINDOW_AFTER_SEC=2,
            BURST_HALF_WIDTH_SEC=1,
            PROBE_CADENCE_SEC={"trades": 1.0, "orderbook": 1.0, "ticker": 1.0},
            BURST_CADENCE_SEC={"trades": 0.5, "orderbook": 0.5, "ticker": 0.5},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def clock(self) -> FakeClock:
        return FakeClock(T0 - config.CAPTURE_WINDOW_BEFORE_SEC)


class DirectCollectorBoundaryTests(TempHarness):
    def test_run_capture_has_no_default_live_fetch(self):
        self.short_window()
        directory = self.tmpdir() / "capture"
        clock = self.clock()
        self.assertFalse(hasattr(capture, "_default_fetch"))
        with self.assertRaisesRegex(
            capture.CaptureError, "(?i:fetch|authori|entrypoint|synthetic|fixture)"
        ):
            capture.run_capture(
                capture_job(),
                capture_dir=directory,
                clock=clock.time,
                monotonic=clock.monotonic,
                sleep_fn=clock.sleep,
                should_stop=lambda: False,
            )
        self.assertFalse(directory.exists())

    def test_existing_samples_file_is_exclusive_and_never_truncated(self):
        self.short_window()
        directory = self.tmpdir() / "capture"
        directory.mkdir()
        samples_path = directory / "samples.jsonl"
        sentinel = b"immutable prior evidence\n"
        samples_path.write_bytes(sentinel)
        clock = self.clock()

        with self.assertRaisesRegex(capture.CaptureError, "samples|exist|exclusive"):
            capture.run_capture(
                capture_job(),
                capture_dir=directory,
                clock=clock.time,
                monotonic=clock.monotonic,
                sleep_fn=clock.sleep,
                should_stop=lambda: False,
                fetch=capture.SyntheticFixtureTransport({
                    probe: bybit_payload(probe, clock.time())
                    for probe in capture.PROBES
                }),
            )
        self.assertEqual(samples_path.read_bytes(), sentinel)


class TerminalAccountingTests(TempHarness):
    def test_receipt_failure_is_failed_exception_in_run_record_and_claim_archive(self):
        root = self.tmpdir()
        run_record = root / "capture-run.json"
        released: list[str] = []
        manifest = {
            "capture_id": "run-v7",
            "status": "COMPLETED",
            "stop_reason": "window_complete",
            "replay_readiness": {"ready": True},
            "rows_written": 100,
            "requests_made": 100,
        }
        with mock.patch.object(config, "CAPTURE_ROOT", root / "captures"), \
             mock.patch.object(config, "RUN_RECORD_PATH", run_record), \
             mock.patch.object(capture.risk_gate, "consume_capture_token",
                               return_value={"binding_hash": "f" * 64}), \
             mock.patch.object(capture.risk_gate, "load_and_verify_plan",
                               return_value={"plan_id": PLAN_ID, "plan_hash": PLAN_HASH}), \
             mock.patch.object(capture.risk_gate, "read_shared_gate",
                               return_value={"open": True,
                                             "status": "READY_FOR_POSTPROCESS"}), \
             mock.patch.object(capture.registry, "events_for_capture",
                               return_value=[official_event()]), \
             mock.patch.object(capture, "claim_global_market_writer",
                               return_value={"owner_pid": 1,
                                             "ownership_token": "claim"}), \
             mock.patch.object(capture, "observe_venue_metadata", return_value={}), \
             mock.patch.object(capture, "_run_capture_core", return_value=manifest), \
             mock.patch.object(capture, "_build_capture_receipt_from_committed_manifest",
                               side_effect=capture.CaptureError("receipt commit failed")), \
             mock.patch.object(
                 capture,
                 "release_global_market_writer",
                 side_effect=lambda *args, **kwargs: released.append(
                     str(kwargs["final_status"])
                 ),
             ):
            with self.assertRaisesRegex(capture.CaptureError, "receipt commit failed"):
                capture.capture_event(
                    run_id="run-v7",
                    capture_token="token",
                    event_id=EVENT_ID,
                    source_class=SOURCE_CLASS,
                )

        terminal = json.loads(run_record.read_text(encoding="utf-8"))
        self.assertEqual(terminal["status"], "FAILED_EXCEPTION")
        self.assertIn("receipt commit failed", terminal["detail"])
        self.assertEqual(released, ["FAILED_EXCEPTION"])

    def test_gate_is_rechecked_after_claim_and_before_any_network(self):
        root = self.tmpdir()
        run_record = root / "capture-run.json"
        observed = mock.Mock(side_effect=AssertionError("network must not start"))
        released: list[str] = []
        with mock.patch.object(config, "CAPTURE_ROOT", root / "captures"), \
             mock.patch.object(config, "RUN_RECORD_PATH", run_record), \
             mock.patch.object(capture.risk_gate, "consume_capture_token",
                               return_value={"binding_hash": "f" * 64}), \
             mock.patch.object(capture.risk_gate, "load_and_verify_plan",
                               return_value={"plan_id": PLAN_ID, "plan_hash": PLAN_HASH}), \
             mock.patch.object(capture.registry, "events_for_capture",
                               return_value=[official_event()]), \
             mock.patch.object(capture, "claim_global_market_writer",
                               return_value={"owner_pid": 1,
                                             "ownership_token": "claim"}), \
             mock.patch.object(capture.risk_gate, "read_shared_gate", return_value={
                 "open": False, "status": "RUNNING", "detail": "gate changed",
             }) as read_gate, \
             mock.patch.object(capture, "observe_venue_metadata", observed), \
             mock.patch.object(
                 capture,
                 "release_global_market_writer",
                 side_effect=lambda *args, **kwargs: released.append(
                     str(kwargs["final_status"])
                 ),
             ):
            with self.assertRaisesRegex(
                (capture.CaptureError, risk_gate.RiskGateError), "gate|RUNNING"
            ):
                capture.capture_event(
                    run_id="run-v7",
                    capture_token="token",
                    event_id=EVENT_ID,
                    source_class=SOURCE_CLASS,
                )

        read_gate.assert_called()
        observed.assert_not_called()
        self.assertEqual(released, ["FAILED_EXCEPTION"])


class VenueEvidenceFreshnessTests(unittest.TestCase):
    def test_dense_but_hour_old_okx_payloads_are_not_ready(self):
        verdict = capture.replay_readiness(
            dense_okx_samples(payload_symbol="NEW-USDT-SWAP", stale_sec=3600),
            t0_ts=T0,
            required_probes=capture.PROBES,
        )
        self.assertFalse(verdict["ready"])
        self.assertGreater(verdict["invalid_samples"], 0)
        self.assertTrue(any("stale" in note.lower() for note in verdict["notes"]))

    def test_dense_payloads_for_another_instrument_are_not_ready(self):
        verdict = capture.replay_readiness(
            dense_okx_samples(payload_symbol="OTHER-USDT-SWAP"),
            t0_ts=T0,
            required_probes=capture.PROBES,
        )
        self.assertFalse(verdict["ready"])
        self.assertGreater(verdict["invalid_samples"], 0)
        self.assertTrue(any(
            "instrument" in note.lower() or "symbol" in note.lower()
            for note in verdict["notes"]
        ))

    def test_minute_precision_anchor_cannot_be_seconds_grade_ready(self):
        records = dense_bybit_samples()
        minute_precision = capture.replay_readiness(
            records,
            t0_ts=T0,
            t0_precision_sec=60,
            required_probes=capture.PROBES,
        )
        self.assertFalse(minute_precision["ready"])
        self.assertTrue(any(
            "precision" in note.lower() for note in minute_precision["notes"]
        ))

        second_precision = capture.replay_readiness(
            records,
            t0_ts=T0,
            t0_precision_sec=1,
            required_probes=capture.PROBES,
        )
        self.assertTrue(second_precision["ready"], second_precision["notes"])

    def test_empty_required_probe_set_fails_closed(self):
        verdict = capture.replay_readiness(
            [], t0_ts=T0, required_probes=()
        )
        self.assertFalse(verdict["ready"])
        self.assertTrue(any("probe" in note.lower() for note in verdict["notes"]))


class FilesystemEvidenceTests(TempHarness):
    def test_capture_id_cannot_escape_the_evidence_directory(self):
        root = self.tmpdir()
        evidence = root / "evidence"
        capture_dir = root / "capture"
        capture_dir.mkdir()
        (capture_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
        manifest = {
            "capture_id": "../escaped",
            "status": "STOPPED_INCOMPLETE",
            "stop_reason": "test",
            "venue": "bybit",
            "symbol": "NEWUSDT",
            "t0_ts": T0,
            "t0_source_class": SOURCE_CLASS,
            "finished_at_utc": "2027-01-15T00:00:00Z",
            "rows_written": 0,
            "requests_made": 0,
            "output_sha256": "0" * 64,
            "sampling": {},
            "replay_readiness": {"ready": False, "notes": ["test"]},
            "venue_metadata_observed": None,
            "lineage": dict(LINEAGE),
        }
        with self.assertRaisesRegex(capture.CaptureError, "capture_id|safe|path|manifest"):
            capture._build_capture_receipt_from_committed_manifest(
                manifest, capture_dir
            )
        self.assertFalse((root / "escaped.json").exists())


class TokenExclusivityTests(TempHarness):
    def verified_preflight(self) -> dict:
        return {
            "schema": risk_gate.PREFLIGHT_RESULT_SCHEMA,
            "ok": True,
            "verified": True,
            "decision": "ALLOW_VISIBLE_CAPTURE",
            "write_class": "market_data_capture",
            "action": risk_gate.CAPTURE_ACTION,
            "run_id": "run-v7",
            "event_id": EVENT_ID,
            "source_class": SOURCE_CLASS,
            "plan_id": PLAN_ID,
            "plan_hash": PLAN_HASH,
            "resolved_paths_hash": risk_gate.canonical_hash(
                risk_gate.resolved_config()
            ),
            "gate_status": "READY_FOR_POSTPROCESS",
            "capability_scan": {
                "status": "CAPABILITY_SCAN_CLEAN",
                "report_hash": "1" * 64,
            },
        }

    def test_mint_refuses_to_overwrite_an_outstanding_token(self):
        token_path = self.tmpdir() / "capture-token.json"
        plan = {
            "plan_id": PLAN_ID,
            "plan_hash": PLAN_HASH,
            "status": "CAPTURE_IMPLEMENTATION_AUDIT_GREEN",
        }
        with mock.patch.object(config, "CAPTURE_TOKEN_PATH", token_path), \
             mock.patch.object(risk_gate, "load_and_verify_plan", return_value=plan), \
             mock.patch.object(risk_gate, "verify_plan_write_authorization",
                               return_value={"write_class": "market_data_capture"}), \
             mock.patch.object(risk_gate, "verify_resolved_path_bindings",
                               return_value={}):
            risk_gate.mint_capture_token(
                "run-v7",
                event_id=EVENT_ID,
                source_class=SOURCE_CLASS,
                verified_preflight=self.verified_preflight(),
            )
            original = token_path.read_bytes()
            blocked = False
            try:
                risk_gate.mint_capture_token(
                    "run-v7",
                    event_id=EVENT_ID,
                    source_class=SOURCE_CLASS,
                    verified_preflight=self.verified_preflight(),
                )
            except risk_gate.RiskGateError:
                blocked = True

        self.assertEqual(token_path.read_bytes(), original)
        self.assertTrue(blocked, "a second mint must fail while a token is outstanding")


if __name__ == "__main__":
    unittest.main()
