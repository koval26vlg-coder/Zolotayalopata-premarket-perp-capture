from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import event_registry as registry  # noqa: E402


class AssetIdentityContractTests(unittest.TestCase):
    def test_same_ticker_crypto_and_equity_are_not_equivalent(self) -> None:
        crypto = registry.AssetIdentity(
            asset_class=registry.ASSET_CLASS_CRYPTO_TOKEN,
            issuer_namespace="crypto_asset",
            issuer_id="OPENAI",
            evidence_class=registry.IDENTITY_EVIDENCE_VENUE_EXPLICIT_METADATA,
        )
        equity = registry.AssetIdentity(
            asset_class=registry.ASSET_CLASS_EQUITY_ISSUER,
            issuer_namespace="legal_issuer",
            issuer_id="OPENAI",
            evidence_class=registry.IDENTITY_EVIDENCE_VENUE_EXPLICIT_METADATA,
        )

        self.assertFalse(registry.asset_identities_equivalent(crypto, equity))

    def test_same_ticker_different_issuer_ids_are_not_equivalent(self) -> None:
        left = registry.AssetIdentity(
            asset_class=registry.ASSET_CLASS_EQUITY_ISSUER,
            issuer_namespace="lei",
            issuer_id="ISSUER-A",
            evidence_class=registry.IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
        )
        right = registry.AssetIdentity(
            asset_class=registry.ASSET_CLASS_EQUITY_ISSUER,
            issuer_namespace="lei",
            issuer_id="ISSUER-B",
            evidence_class=registry.IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
        )

        self.assertFalse(registry.asset_identities_equivalent(left, right))

    def test_unclassified_identity_is_never_capture_equivalent(self) -> None:
        unknown = registry.AssetIdentity(
            asset_class=registry.ASSET_CLASS_UNCLASSIFIED,
            issuer_namespace="unresolved",
            issuer_id="OPENAI",
            evidence_class=registry.IDENTITY_EVIDENCE_LEGACY_UNCLASSIFIED,
        )

        self.assertFalse(registry.asset_identities_equivalent(unknown, unknown))

    def test_exact_known_identity_is_equivalent(self) -> None:
        left = registry.AssetIdentity(
            asset_class=registry.ASSET_CLASS_CRYPTO_TOKEN,
            issuer_namespace="crypto_asset",
            issuer_id="abc-token",
            evidence_class=registry.IDENTITY_EVIDENCE_VENUE_EXPLICIT_METADATA,
        )
        right = registry.AssetIdentity(
            asset_class=registry.ASSET_CLASS_CRYPTO_TOKEN,
            issuer_namespace="crypto_asset",
            issuer_id="abc-token",
            evidence_class=registry.IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
        )

        self.assertTrue(registry.asset_identities_equivalent(left, right))


