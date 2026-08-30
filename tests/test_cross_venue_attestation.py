"""Who lists the underlying and who trades the perpetual are different questions.

Until 2026-08-24 one answer served both: the announcement had to come from the venue
that traded the perp. A token whose perpetual sits on Bybit may well be spot-listed on
Binance or Upbit, and that listing is the catalyst the hypothesis is about - so the old
rule would have refused precisely the announcement worth attesting.

Widening whose announcement counts is a decision about trust, and these tests pin its
edges: an exchange's own announcement domain counts, an aggregator never does, one
venue's domain does not vouch for another, and none of it widens where this project
reaches for market data.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for search_path in (ROOT / "src", ROOT / "tests"):
    if str(search_path) not in sys.path:
        sys.path.insert(0, str(search_path))

import event_registry as registry  # noqa: E402
import official_attestation as attestation  # noqa: E402
import project_config as config  # noqa: E402
import public_http  # noqa: E402
import test_activation_hardening_v6_registry as v6  # noqa: E402
import test_activation_hardening_v7_registry as v7  # noqa: E402


ANNOUNCED = "2027-01-15T04:00:00Z"
QUOTED_TIME = "Jan 15, 2027, 4:00AM UTC"
QUOTED_SYMBOL = "KII/USDT"
QUOTE = f"Spot trading for {QUOTED_SYMBOL} will start on {QUOTED_TIME}."


def build(**overrides):
    fields = {
        "venue": "bybit",
        "listing_venue": "bybit",
        "spot_symbol": "KIIUSDT",
        "premarket_contract_id": "KIIUSDT",
        "lifecycle_generation": 0,
        "announced_utc": ANNOUNCED,
        "announcement_url":
            "https://announcements.bybit.com/en-US/article/bybit-to-list-kii-on-spot/",
        "quoted_sentence": QUOTE,
        "quoted_time_text": QUOTED_TIME,
        "quoted_symbol_text": QUOTED_SYMBOL,
        "attested_by": "koval",
        "now_ts": attestation.parse_announced_utc(ANNOUNCED) - 7 * 24 * 3600,
    }
    fields.update(overrides)
    listing_venue = fields.get("listing_venue") or fields["venue"]
    if listing_venue != fields["venue"]:
        fields.setdefault(
            "asset_identity",
            registry.AssetIdentity(
                asset_class=registry.ASSET_CLASS_CRYPTO_TOKEN,
                issuer_namespace="crypto_asset",
                issuer_id="KII",
                evidence_class=registry.IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
            ),
        )
        fields.setdefault("same_underlying_decision", "SAME_UNDERLYING")
        fields.setdefault(
            "quoted_identity_sentence",
            "Kite AI (KII) is the asset being listed for spot trading.",
        )
        fields.setdefault("quoted_underlying_text", "Kite AI (KII)")
    return attestation.build_attestation(**fields)


class RoleSeparationTests(unittest.TestCase):
    def test_a_binance_listing_can_anchor_a_bybit_perpetual(self):
        # The case the old rule refused.
        observation = build(
            listing_venue="binance",
            announcement_url="https://www.binance.com/en/support/announcement/abc123",
        )
        self.assertEqual(observation["listing_venue"], "binance")
        self.assertEqual(observation["venue"], "bybit")

    def test_both_roles_ride_with_the_record(self):
        observation = build(
            listing_venue="upbit",
            announcement_url="https://upbit.com/service_center/notice?id=1",
        )
        self.assertEqual(observation["attestation"]["listing_venue"], "upbit")
        self.assertEqual(observation["attestation"]["perpetual_venue"], "bybit")

    def test_real_attest_preserves_binance_listing_venue_through_locked_commit(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        path = root / "registry.jsonl"
        records = registry.build_stream_revisions([], [v6._metadata_observation()])
        v6._write_records(path, records)
        run_id = "cross-venue-binance-bybit"

        with mock.patch.object(
            registry,
            "active_registry_contract_hash",
            return_value="b" * 64,
        ):
            preflight = v7._valid_attestation_preflight(run_id)
            v6._write_refresh_summary(
                path,
                records,
                last_complete_metadata_refresh_received_at=v6.RECEIVED_AT,
            )
            with mock.patch.object(
                attestation.risk_gate, "preflight", return_value=preflight
            ), mock.patch.object(
                attestation.time,
                "time",
                return_value=v6._t0_ts() - 7 * 24 * 3600,
            ):
                result = attestation.attest(
                    path=path,
                    run_id=run_id,
                    venue="bybit",
                    listing_venue="binance",
                    spot_symbol=v6.SPOT_SYMBOL,
                    premarket_contract_id=v6.CONTRACT_ID,
                    lifecycle_generation=v6.GENERATION,
                    announced_utc=v6.ANNOUNCED_UTC,
                    announcement_url=(
                        "https://www.binance.com/en/support/announcement/abc123"
                    ),
                    quoted_sentence=v6.QUOTE,
                    quoted_time_text=v6.QUOTED_TIME,
                    quoted_symbol_text=v6.QUOTED_SYMBOL,
                    attested_by="cross-venue-test",
                    same_underlying_decision="SAME_UNDERLYING",
                    quoted_identity_sentence=(
                        "Kite AI (KII) is the asset being listed for spot trading."
                    ),
                    quoted_underlying_text="Kite AI (KII)",
                )

        self.assertEqual(result["status"], "ATTESTED")
        official = registry.load_registry(path)[-1]
        self.assertEqual(official["listing_venue"], "binance")
        self.assertEqual(official["venue"], "bybit")
        self.assertEqual(official["attestation"]["listing_venue"], "binance")
        self.assertEqual(official["attestation"]["perpetual_venue"], "bybit")
        episode = registry.materialize_episodes(registry.load_registry(path))[0]
        self.assertEqual(episode["listing_venue"], "binance")
        self.assertEqual(
            episode["official_t0_provenance"]["listing_venue"], "binance"
        )

    def test_the_listing_venue_defaults_to_the_perpetual_venue(self):
        # The same-exchange case must stay as simple as it was.
        observation = build(listing_venue=None)
        self.assertEqual(observation["listing_venue"], "bybit")

    def test_an_unknown_listing_venue_is_refused(self):
        with self.assertRaisesRegex(attestation.AttestationError, "unknown listing venue"):
            build(listing_venue="some-exchange")

    def test_the_perpetual_venue_stays_narrow(self):
        # Widening who may announce must not widen who we capture from.
        self.assertEqual(sorted(config.PERP_VENUES), ["bybit", "gate", "okx"])
        with self.assertRaisesRegex(attestation.AttestationError, "unknown perpetual venue"):
            build(venue="binance", listing_venue="binance",
                  announcement_url="https://www.binance.com/en/support/announcement/x")


class TrustEdgeTests(unittest.TestCase):
    def test_an_aggregator_is_refused_for_every_listing_venue(self):
        for listing_venue in config.OFFICIAL_ANNOUNCEMENT_HOSTS:
            for url in ("https://coinmarketcal.com/en/event/x",
                        "https://twitter.com/binance/status/1",
                        "https://cryptonews.example/listing"):
                with self.subTest(listing_venue=listing_venue, url=url):
                    with self.assertRaises(attestation.AttestationError):
                        attestation.require_official_source(listing_venue, url)

    def test_one_venues_domain_does_not_vouch_for_another(self):
        with self.assertRaises(attestation.AttestationError):
            attestation.require_official_source(
                "binance", "https://announcements.bybit.com/en-US/article/x/"
            )
        with self.assertRaises(attestation.AttestationError):
            attestation.require_official_source(
                "bybit", "https://www.binance.com/en/support/announcement/x"
            )

    def test_a_lookalike_domain_is_refused(self):
        for url in ("https://www.binance.com.evil.example/en/support/announcement/x",
                    "https://binance.com.attacker.test/x",
                    "https://www.binance.co/en/support/announcement/x"):
            with self.subTest(url=url):
                with self.assertRaises(attestation.AttestationError):
                    attestation.require_official_source("binance", url)

    def test_every_declared_host_is_https_only(self):
        for listing_venue, hosts in config.OFFICIAL_ANNOUNCEMENT_HOSTS.items():
            for host in hosts:
                with self.subTest(host=host):
                    with self.assertRaises(attestation.AttestationError):
                        attestation.require_official_source(
                            listing_venue, f"http://{host}/article"
                        )

    def test_the_declared_hosts_are_venue_domains_not_free_text(self):
        for listing_venue, hosts in config.OFFICIAL_ANNOUNCEMENT_HOSTS.items():
            self.assertTrue(hosts, f"{listing_venue} declares no host")
            for host in hosts:
                with self.subTest(host=host):
                    self.assertEqual(host, host.lower().strip())
                    self.assertNotIn("/", host)
                    self.assertIn(".", host)


class ReachIsUnchangedTests(unittest.TestCase):
    """Trusting an announcement is not permission to contact anyone."""

    def test_no_newly_trusted_venue_became_a_market_data_endpoint(self):
        # www.okx.com is legitimately both: OKX serves its API and its announcements
        # from one host, and did so before this widening. The property that matters is
        # that trusting Binance, Bitget, KuCoin or Upbit to announce gave them no
        # place in the endpoint list.
        endpoint_hosts = {
            host for host, _path in config.MARKET_DATA_ALLOWED_ENDPOINTS
        }
        newly_trusted = {
            host
            for venue, hosts in config.OFFICIAL_ANNOUNCEMENT_HOSTS.items()
            if venue not in config.PERP_VENUES
            for host in hosts
        }
        self.assertTrue(newly_trusted, "the widening added no venue at all")
        self.assertEqual(endpoint_hosts & newly_trusted, set())

    def test_the_market_data_allow_list_names_three_venues_plus_gate_archive(self):
        self.assertEqual(
            sorted({host for host, _ in config.MARKET_DATA_ALLOWED_ENDPOINTS}),
            ["api.bybit.com", "api.gateio.ws", "download.gatedata.org", "www.okx.com"],
        )

    def test_an_announcement_host_is_not_reachable_through_public_http(self):
        for url in ("https://www.binance.com/en/support/announcement/x",
                    "https://upbit.com/service_center/notice",
                    "https://www.kucoin.com/announcement"):
            with self.subTest(url=url):
                self.assertFalse(public_http.endpoint_is_allowed(url))

    def test_only_exact_documented_announcement_indexes_are_reachable(self):
        for host, path in config.ANNOUNCEMENT_ALLOWED_ENDPOINTS:
            with self.subTest(host=host, path=path):
                self.assertTrue(public_http.endpoint_is_allowed(f"https://{host}{path}"))
        self.assertFalse(
            public_http.endpoint_is_allowed(
                "https://www.kucoin.com/announcement/en-some-listing"
            )
        )

    def test_the_attestation_module_still_fetches_nothing(self):
        source = (ROOT / "src/official_attestation.py").read_text(encoding="utf-8")
        for token in ("urlopen", "get_json(", "requests."):
            with self.subTest(token=token):
                self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
