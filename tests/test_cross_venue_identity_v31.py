"""RED contract: a cross-venue ticker match is not same-underlying evidence."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import event_registry as registry  # noqa: E402
import official_attestation as attestation  # noqa: E402
import project_config as config  # noqa: E402


ANNOUNCED = "2027-01-15T04:00:00Z"
QUOTED_TIME = "Jan 15, 2027, 4:00AM UTC"
QUOTE = f"Spot trading for KII/USDT will start on {QUOTED_TIME}."
IDENTITY_QUOTE = "Kite AI (KII) is the asset being listed for spot trading."


def fields() -> dict:
    return {
        "venue": "bybit",
        "listing_venue": "binance",
        "spot_symbol": "KIIUSDT",
        "premarket_contract_id": "KIIUSDT",
        "lifecycle_generation": 0,
        "announced_utc": ANNOUNCED,
        "announcement_url": (
            "https://www.binance.com/en/support/announcement/abc123"
        ),
        "quoted_sentence": QUOTE,
        "quoted_time_text": QUOTED_TIME,
        "quoted_symbol_text": "KII/USDT",
        "attested_by": "koval",
        "now_ts": attestation.parse_announced_utc(ANNOUNCED) - 7 * 24 * 3600,
        "asset_identity": registry.AssetIdentity(
            asset_class=registry.ASSET_CLASS_CRYPTO_TOKEN,
            issuer_namespace="crypto_asset",
            issuer_id="KII",
            evidence_class=registry.IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
        ),
        "same_underlying_decision": "SAME_UNDERLYING",
        "quoted_identity_sentence": IDENTITY_QUOTE,
        "quoted_underlying_text": "Kite AI (KII)",
    }


class CrossVenueIdentityEvidenceTests(unittest.TestCase):
    def test_v31_schema_is_new_and_cross_venue_record_carries_bound_identity(self) -> None:
        self.assertEqual(
            config.OFFICIAL_ATTESTATION_SCHEMA,
            "premarket_perp_official_attestation_v3",
        )
        observation = attestation.build_attestation(**fields())
        nested = observation["attestation"]["underlying_identity_attestation"]
        self.assertEqual(nested["decision"], "SAME_UNDERLYING")
        self.assertEqual(nested["perpetual_episode_id"], observation["episode_id"])
        self.assertEqual(nested["perpetual_asset_class"], "CRYPTO_TOKEN")
        self.assertEqual(nested["perpetual_issuer_id"], "KII")
        self.assertEqual(
            nested["perpetual_asset_identity_hash"],
            observation["asset_identity_hash"],
        )
        self.assertEqual(nested["listing_venue"], "binance")
        self.assertEqual(nested["announcement_url"], observation["source_url"])

    def test_every_cross_venue_human_identity_field_is_mandatory(self) -> None:
        for name in (
            "same_underlying_decision",
            "quoted_identity_sentence",
            "quoted_underlying_text",
        ):
            case = fields()
            case[name] = None
            with self.subTest(field=name), self.assertRaises(
                attestation.AttestationError
            ):
                attestation.build_attestation(**case)

    def test_ticker_only_identity_quote_is_rejected(self) -> None:
        case = fields()
        case["quoted_identity_sentence"] = "KII/USDT will be listed."
        case["quoted_underlying_text"] = "KII/USDT"
        with self.assertRaisesRegex(
            attestation.AttestationError, "richer than the market ticker"
        ):
            attestation.build_attestation(**case)

    def test_short_ticker_cannot_match_inside_another_underlying(self) -> None:
        case = fields()
        case["spot_symbol"] = "AIUSDT"
        case["premarket_contract_id"] = "AIUSDT"
        case["asset_identity"] = registry.AssetIdentity(
            asset_class=registry.ASSET_CLASS_CRYPTO_TOKEN,
            issuer_namespace="crypto_asset",
            issuer_id="AI",
            evidence_class=registry.IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
        )
        case["quoted_symbol_text"] = "AI/USDT"
        case["quoted_sentence"] = (
            f"Spot trading for AI/USDT will start on {QUOTED_TIME}."
        )
        case["quoted_identity_sentence"] = "KAITO is the asset being listed."
        case["quoted_underlying_text"] = "KAITO"
        with self.assertRaisesRegex(
            attestation.AttestationError, "exact underlying ticker token"
        ):
            attestation.build_attestation(**case)

    def test_same_venue_api_remains_simple(self) -> None:
        case = fields()
        case.update({
            "listing_venue": "bybit",
            "announcement_url": (
                "https://announcements.bybit.com/en-US/article/kii-blt123/"
            ),
        })
        for name in (
            "same_underlying_decision",
            "quoted_identity_sentence",
            "quoted_underlying_text",
        ):
            case.pop(name)
        observation = attestation.build_attestation(**case)
        self.assertNotIn(
            "underlying_identity_attestation", observation["attestation"]
        )


if __name__ == "__main__":
    unittest.main()