class VenueAssetClassificationTests(unittest.TestCase):
    @staticmethod
    def adapter(venue: str):
        return next(item for item in registry.ADAPTERS if item.venue == venue)

    def test_bybit_stock_is_preipo_equity_not_crypto(self) -> None:
        identity = registry.classify_asset_identity(
            self.adapter("bybit"),
            {
                "symbol": "OPENAIUSDT",
                "baseCoin": "OPENAI",
                "symbolType": "stock",
            },
        )

        self.assertEqual(identity.asset_class, registry.ASSET_CLASS_EQUITY_ISSUER)

    def test_bybit_innovation_is_crypto(self) -> None:
        identity = registry.classify_asset_identity(
            self.adapter("bybit"),
            {
                "symbol": "ABCUSDT",
                "baseCoin": "ABC",
                "symbolType": "innovation",
            },
        )

        self.assertEqual(identity.asset_class, registry.ASSET_CLASS_CRYPTO_TOKEN)
        self.assertEqual(identity.issuer_id, "ABC")

    def test_bybit_missing_or_empty_symbol_type_is_unclassified(self) -> None:
        for row in (
            {"symbol": "OPENAIUSDT", "baseCoin": "OPENAI"},
            {
                "symbol": "OPENAIUSDT",
                "baseCoin": "OPENAI",
                "symbolType": "",
            },
        ):
            with self.subTest(row=row):
                identity = registry.classify_asset_identity(
                    self.adapter("bybit"), row
                )
                self.assertEqual(
                    identity.asset_class, registry.ASSET_CLASS_UNCLASSIFIED
                )
                self.assertEqual(
                    identity.evidence_class,
                    registry.IDENTITY_EVIDENCE_LEGACY_UNCLASSIFIED,
                )

    def test_okx_stock_category_is_preipo_equity(self) -> None:
        identity = registry.classify_asset_identity(
            self.adapter("okx"),
            {
                "instId": "OPENAI-USDT-SWAP",
                "uly": "OPENAI-USDT",
                "instCategory": "3",
            },
        )

        self.assertEqual(identity.asset_class, registry.ASSET_CLASS_EQUITY_ISSUER)

    def test_okx_crypto_category_is_crypto(self) -> None:
        identity = registry.classify_asset_identity(
            self.adapter("okx"),
            {
                "instId": "ABC-USDT-SWAP",
                "uly": "ABC-USDT",
                "instCategory": "1",
            },
        )

        self.assertEqual(identity.asset_class, registry.ASSET_CLASS_CRYPTO_TOKEN)

    def test_gate_stock_contract_is_preipo_equity(self) -> None:
        identity = registry.classify_asset_identity(
            self.adapter("gate"),
            {"name": "OPENAI_USDT", "contract_type": "stocks"},
        )

        self.assertEqual(identity.asset_class, registry.ASSET_CLASS_EQUITY_ISSUER)

    def test_gate_missing_contract_type_is_unclassified(self) -> None:
        identity = registry.classify_asset_identity(
            self.adapter("gate"),
            {"name": "OPENAI_USDT"},
        )

        self.assertEqual(identity.asset_class, registry.ASSET_CLASS_UNCLASSIFIED)


