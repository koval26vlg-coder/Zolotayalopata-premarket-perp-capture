"""RED contract for bounded official-announcement discovery in PlanOnly v31.

Discovery is not attestation.  It may find an official article that looks relevant,
but it must never manufacture ``official_spot_t0`` or capture authority from index
metadata or a ticker match.
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

import announcement_candidate_store as candidate_store  # noqa: E402
import announcement_discovery as discovery  # noqa: E402
import event_registry as registry  # noqa: E402
import project_config as config  # noqa: E402
import public_http  # noqa: E402


def target(*, ticker: str = "ABC", episode_id: str = "episode-abc") -> dict:
    return {
        "episode_id": episode_id,
        "perpetual_venue": "bybit",
        "premarket_contract_id": f"{ticker}USDT",
        "lifecycle_generation": 0,
        "asset_class": registry.ASSET_CLASS_CRYPTO_TOKEN,
        "issuer_namespace": "crypto_asset",
        "issuer_id": ticker,
        "asset_identity_hash": "a" * 64,
        "registry_sha256": "b" * 64,
        "registry_tail_record_hash": "c" * 64,
        "mutation_receipt_seq": 4,
        "mutation_receipt_hash": "d" * 64,
        "summary_content_hash": "e" * 64,
        "registry_authority_state_hash": "f" * 64,
        "plan_id": "premarket_perp_capture_20260822_v31",
        "plan_hash": "1" * 64,
        "metadata_refresh_received_at": "2026-08-29T00:00:00Z",
    }


class TargetGateTests(unittest.TestCase):
    def test_stale_registry_blocks_before_any_announcement_request(self) -> None:
        with (
            mock.patch.object(
                registry,
                "select_unattested_crypto_premarket_episodes",
                return_value={
                    "status": "METADATA_REFRESH_REQUIRED",
                    "targets": [],
                    "capture_authorized": False,
                },
            ),
            mock.patch.object(public_http, "get_json") as get_json,
        ):
            result = discovery.run_discovery(now_ts=1_800_000_000)

        self.assertEqual(result["status"], "METADATA_REFRESH_REQUIRED")
        self.assertEqual(result["announcement_requests"], 0)
        self.assertIs(result["capture_authorized"], False)
        get_json.assert_not_called()

    def test_fresh_zero_target_registry_does_not_touch_network_or_store(self) -> None:
        temporary = Path(tempfile.mkdtemp())
        store_path = temporary / "candidates.jsonl"
        with (
            mock.patch.object(
                registry,
                "select_unattested_crypto_premarket_episodes",
                return_value={
                    "status": "NO_ANNOUNCEMENT_TARGETS",
                    "targets": [],
                    "capture_authorized": False,
                },
            ),
            mock.patch.object(public_http, "get_json") as get_json,
            mock.patch.object(config, "ANNOUNCEMENT_CANDIDATE_PATH", store_path),
        ):
            result = discovery.run_discovery(now_ts=1_800_000_000)

        self.assertEqual(result["status"], "NO_ANNOUNCEMENT_TARGETS")
        self.assertEqual(result["announcement_requests"], 0)
        self.assertFalse(store_path.exists())
        get_json.assert_not_called()

    def test_registry_recovery_required_is_not_reported_as_no_match(self) -> None:
        with (
            mock.patch.object(
                registry,
                "select_unattested_crypto_premarket_episodes",
                return_value={
                    "status": "REGISTRY_RECOVERY_REQUIRED",
                    "targets": [],
                    "capture_authorized": False,
                    "problems": ["broken receipt"],
                },
            ),
            mock.patch.object(public_http, "get_json") as get_json,
        ):
            result = discovery.run_discovery(now_ts=1_800_000_000)

        self.assertEqual(result["status"], "REGISTRY_RECOVERY_REQUIRED")
        self.assertNotEqual(result["status"], "NO_ANNOUNCEMENT_TARGETS")
        get_json.assert_not_called()


class ExactSymbolMatchTests(unittest.TestCase):
    def test_short_ticker_never_matches_inside_another_token(self) -> None:
        self.assertFalse(discovery.title_mentions_ticker("Binance Will List KAITO", "AI"))
        self.assertTrue(discovery.title_mentions_ticker("Binance Will List AI (AI)", "AI"))
        self.assertTrue(discovery.title_mentions_ticker("New ABC/USDT spot listing", "ABC"))

    def test_ticker_match_is_explicitly_not_asset_identity_proof(self) -> None:
        item = discovery.make_candidate(
            target=target(),
            listing_venue="kucoin",
            article={
                "article_id": "code-1",
                "title": "ABC (ABC) Gets Listed on KuCoin!",
                "url": "https://www.kucoin.com/announcement/en-abc-gets-listed",
                "published_at_ms": None,
                "source_page": 1,
                "source_payload_sha256": "2" * 64,
            },
            detected_at_utc="2026-08-29T00:00:00Z",
        )

        self.assertEqual(item["evidence_class"], "UNVERIFIED_ANNOUNCEMENT_DISCOVERY")
        self.assertEqual(item["identity_match_basis"], "EXACT_TICKER_TOKEN_HEURISTIC_ONLY")
        self.assertEqual(item["review_state"], "HUMAN_ATTESTATION_REQUIRED")
        for forbidden in ("official_spot_t0", "capture_eligible", "evidence_use"):
            self.assertNotIn(forbidden, item)


class VenuePayloadContractTests(unittest.TestCase):
    def test_bybit_page_requires_success_and_exact_list_shape(self) -> None:
        payload = {
            "retCode": 0,
            "result": {
                "list": [{
                    "title": "Bybit Will List ABC (ABC)",
                    "url": "https://announcements.bybit.com/en-US/article/abc-blt123/",
                    "publishTime": 1_800_000_000_000,
                    "type": {"key": "new_crypto", "title": "New Listings"},
                    "tags": ["Spot", "Spot Listings"],
                }],
                "total": 1,
            },
        }
        page = discovery.parse_bybit_page(payload, page=1)
        self.assertEqual(len(page.articles), 1)
        self.assertIsNone(page.next_page)
        with self.assertRaises(discovery.AnnouncementDiscoveryError):
            discovery.parse_bybit_page({"retCode": 0, "result": {"list": {}}}, page=1)

    def test_bitget_page_filters_spot_and_keeps_publication_time_only(self) -> None:
        payload = {
            "code": "00000",
            "msg": "success",
            "data": [{
                "annId": "123",
                "annTitle": "Bitget Will List ABC in the Innovation Zone",
                "annUrl": "https://www.bitget.com/support/articles/abc-listing",
                "cTime": "1800000000000",
                "annType": "coin_listings",
                "annSubType": "spot",
            }],
        }
        page = discovery.parse_bitget_page(payload, page=1)
        self.assertEqual(page.articles[0]["published_at_ms"], 1_800_000_000_000)
        self.assertNotIn("official_spot_t0", page.articles[0])

    def test_kucoin_page_requires_new_listing_class_and_official_path(self) -> None:
        payload = {
            "code": "200000",
            "data": {
                "items": [{
                    "annId": 321,
                    "annTitle": "ABC (ABC) Gets Listed on KuCoin!",
                    "annType": ["latest-announcements", "new-listings"],
                    "annUrl": "https://www.kucoin.com/announcement/en-abc-gets-listed",
                    "cTime": 1_800_000_000_000,
                }],
                "totalPage": 1,
                "currentPage": 1,
                "pageSize": 50,
            },
        }
        page = discovery.parse_kucoin_page(payload, page=1)
        self.assertEqual(
            page.articles[0]["url"],
            "https://www.kucoin.com/announcement/en-abc-gets-listed",
        )
        with self.assertRaises(discovery.AnnouncementDiscoveryError):
            discovery.parse_kucoin_page(
                {"code": "200000", "data": {
                    "items": [{
                        "annId": 1,
                        "annTitle": "ABC",
                        "annType": ["activities"],
                        "annUrl": "https://www.kucoin.com/announcement/en-abc",
                        "cTime": 1_800_000_000_000,
                    }],
                    "totalPage": 1,
                    "currentPage": 1,
                    "pageSize": 50,
                }},
                page=1,
            )

    def test_duplicate_article_across_pages_fails_closed(self) -> None:
        pages = [
            discovery.AnnouncementPage(
                articles=({"article_id": "same", "title": "ABC", "url": "https://x"},),
                next_page=2,
            ),
            discovery.AnnouncementPage(
                articles=({"article_id": "same", "title": "ABC again", "url": "https://x"},),
                next_page=None,
            ),
        ]
        with self.assertRaises(discovery.AnnouncementDiscoveryError):
            discovery.flatten_unique_pages(pages)


class CandidateStoreTests(unittest.TestCase):
    def test_candidate_identity_is_deterministic_and_content_revision_is_append_only(self) -> None:
        root = Path(tempfile.mkdtemp())
        path = root / "candidates.jsonl"
        first = discovery.make_candidate(
            target=target(),
            listing_venue="kucoin",
            article={
                "article_id": "same-code",
                "title": "ABC (ABC) Gets Listed on KuCoin!",
                "url": "https://www.kucoin.com/announcement/en-abc-gets-listed",
                "published_at_ms": None,
                "source_page": 1,
                "source_payload_sha256": "2" * 64,
            },
            detected_at_utc="2026-08-29T00:00:00Z",
        )
        duplicate = dict(first)
        changed = dict(first, article_title="Binance Will List ABC (updated)")

        one = candidate_store.append_candidates(path, [first], run_id="run-1")
        two = candidate_store.append_candidates(path, [duplicate], run_id="run-2")
        three = candidate_store.append_candidates(path, [changed], run_id="run-3")
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(one["appended_records"], 1)
        self.assertEqual(two["appended_records"], 0)
        self.assertEqual(three["appended_records"], 1)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["candidate_id"], records[1]["candidate_id"])
        self.assertEqual(records[1]["content_revision"], 1)
        self.assertEqual(records[1]["supersedes_record_hash"], records[0]["record_hash"])
        self.assertEqual(records[1]["previous_record_hash"], records[0]["record_hash"])

    def test_store_rejects_any_candidate_that_smuggles_official_authority(self) -> None:
        root = Path(tempfile.mkdtemp())
        path = root / "candidates.jsonl"
        bad = {
            "candidate_id": "x",
            "evidence_class": "UNVERIFIED_ANNOUNCEMENT_DISCOVERY",
            "official_spot_t0": 1_800_000_000,
        }
        with self.assertRaises(candidate_store.CandidateStoreError):
            candidate_store.append_candidates(path, [bad], run_id="bad")
        self.assertFalse(path.exists())

    def test_store_rejects_unknown_authority_aliases_and_nested_extensions(self) -> None:
        root = Path(tempfile.mkdtemp())
        valid = discovery.make_candidate(
            target=target(),
            listing_venue="kucoin",
            article={
                "article_id": "strict-schema",
                "title": "ABC (ABC) Gets Listed on KuCoin!",
                "url": "https://www.kucoin.com/announcement/en-abc-strict-schema",
                "published_at_ms": None,
                "source_page": 1,
                "source_payload_sha256": "2" * 64,
            },
            detected_at_utc="2026-08-29T00:00:00Z",
        )
        additions = {
            "capture_authorized": True,
            "official_t0": "2026-08-29T01:00:00Z",
            "same_underlying_identity_verified": True,
            "notes": {"capture_authorized": True},
        }
        for index, (field, value) in enumerate(additions.items()):
            with self.subTest(field=field):
                path = root / f"unknown-{index}.jsonl"
                bad = dict(valid)
                bad[field] = value
                with self.assertRaises(candidate_store.CandidateStoreError):
                    candidate_store.append_candidates(path, [bad], run_id=f"bad-{index}")
                self.assertFalse(path.exists())

    def test_store_enforces_fixed_unverified_boundary_fields(self) -> None:
        root = Path(tempfile.mkdtemp())
        valid = discovery.make_candidate(
            target=target(),
            listing_venue="kucoin",
            article={
                "article_id": "fixed-boundaries",
                "title": "ABC (ABC) Gets Listed on KuCoin!",
                "url": "https://www.kucoin.com/announcement/en-abc-fixed-boundaries",
                "published_at_ms": None,
                "source_page": 1,
                "source_payload_sha256": "2" * 64,
            },
            detected_at_utc="2026-08-29T00:00:00Z",
        )
        mutations = {
            "identity_authority": "SAME_UNDERLYING_VERIFIED",
            "article_body_fetched": True,
            "registry_write": True,
            "human_attestation_required": False,
        }
        for index, (field, value) in enumerate(mutations.items()):
            with self.subTest(field=field):
                path = root / f"boundary-{index}.jsonl"
                bad = dict(valid)
                bad[field] = value
                with self.assertRaises(candidate_store.CandidateStoreError):
                    candidate_store.append_candidates(path, [bad], run_id=f"bad-{index}")
                self.assertFalse(path.exists())

    def test_store_rechecks_article_url_against_the_listing_venue(self) -> None:
        root = Path(tempfile.mkdtemp())
        valid = discovery.make_candidate(
            target=target(),
            listing_venue="kucoin",
            article={
                "article_id": "official-host",
                "title": "ABC (ABC) Gets Listed on KuCoin!",
                "url": "https://www.kucoin.com/announcement/en-abc-official-host",
                "published_at_ms": None,
                "source_page": 1,
                "source_payload_sha256": "2" * 64,
            },
            detected_at_utc="2026-08-29T00:00:00Z",
        )
        invalid_urls = (
            "https://attacker.example/announcement/en-abc-official-host",
            "https://www.bitget.com/support/articles/abc-official-host",
            "https://www.kucoin.com/support/articles/abc-official-host",
        )
        for index, article_url in enumerate(invalid_urls):
            with self.subTest(article_url=article_url):
                path = root / f"url-{index}.jsonl"
                bad = dict(valid, article_url=article_url)
                with self.assertRaises(candidate_store.CandidateStoreError):
                    candidate_store.append_candidates(path, [bad], run_id=f"bad-{index}")
                self.assertFalse(path.exists())


class NetworkFailureSemanticsTests(unittest.TestCase):
    def test_partial_venue_failure_is_retry_not_negative_evidence(self) -> None:
        with (
            mock.patch.object(
                registry,
                "select_unattested_crypto_premarket_episodes",
                return_value={
                    "status": "TARGETS_READY",
                    "targets": [target()],
                    "capture_authorized": False,
                },
            ),
            mock.patch.object(discovery, "discover_venue") as discover_venue,
            mock.patch.object(
                discovery,
                "_preflight",
                return_value={
                    "plan_id": "premarket_perp_capture_20260822_v31",
                    "plan_hash": "1" * 64,
                    "resolved_paths_hash": "2" * 64,
                },
            ),
            mock.patch.object(candidate_store, "append_candidates", return_value={
                "appended_records": 1,
                "duplicate_records": 0,
            }),
        ):
            discover_venue.side_effect = [
                [{
                    "article_id": "abc",
                    "title": "Bybit Will List ABC",
                    "url": "https://announcements.bybit.com/en-US/article/abc-blt123/",
                    "published_at_ms": None,
                    "source_page": 1,
                    "source_payload_sha256": "2" * 64,
                }],
                discovery.AnnouncementDiscoveryError("OKX unavailable"),
                [],
            ]
            result = discovery.run_discovery(now_ts=1_800_000_000)

        self.assertEqual(result["status"], "PARTIAL_RETRY_NEXT_INTERVAL")
        self.assertEqual(result["appended_candidates"], 1)
        self.assertTrue(result["pending_retry"])
        self.assertIn("bitget", result["venue_errors"])


if __name__ == "__main__":
    unittest.main()
