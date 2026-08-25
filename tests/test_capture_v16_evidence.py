"""Capture v16 keeps causal input readiness separate from strategy acceptance."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import capture  # noqa: E402
import event_registry as registry  # noqa: E402


class CaptureEvidenceBoundaryTests(unittest.TestCase):
    def test_bybit_probe_rejects_boolean_and_float_success_codes(self) -> None:
        for ret_code in (False, 0.0):
            with self.subTest(ret_code=ret_code):
                payload = {
                    "retCode": ret_code,
                    "result": {
                        "s": "NEWUSDT",
                        "b": [["10", "1"]],
                        "a": [["11", "1"]],
                        "ts": 1_800_000_000_000,
                    },
                }
                with self.assertRaisesRegex(
                    capture.CaptureError, "successful retCode"
                ):
                    capture.validate_probe_payload(
                        "bybit",
                        "orderbook",
                        payload,
                        "NEWUSDT",
                        received_ts=1_800_000_000,
                    )

    def test_job_binds_contract_spot_and_explicit_crypto_identity(self) -> None:
        event = {
            "episode_id": "bybit:NEWUSDT:g0",
            "venue": "bybit",
            "listing_venue": "bybit",
            "symbol": "NEWUSDT",
            "premarket_contract_id": "NEWUSDT",
            "spot_symbol": "NEWUSDT",
            "official_spot_t0": 1_800_000_000,
            "t0_source_class": registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            "t0_precision_sec": 1,
            "asset_class": registry.ASSET_CLASS_CRYPTO_TOKEN,
            "issuer_namespace": "crypto_asset",
            "issuer_id": "NEW",
            "asset_identity_hash": "a" * 64,
        }
        job = capture.job_from_event(event, capture_id="capture-v16-test")
        self.assertEqual(job.lineage["venue"], "bybit")
        self.assertEqual(job.lineage["listing_venue"], "bybit")
        self.assertEqual(job.lineage["premarket_contract_id"], "NEWUSDT")
        self.assertEqual(job.lineage["spot_symbol"], "NEWUSDT")
        self.assertEqual(job.lineage["official_spot_t0"], 1_800_000_000)
        self.assertEqual(job.lineage["t0_precision_sec"], 1)
        self.assertEqual(job.lineage["asset_class"], registry.ASSET_CLASS_CRYPTO_TOKEN)
        self.assertEqual(job.lineage["issuer_namespace"], "crypto_asset")
        self.assertEqual(job.lineage["issuer_id"], "NEW")
        self.assertEqual(job.lineage["asset_identity_hash"], "a" * 64)

    def test_ready_capture_is_only_a_causal_replay_input(self) -> None:
        classification = capture.capture_evidence_classification({"ready": True})
        self.assertEqual(
            classification["evidence_class"], "CAUSAL_REPLAY_INPUT_READY"
        )
        self.assertFalse(classification["acceptance_capable"])

    def test_incomplete_capture_stays_descriptive_only(self) -> None:
        classification = capture.capture_evidence_classification({"ready": False})
        self.assertEqual(classification["evidence_class"], "DESCRIPTIVE_ONLY")
        self.assertFalse(classification["acceptance_capable"])


if __name__ == "__main__":
    unittest.main()