class ObservationAdmissionTests(unittest.TestCase):
    @staticmethod
    def identity(asset_class: str, issuer_id: str, *, evidence: str) -> registry.AssetIdentity:
        namespace = "crypto_asset" if asset_class == registry.ASSET_CLASS_CRYPTO_TOKEN else "legal_issuer"
        return registry.AssetIdentity(
            asset_class=asset_class,
            issuer_namespace=namespace,
            issuer_id=issuer_id,
            evidence_class=evidence,
        )

    def test_unclassified_official_observation_is_descriptive_only(self) -> None:
        observation = registry.make_timestamp_observation(
            episode_id=registry.make_episode_id("bybit", "OPENAIUSDT"),
            venue="bybit",
            premarket_contract_id="OPENAIUSDT",
            spot_symbol="OPENAIUSDT",
            timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            timestamp_ts=1_800_000_000,
            instrument_role="spot",
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="human_attestation:test",
            source_url="https://announcements.bybit.com/example",
            received_at_utc="2026-08-23T00:00:00Z",
            asset_identity=registry.AssetIdentity(
                asset_class=registry.ASSET_CLASS_UNCLASSIFIED,
                issuer_namespace="unresolved",
                issuer_id="OPENAI",
                evidence_class=registry.IDENTITY_EVIDENCE_LEGACY_UNCLASSIFIED,
            ),
        )

        self.assertEqual(observation["evidence_use"], "DESCRIPTIVE_ONLY")
        self.assertFalse(observation["capture_eligible"])

    def test_known_crypto_official_observation_can_be_an_anchor(self) -> None:
        observation = registry.make_timestamp_observation(
            episode_id=registry.make_episode_id("bybit", "ABCUSDT"),
            venue="bybit",
            premarket_contract_id="ABCUSDT",
            spot_symbol="ABCUSDT",
            timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            timestamp_ts=1_800_000_000,
            instrument_role="spot",
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="human_attestation:test",
            source_url="https://announcements.bybit.com/example",
            received_at_utc="2026-08-23T00:00:00Z",
            asset_identity=registry.AssetIdentity(
                asset_class=registry.ASSET_CLASS_CRYPTO_TOKEN,
                issuer_namespace="crypto_asset",
                issuer_id="ABC",
                evidence_class=registry.IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
            ),
        )

        self.assertEqual(observation["asset_class"], registry.ASSET_CLASS_CRYPTO_TOKEN)
        self.assertEqual(observation["issuer_id"], "ABC")
        self.assertEqual(observation["evidence_use"], "ACCEPTANCE_ANCHOR")
        self.assertTrue(observation["capture_eligible"])

    def test_materializer_requires_matching_metadata_and_official_crypto_identity(self) -> None:
        episode_id = registry.make_episode_id("bybit", "ABCUSDT")
        metadata = registry.make_timestamp_observation(
            episode_id=episode_id,
            venue="bybit",
            premarket_contract_id="ABCUSDT",
            spot_symbol=None,
            timestamp_kind=registry.TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
            timestamp_ts=1_799_000_000,
            instrument_role="premarket_perp",
            source_class=registry.SOURCE_VENUE_INSTRUMENT_METADATA,
            source_identity="bybit:metadata:launchTime",
            source_url="https://api.bybit.com/v5/market/instruments-info",
            received_at_utc="2026-08-23T00:00:00Z",
            asset_identity=self.identity(
                registry.ASSET_CLASS_CRYPTO_TOKEN,
                "ABC",
                evidence=registry.IDENTITY_EVIDENCE_VENUE_EXPLICIT_METADATA,
            ),
        )
        official = registry.make_timestamp_observation(
            episode_id=episode_id,
            venue="bybit",
            premarket_contract_id="ABCUSDT",
            spot_symbol="ABCUSDT",
            timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            timestamp_ts=1_800_000_000,
            instrument_role="spot",
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="human_attestation:test",
            source_url="https://announcements.bybit.com/example",
            received_at_utc="2026-08-23T00:01:00Z",
            asset_identity=self.identity(
                registry.ASSET_CLASS_CRYPTO_TOKEN,
                "ABC",
                evidence=registry.IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
            ),
        )
        # v31 makes the spot-listing venue explicit even for a same-venue anchor;
        # this test is about asset identity, not legacy role inference.
        official["listing_venue"] = "bybit"

        episode = registry.materialize_episodes([metadata, official])[0]

        self.assertFalse(episode["asset_identity_conflict"])
        self.assertEqual(episode["asset_class"], registry.ASSET_CLASS_CRYPTO_TOKEN)
        self.assertTrue(episode["capture_eligible"])

    def test_materializer_blocks_same_ticker_cross_asset_binding(self) -> None:
        episode_id = registry.make_episode_id("bybit", "OPENAIUSDT")
        metadata = registry.make_timestamp_observation(
            episode_id=episode_id,
            venue="bybit",
            premarket_contract_id="OPENAIUSDT",
            spot_symbol=None,
            timestamp_kind=registry.TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
            timestamp_ts=1_799_000_000,
            instrument_role="premarket_perp",
            source_class=registry.SOURCE_VENUE_INSTRUMENT_METADATA,
            source_identity="bybit:metadata:launchTime",
            source_url="https://api.bybit.com/v5/market/instruments-info",
            received_at_utc="2026-08-23T00:00:00Z",
            asset_identity=self.identity(
                registry.ASSET_CLASS_EQUITY_ISSUER,
                "OPENAI",
                evidence=registry.IDENTITY_EVIDENCE_VENUE_EXPLICIT_METADATA,
            ),
        )
        official = registry.make_timestamp_observation(
            episode_id=episode_id,
            venue="bybit",
            premarket_contract_id="OPENAIUSDT",
            spot_symbol="OPENAIUSDT",
            timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            timestamp_ts=1_800_000_000,
            instrument_role="spot",
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="human_attestation:test",
            source_url="https://announcements.bybit.com/example",
            received_at_utc="2026-08-23T00:01:00Z",
            asset_identity=self.identity(
                registry.ASSET_CLASS_CRYPTO_TOKEN,
                "OPENAI",
                evidence=registry.IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
            ),
        )

        episode = registry.materialize_episodes([metadata, official])[0]

        self.assertTrue(episode["asset_identity_conflict"])
        self.assertFalse(episode["capture_eligible"])
        self.assertEqual(episode["evidence_use"], "DESCRIPTIVE_ONLY")

    def test_legacy_registry_rows_remain_byte_identical_and_project_unclassified(self) -> None:
        path = registry.REGISTRY_V2_PATH
        before = path.read_bytes()
        rows = [json.loads(line) for line in before.splitlines() if line.strip()]

        projected = [registry.project_legacy_asset_identity(row) for row in rows]

        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(projected)
        self.assertTrue(
            all(item["asset_class"] == registry.ASSET_CLASS_UNCLASSIFIED for item in projected)
        )
        self.assertTrue(all(item["capture_eligible"] is False for item in projected))


if __name__ == "__main__":
    unittest.main()
