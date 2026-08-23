"""What a replay may and may not claim from polled snapshots.

The hypothesis is about +5, +15 and +60 seconds, and the capture samples the book at
the instants we ask. So the interesting cases here are not the arithmetic - they are
the gaps: a horizon nobody sampled, a capture that ended early, a book that is crossed,
a manifest that does not match its own bytes.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import project_config as config  # noqa: E402
import replay  # noqa: E402


T0 = 1_800_000_000


def bybit_book(bid, ask, *, bid_sz=10.0, ask_sz=10.0):
    return {"retCode": 0, "result": {
        "s": "NEWUSDT",
        "b": [[str(bid), str(bid_sz)]],
        "a": [[str(ask), str(ask_sz)]],
        "ts": 0,
    }}


def okx_book(bid, ask):
    return {"code": "0", "data": [{
        "instId": "NEW-USDT-SWAP",
        "bids": [[str(bid), "5", "0", "1"]],
        "asks": [[str(ask), "5", "0", "1"]],
        "ts": "0",
    }]}


def gate_book(bid, ask):
    return {"contract": "NEW_USDT",
            "bids": [{"p": str(bid), "s": 7}],
            "asks": [{"p": str(ask), "s": 7}],
            "current": 0}


def sample(received_ts, payload, *, request_ts=None, exchange_ts=None,
           probe="orderbook", error=None):
    request_ts = received_ts - 0.1 if request_ts is None else request_ts
    exchange_ts = received_ts if exchange_ts is None else exchange_ts
    row = {
        "schema": "premarket_perp_capture_sample_v1",
        "capture_id": "test_capture",
        "probe": probe,
        "request_ts": request_ts,
        "received_ts": received_ts,
        "exchange_ts": exchange_ts,
        "payload": payload,
    }
    if error:
        row["error"] = error
    return row


def write_capture(rows, *, venue="bybit", t0_ts=T0, readiness=None):
    directory = Path(tempfile.mkdtemp())
    body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    (directory / "samples.jsonl").write_text(body, encoding="utf-8", newline="")
    digest = hashlib.sha256((directory / "samples.jsonl").read_bytes()).hexdigest()
    manifest = {
        "schema": "premarket_perp_capture_v1",
        "capture_id": "test_capture",
        "evidence_class": "SYNTHETIC_OFFLINE_ONLY",
        "acceptance_capable": False,
        "venue": venue,
        "symbol": "NEWUSDT",
        "t0_ts": t0_ts,
        "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
        "output_sha256": digest,
        "replay_readiness": readiness or {"ready": True},
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline=""
    )
    return directory


def replay_fixture(directory, **kwargs):
    return replay.replay_capture(
        directory,
        evidence_mode=replay.SYNTHETIC_EVIDENCE_MODE,
        **kwargs,
    )


class IntegrityTests(unittest.TestCase):
    def test_a_capture_replays_when_its_bytes_match_its_manifest(self):
        directory = write_capture([sample(T0, bybit_book(100, 101))])
        manifest, samples = replay.load_capture(
            directory,
            evidence_mode=replay.SYNTHETIC_EVIDENCE_MODE,
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(manifest["venue"], "bybit")

    def test_samples_that_do_not_match_the_manifest_are_refused(self):
        # The manifest is what vouches for the bytes; replaying past a mismatch would
        # make the whole evidence chain decorative.
        directory = write_capture([sample(T0, bybit_book(100, 101))])
        path = directory / "samples.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace("100", "999"),
                        encoding="utf-8", newline="")
        with self.assertRaisesRegex(replay.ReplayError, "do not match the manifest"):
            replay.load_capture(
                directory,
                evidence_mode=replay.SYNTHETIC_EVIDENCE_MODE,
            )

    def test_a_missing_manifest_or_samples_file_is_refused(self):
        directory = Path(tempfile.mkdtemp())
        with self.assertRaises(replay.ReplayError):
            replay.load_capture(
                directory,
                evidence_mode=replay.SYNTHETIC_EVIDENCE_MODE,
            )


class BookExtractionTests(unittest.TestCase):
    def test_every_venue_layout_is_understood(self):
        for venue, payload in (("bybit", bybit_book(100, 101)),
                               ("okx", okx_book(100, 101)),
                               ("gate", gate_book(100, 101))):
            with self.subTest(venue=venue):
                (bid_px, _), (ask_px, _) = replay.top_of_book(venue, payload)
                self.assertEqual((bid_px, ask_px), (100.0, 101.0))

    def test_an_unknown_venue_is_refused_rather_than_guessed(self):
        with self.assertRaises(replay.ReplayError):
            replay.top_of_book("binance", {})

    def test_a_crossed_book_is_not_a_price(self):
        # Letting a crossed book through would let it set a bound.
        self.assertIsNone(replay.top_of_book("bybit", bybit_book(101, 100)))

    def test_an_empty_side_yields_nothing(self):
        payload = {"retCode": 0, "result": {"b": [], "a": [[1, 1]], "ts": 0}}
        self.assertIsNone(replay.top_of_book("bybit", payload))

    def test_failed_samples_never_enter_the_series(self):
        rows = [
            sample(T0 - 1, bybit_book(100, 101)),
            sample(T0, None, error="RuntimeError: instrument not found"),
            sample(T0 + 1, bybit_book(102, 103)),
        ]
        series = replay.book_series(rows, "bybit")
        self.assertEqual([item.bid_px for item in series], [100.0, 102.0])

    def test_the_series_is_indexed_by_received_clock_not_exchange_clock(self):
        rows = [
            sample(T0 + 5, bybit_book(102, 103), exchange_ts=T0 + 1),
            sample(T0 + 1, bybit_book(100, 101), exchange_ts=T0 + 2),
        ]
        series = replay.book_series(rows, "bybit")
        self.assertEqual([item.received_ts for item in series], [T0 + 1, T0 + 5])
        self.assertEqual([item.exchange_ts for item in series], [T0 + 2, T0 + 1])


class GrossReturnTests(unittest.TestCase):
    def observation(self, price):
        return replay.CausalBookObservation(
            status="OBSERVED",
            target_ts=T0,
            side="bid",
            max_lag_sec=0.5,
            price=price,
        )

    def test_gross_bbo_return_uses_two_observed_point_prices(self):
        result = replay.gross_bbo_return(
            self.observation(100.0), self.observation(120.0)
        )
        self.assertTrue(result["computable"])
        self.assertAlmostEqual(result["value"], 0.2, places=6)

    def test_an_unobserved_leg_is_not_computable(self):
        missing = replay.CausalBookObservation(
            status="NO_SAMPLE_AT_OR_AFTER_TARGET",
            target_ts=T0,
            side="bid",
            max_lag_sec=0.5,
        )
        self.assertFalse(
            replay.gross_bbo_return(self.observation(100.0), missing)["computable"]
        )


class ReplayReportTests(unittest.TestCase):
    def _dense_capture(self):
        rows = []
        for offset in range(-120, 121):
            price = 100.0 + max(0, offset) * 0.1
            rows.append(sample(T0 + offset, bybit_book(price, price + 1)))
        return write_capture(rows)

    def test_a_dense_capture_answers_every_horizon(self):
        report = replay_fixture(self._dense_capture())
        self.assertEqual(report["horizons_observed"], report["horizons_requested"])
        self.assertTrue(report["causal_replay_readiness"]["ready"])
        for horizon in report["horizons"]:
            with self.subTest(offset=horizon["offset_sec"]):
                self.assertTrue(horizon["exit_observation"]["observed"])
                self.assertTrue(horizon["gross_bbo_return"]["computable"])

    def test_exits_are_priced_against_the_bid_and_entries_against_the_ask(self):
        # Selling a long means hitting a bid. A mid price is not a price anyone trades at.
        report = replay_fixture(self._dense_capture())
        self.assertEqual(report["entry"]["observation"]["side"], "ask")
        self.assertTrue(all(
            horizon["exit_observation"]["side"] == "bid"
            for horizon in report["horizons"]
        ))
        self.assertIn("bid", report["method"])

    def test_a_sparse_capture_does_not_invent_brackets_or_returns(self):
        rows = [sample(T0 - 100, bybit_book(100, 101)),
                sample(T0 + 100, bybit_book(200, 201))]
        report = replay_fixture(write_capture(rows))
        for horizon in report["horizons"]:
            with self.subTest(offset=horizon["offset_sec"]):
                self.assertFalse(horizon["exit_observation"]["observed"])
                self.assertFalse(horizon["gross_bbo_return"]["computable"])
                self.assertIsNone(horizon["gross_bbo_return"]["value"])

    def test_a_capture_that_stopped_early_says_which_horizons_it_cannot_answer(self):
        rows = [sample(T0 + offset, bybit_book(100, 101)) for offset in range(-60, 11)]
        report = replay_fixture(write_capture(rows))
        answered = {
            h["offset_sec"] for h in report["horizons"]
            if h["gross_bbo_return"]["computable"]
        }
        self.assertIn(0, answered)
        self.assertIn(5, answered)
        self.assertNotIn(60, answered)
        missing = next(h for h in report["horizons"] if h["offset_sec"] == 60)
        self.assertEqual(
            missing["exit_observation"]["status"], "NO_SAMPLE_AT_OR_AFTER_TARGET"
        )

    def test_the_report_never_produces_an_acceptance_decision(self):
        report = replay_fixture(self._dense_capture())
        self.assertNotIn("acceptance_decision", report)
        self.assertEqual(report["research_classification"], "DESCRIPTIVE_ONLY")
        self.assertEqual(config.RISK_CONTRACT["acceptance_decision"], "NONE_CAPTURE_ONLY")

    def test_the_report_carries_the_captures_own_readiness_verdict(self):
        directory = write_capture(
            [sample(T0, bybit_book(100, 101))],
            readiness={"ready": False, "notes": ["only 3s of pre-t0 coverage"]},
        )
        report = replay_fixture(directory)
        self.assertFalse(report["capture_replay_readiness"]["ready"])

    def test_the_t0_source_class_travels_into_the_replay(self):
        report = replay_fixture(self._dense_capture())
        self.assertEqual(report["t0_source_class"], "OFFICIAL_ANNOUNCEMENT")

    def test_a_manifest_without_a_venue_or_t0_is_refused(self):
        directory = write_capture([sample(T0, bybit_book(100, 101))], t0_ts=0)
        with self.assertRaisesRegex(replay.ReplayError, "no venue or t0"):
            replay_fixture(directory)

    def test_the_human_report_shows_observed_points_not_intervals(self):
        text = replay.format_report(replay_fixture(self._dense_capture()))
        self.assertNotIn("..", text)
        self.assertIn("gross BBO return", text)
        self.assertIn("descriptive top-of-book observations only", text)


class OfflineTests(unittest.TestCase):
    def test_replay_never_reaches_the_network(self):
        # Imports and call sites, not bare words: a check that fails on a comment
        # saying "opens no socket" tests the prose rather than the code.
        source = (Path(__file__).resolve().parents[1] / "src/replay.py").read_text(
            encoding="utf-8"
        )
        for token in ("import socket", "import urllib", "import http",
                      "import public_http", "urlopen(", "get_json(", "requests."):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_replay_takes_no_claim_and_belongs_to_no_write_class(self):
        source = (Path(__file__).resolve().parents[1] / "src/replay.py").read_text(
            encoding="utf-8"
        )
        for token in ("claim_global_market_writer", "consume_capture_token",
                      "registry_lock", "append_entries"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_replay_is_bound_by_the_plan(self):
        self.assertEqual(dict(config.BOUND_RUNTIME_FILES)["replay"], "src/replay.py")


if __name__ == "__main__":
    unittest.main()
