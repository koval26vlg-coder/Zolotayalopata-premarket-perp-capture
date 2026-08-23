"""An official t0 is read by a person, and the record carries what they read.

Measured on 2026-08-23: no venue publishes the official spot listing moment as a
machine-readable field, and one Bybit spot-listing article carried twenty-four time
expressions. These tests pin the consequences - the source must be the venue's own
announcement, the sentence must ride with the record, the precision must match what an
announcement actually offers, and a moment already too close to capture is refused.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import event_registry as registry  # noqa: E402
import official_attestation as attestation  # noqa: E402
import project_config as config  # noqa: E402


NOW = 1_800_000_000
T0 = NOW + 7 * 24 * 3600
ANNOUNCED = "2027-01-15T04:00:00Z"
QUOTE = "Spot trading for KII/USDT will start on Sep 9, 2026, 4:00AM UTC."
URL = "https://announcements.bybit.com/en-US/article/bybit-to-list-kii-on-spot/"


def build(**overrides):
    fields = {
        "venue": "bybit",
        "spot_symbol": "KIIUSDT",
        "premarket_contract_id": "KIIUSDT",
        "announced_utc": ANNOUNCED,
        "announcement_url": URL,
        "quoted_sentence": QUOTE,
        "attested_by": "koval",
        "now_ts": attestation.parse_announced_utc(ANNOUNCED) - 7 * 24 * 3600,
    }
    fields.update(overrides)
    return attestation.build_attestation(**fields)


class TimeParsingTests(unittest.TestCase):
    def test_an_explicit_utc_instant_is_accepted(self):
        self.assertEqual(
            attestation.parse_announced_utc("2026-09-09T04:00:00Z"), 1788926400
        )

    def test_a_naive_timestamp_is_refused(self):
        # "Sep 9, 4:00" in whose timezone? The whole defect this module exists for.
        with self.assertRaises(attestation.AttestationError):
            attestation.parse_announced_utc("2026-09-09T04:00:00")

    def test_a_non_utc_offset_is_refused(self):
        with self.assertRaises(attestation.AttestationError):
            attestation.parse_announced_utc("2026-09-09T04:00:00+08:00")

    def test_prose_is_not_parsed_leniently(self):
        # A lenient parser would hide exactly the ambiguity worth surfacing.
        for text in ("Sep 9, 2026, 4:00AM UTC", "tomorrow", "2026-09-09 4AM"):
            with self.subTest(text=text):
                with self.assertRaises(attestation.AttestationError):
                    attestation.parse_announced_utc(text)


class SourceTests(unittest.TestCase):
    def test_the_venues_own_announcement_host_is_accepted(self):
        self.assertTrue(attestation.require_official_source("bybit", URL))

    def test_an_aggregator_or_forum_is_refused(self):
        for url in ("https://coinmarketcal.com/en/event/kii-listing",
                    "https://twitter.com/Bybit_Official/status/1",
                    "https://announcements.bybit.com.evil.example/article/x"):
            with self.subTest(url=url):
                with self.assertRaises(attestation.AttestationError):
                    attestation.require_official_source("bybit", url)

    def test_http_is_refused(self):
        with self.assertRaises(attestation.AttestationError):
            attestation.require_official_source(
                "bybit", "http://announcements.bybit.com/en-US/article/x"
            )

    def test_one_venues_host_does_not_vouch_for_another(self):
        with self.assertRaises(attestation.AttestationError):
            attestation.require_official_source("okx", URL)

    def test_the_recorded_url_drops_any_fragment(self):
        cleaned = attestation.require_official_source("bybit", URL + "#section-2")
        self.assertNotIn("#", cleaned)


class QuotationTests(unittest.TestCase):
    def test_the_sentence_is_required(self):
        with self.assertRaises(attestation.AttestationError):
            build(quoted_sentence="")

    def test_a_token_gesture_at_a_sentence_is_refused(self):
        with self.assertRaises(attestation.AttestationError):
            build(quoted_sentence="4:00AM UTC")

    def test_a_sentence_with_no_time_at_all_is_refused(self):
        with self.assertRaises(attestation.AttestationError):
            build(quoted_sentence="Bybit is excited to announce the listing of KII.")

    def test_the_sentence_rides_with_the_record(self):
        # Twenty-four time expressions in one article: a reviewer needs the line.
        observation = build()
        self.assertEqual(observation["attestation"]["quoted_sentence"], QUOTE)
        self.assertEqual(observation["attestation"]["announcement_url"], URL)


class ObservationTests(unittest.TestCase):
    def test_the_record_is_an_official_announcement_of_a_spot_t0(self):
        observation = build()
        self.assertEqual(
            observation["t0_source_class"], registry.SOURCE_OFFICIAL_ANNOUNCEMENT
        )
        self.assertEqual(
            observation["timestamp_kind"], registry.TIMESTAMP_OFFICIAL_SPOT_T0
        )
        self.assertEqual(observation["instrument_role"], "spot")

    def test_precision_matches_what_an_announcement_actually_offers(self):
        # Announcements say "4:00AM UTC". Claiming second precision would be a lie
        # about the source, not a detail.
        self.assertEqual(build()["t0_precision_sec"], 60)
        self.assertEqual(attestation.ANNOUNCED_PRECISION_SEC, 60)

    def test_the_record_says_a_person_read_it(self):
        observation = build()
        self.assertIn("human_attestation:koval", observation["source_identity"])
        self.assertIn(
            "OFFICIAL_T0_READ_BY_A_PERSON_FROM_ANNOUNCEMENT_PROSE",
            observation["caveats"],
        )

    def test_an_anonymous_attestation_is_refused(self):
        with self.assertRaises(attestation.AttestationError):
            build(attested_by="  ")

    def test_a_spot_symbol_is_required(self):
        with self.assertRaises(attestation.AttestationError):
            build(spot_symbol="")


class LeadTimeTests(unittest.TestCase):
    def test_a_moment_that_has_already_passed_cannot_anchor_a_capture(self):
        t0 = attestation.parse_announced_utc(ANNOUNCED)
        with self.assertRaisesRegex(attestation.AttestationError, "could not cover"):
            build(now_ts=t0 + 60)

    def test_a_moment_too_close_to_cover_the_window_is_refused(self):
        t0 = attestation.parse_announced_utc(ANNOUNCED)
        with self.assertRaises(attestation.AttestationError):
            build(now_ts=t0 - config.CAPTURE_WINDOW_BEFORE_SEC + 60)

    def test_exactly_enough_lead_is_accepted(self):
        t0 = attestation.parse_announced_utc(ANNOUNCED)
        observation = build(now_ts=t0 - config.CAPTURE_WINDOW_BEFORE_SEC)
        self.assertEqual(
            observation["attestation"]["lead_sec_at_attestation"],
            config.CAPTURE_WINDOW_BEFORE_SEC,
        )

    def test_the_lead_is_the_full_capture_window(self):
        self.assertEqual(attestation.MIN_LEAD_SEC, config.CAPTURE_WINDOW_BEFORE_SEC)


class WritePathTests(unittest.TestCase):
    def test_an_attestation_is_a_write_and_needs_a_verified_preflight(self):
        with mock.patch.object(attestation.risk_gate, "preflight",
                               return_value={"ok": True, "verified": False}):
            with self.assertRaisesRegex(attestation.AttestationError, "PREFLIGHT_BLOCKED"):
                attestation.attest(
                    run_id="r1", venue="bybit", spot_symbol="KIIUSDT",
                    premarket_contract_id="KIIUSDT", announced_utc=ANNOUNCED,
                    announcement_url=URL, quoted_sentence=QUOTE, attested_by="koval",
                )

    def test_a_blocked_preflight_never_reaches_the_registry(self):
        with mock.patch.object(attestation.risk_gate, "preflight",
                               side_effect=RuntimeError("gate closed")), \
             mock.patch.object(attestation.registry, "append_entries") as appended:
            with self.assertRaises(attestation.AttestationError):
                attestation.attest(
                    run_id="r1", venue="bybit", spot_symbol="KIIUSDT",
                    premarket_contract_id="KIIUSDT", announced_utc=ANNOUNCED,
                    announcement_url=URL, quoted_sentence=QUOTE, attested_by="koval",
                )
        appended.assert_not_called()

    def test_a_run_id_is_required(self):
        with self.assertRaises(attestation.AttestationError):
            attestation.attest(
                run_id="", venue="bybit", spot_symbol="KIIUSDT",
                premarket_contract_id="KIIUSDT", announced_utc=ANNOUNCED,
                announcement_url=URL, quoted_sentence=QUOTE, attested_by="koval",
            )


class ProvenanceTests(unittest.TestCase):
    def test_this_module_never_fetches_anything(self):
        source = (Path(__file__).resolve().parents[1]
                  / "src/official_attestation.py").read_text(encoding="utf-8")
        for forbidden in ("urlopen", "get_json", "public_http", "requests."):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, source)

    def test_the_module_is_bound_by_the_plan(self):
        self.assertEqual(
            dict(config.BOUND_RUNTIME_FILES)["official_attestation"],
            "src/official_attestation.py",
        )

    def test_why_explains_the_measurement_rather_than_asserting_a_policy(self):
        import contextlib
        import io
        import json as json_module
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(attestation.main(["--why"]), 0)
        payload = json_module.loads(buffer.getvalue())
        self.assertFalse(payload["machine_readable_official_spot_t0_available"])
        self.assertIn("24 time expressions", payload["ambiguity"])


if __name__ == "__main__":
    unittest.main()
