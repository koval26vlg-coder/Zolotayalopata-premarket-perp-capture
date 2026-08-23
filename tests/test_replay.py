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


def sample(exchange_ts, payload, *, probe="orderbook", error=None):
    row = {
        "schema": "premarket_perp_capture_sample_v1",
        "probe": probe,
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
        "capture_id": "test_capture",
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


class IntegrityTests(unittest.TestCase):
    def test_a_capture_replays_when_its_bytes_match_its_manifest(self):
        directory = write_capture([sample(T0, bybit_book(100, 101))])
        manifest, samples = replay.load_capture(directory)
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
            replay.load_capture(directory)

    def test_a_missing_manifest_or_samples_file_is_refused(self):
        directory = Path(tempfile.mkdtemp())
        with self.assertRaises(replay.ReplayError):
            replay.load_capture(directory)


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

    def test_the_series_is_indexed_by_the_venue_clock_not_ours(self):
        rows = [
            sample(T0 + 5, bybit_book(102, 103)),
            sample(T0 + 1, bybit_book(100, 101)),
        ]
        series = replay.book_series(rows, "bybit")
        self.assertEqual([item.exchange_ts for item in series], [T0 + 1, T0 + 5])


class BracketTests(unittest.TestCase):
    def _series(self, stamps):
        return [replay.BookSample(t, 100.0 + i, 1.0, 101.0 + i, 1.0)
                for i, t in enumerate(stamps)]

    def test_a_horizon_between_two_samples_is_straddled(self):
        bracket = replay.bracket_at(self._series([T0 - 1, T0 + 1]), T0)
        self.assertTrue(bracket.straddled)
        self.assertEqual(bracket.gap_before_sec, 1.0)
        self.assertEqual(bracket.gap_after_sec, 1.0)

    def test_a_wide_bracket_is_straddled_but_not_tight(self):
        wide = replay.BRACKET_TOLERANCE_SEC + 1
        bracket = replay.bracket_at(self._series([T0 - wide, T0 + wide]), T0)
        self.assertTrue(bracket.straddled)
        self.assertFalse(bracket.tight)

    def test_a_horizon_after_the_last_sample_has_no_sample_after_it(self):
        bracket = replay.bracket_at(self._series([T0 - 5, T0 - 1]), T0)
        self.assertFalse(bracket.straddled)
        self.assertIsNotNone(bracket.before)
        self.assertIsNone(bracket.after)


class BoundTests(unittest.TestCase):
    def test_a_price_between_two_samples_is_an_interval_not_a_number(self):
        series = [replay.BookSample(T0 - 1, 100.0, 1, 101.0, 1),
                  replay.BookSample(T0 + 1, 110.0, 1, 111.0, 1)]
        bound = replay.bound_from_bracket(replay.bracket_at(series, T0), "bid")
        self.assertTrue(bound.observed)
        self.assertEqual((bound.low, bound.high), (100.0, 110.0))

    def test_a_horizon_the_capture_never_reached_is_not_computable(self):
        series = [replay.BookSample(T0 - 5, 100.0, 1, 101.0, 1)]
        bound = replay.bound_from_bracket(replay.bracket_at(series, T0 + 60), "bid")
        self.assertFalse(bound.observed)
        self.assertIn("capture ended before", bound.note)

    def test_a_horizon_before_the_capture_started_is_not_computable(self):
        series = [replay.BookSample(T0 + 5, 100.0, 1, 101.0, 1)]
        bound = replay.bound_from_bracket(replay.bracket_at(series, T0 - 60), "ask")
        self.assertFalse(bound.observed)
        self.assertIn("capture began after", bound.note)

    def test_no_samples_at_all_yields_no_bound(self):
        bound = replay.bound_from_bracket(replay.bracket_at([], T0), "bid")
        self.assertFalse(bound.observed)


class ReturnArithmeticTests(unittest.TestCase):
    def _bound(self, low, high):
        return replay.PriceBound(low=low, high=high, observed=True)

    def test_the_worst_case_is_the_lowest_exit_against_the_highest_entry(self):
        result = replay.bounded_return(self._bound(100.0, 110.0),
                                       self._bound(120.0, 130.0))
        self.assertAlmostEqual(result["low"], 120.0 / 110.0 - 1, places=6)
        self.assertAlmostEqual(result["high"], 130.0 / 100.0 - 1, places=6)

    def test_an_interval_may_straddle_zero_and_that_is_the_answer(self):
        # When the data cannot distinguish profit from loss, saying so is the result.
        result = replay.bounded_return(self._bound(100.0, 110.0),
                                       self._bound(99.0, 115.0))
        self.assertLess(result["low"], 0)
        self.assertGreater(result["high"], 0)

    def test_an_unobserved_leg_makes_the_return_uncomputable(self):
        missing = replay.PriceBound(low=None, high=None, observed=False)
        self.assertFalse(replay.bounded_return(self._bound(1.0, 2.0), missing)["computable"])
        self.assertFalse(replay.bounded_return(missing, self._bound(1.0, 2.0))["computable"])


class ReplayReportTests(unittest.TestCase):
    def _dense_capture(self):
        rows = []
        for offset in range(-120, 121):
            price = 100.0 + max(0, offset) * 0.1
            rows.append(sample(T0 + offset, bybit_book(price, price + 1)))
        return write_capture(rows)

    def test_a_dense_capture_answers_every_horizon(self):
        report = replay.replay_capture(self._dense_capture())
        self.assertEqual(report["horizons_computable"], report["horizons_requested"])
        for horizon in report["horizons"]:
            with self.subTest(offset=horizon["offset_sec"]):
                self.assertTrue(horizon["well_observed"])

    def test_exits_are_priced_against_the_bid_and_entries_against_the_ask(self):
        # Selling a long means hitting a bid. A mid price is not a price anyone trades at.
        report = replay.replay_capture(self._dense_capture())
        self.assertEqual(report["entry"]["side"], "ask")
        self.assertIn("bid", report["method"])

    def test_a_sparse_capture_reports_wide_brackets_rather_than_hiding_them(self):
        rows = [sample(T0 - 100, bybit_book(100, 101)),
                sample(T0 + 100, bybit_book(200, 201))]
        report = replay.replay_capture(write_capture(rows))
        for horizon in report["horizons"]:
            with self.subTest(offset=horizon["offset_sec"]):
                self.assertFalse(horizon["well_observed"])
                self.assertTrue(horizon["return"]["computable"])
                span = horizon["return"]["high"] - horizon["return"]["low"]
                self.assertGreater(span, 0.5)

    def test_a_capture_that_stopped_early_says_which_horizons_it_cannot_answer(self):
        rows = [sample(T0 + offset, bybit_book(100, 101)) for offset in range(-60, 11)]
        report = replay.replay_capture(write_capture(rows))
        answered = {h["offset_sec"] for h in report["horizons"] if h["return"]["computable"]}
        self.assertIn(0, answered)
        self.assertIn(5, answered)
        self.assertNotIn(60, answered)
        missing = next(h for h in report["horizons"] if h["offset_sec"] == 60)
        self.assertIn("capture ended before", missing["exit_price"]["note"])

    def test_the_report_never_produces_an_acceptance_decision(self):
        report = replay.replay_capture(self._dense_capture())
        self.assertEqual(report["acceptance_decision"], "NONE_REPLAY_IS_DESCRIPTIVE")
        self.assertEqual(config.RISK_CONTRACT["acceptance_decision"], "NONE_CAPTURE_ONLY")

    def test_the_report_carries_the_captures_own_readiness_verdict(self):
        directory = write_capture(
            [sample(T0, bybit_book(100, 101))],
            readiness={"ready": False, "notes": ["only 3s of pre-t0 coverage"]},
        )
        report = replay.replay_capture(directory)
        self.assertFalse(report["capture_replay_readiness"]["ready"])

    def test_the_t0_source_class_travels_into_the_replay(self):
        report = replay.replay_capture(self._dense_capture())
        self.assertEqual(report["t0_source_class"], "OFFICIAL_ANNOUNCEMENT")

    def test_a_manifest_without_a_venue_or_t0_is_refused(self):
        directory = write_capture([sample(T0, bybit_book(100, 101))], t0_ts=0)
        with self.assertRaisesRegex(replay.ReplayError, "no venue or t0"):
            replay.replay_capture(directory)

    def test_the_human_report_shows_intervals_not_single_numbers(self):
        text = replay.format_report(replay.replay_capture(self._dense_capture()))
        self.assertIn("..", text)
        self.assertIn("no acceptance decision", text)


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
