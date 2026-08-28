from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import paper_replay


def no_candidate_report() -> dict:
    return {
        "status": "NO_SECONDS_GRADE_CANDIDATE",
        "capture_authorized": False,
        "registry": {
            "status": "REGISTRY_OK",
            "registry_sha256": "1" * 64,
            "entries": 18,
        },
        "candidates": [],
        "rejections": [],
    }


class PaperReplayModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(
            hasattr(paper_replay, "paper_tick_from_candidate_report"),
            "paper replay must expose the fail-closed tick boundary",
        )

    def test_paper_replay_runtime_exists_as_a_separate_module(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("paper_replay"),
            "v30 requires a separately bound offline paper runtime",
        )

    def test_no_candidate_creates_no_virtual_position_or_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output_dir = Path(raw) / "paper"
            result = paper_replay.paper_tick_from_candidate_report(
                no_candidate_report(), output_dir=output_dir
            )

            self.assertEqual(result["status"], "NO_ELIGIBLE_EVENT")
            self.assertEqual(result["virtual_positions_created"], 0)
            self.assertFalse(result["acceptance_capable"])
            self.assertFalse(result["paper_broker_execution"])
            self.assertFalse(output_dir.exists())

    def test_model_parameters_are_fixed_and_not_caller_overridable(self) -> None:
        result = paper_replay.paper_tick_from_candidate_report(
            no_candidate_report(), output_dir=Path("unused")
        )

        self.assertEqual(
            result["fixed_model"],
            {
                "direction": "LONG",
                "virtual_notional_usdt": 25,
                "leverage_equivalent": 1,
                "entry_lead_sec": 60,
                "exit_offsets_sec": [0, 5, 15, 60],
                "execution_style": "TAKER_LIKE_CAUSAL_DEPTH",
            },
        )

    def test_no_event_result_hash_is_deterministic_and_self_verifying(self) -> None:
        first = paper_replay.paper_tick_from_candidate_report(
            no_candidate_report(), output_dir=Path("unused")
        )
        second = paper_replay.paper_tick_from_candidate_report(
            no_candidate_report(), output_dir=Path("unused")
        )

        self.assertEqual(first, second)
        material = dict(first)
        claimed = material.pop("result_hash")
        self.assertEqual(claimed, paper_replay.canonical_result_hash(material))
        json.dumps(first, allow_nan=False)

    def test_candidate_without_sealed_capture_does_not_invent_a_trade(self) -> None:
        candidate_report = no_candidate_report()
        candidate_report.update(
            {
                "status": "CANDIDATE_READY",
                "candidates": [
                    {
                        "episode_id": "ep_" + "2" * 64,
                        "venue": "bybit",
                        "asset_class": "CRYPTO_TOKEN",
                        "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
                        "t0_precision_sec": 1,
                    }
                ],
            }
        )

        result = paper_replay.paper_tick_from_candidate_report(
            candidate_report,
            output_dir=Path("unused"),
            sealed_capture_ids=(),
        )

        self.assertEqual(result["status"], "PAPER_NOT_RUN_NO_CAPTURE_EVIDENCE")
        self.assertEqual(result["virtual_positions_created"], 0)
        self.assertIsNone(result["net_pnl_usdt"])
        self.assertFalse(result["acceptance_capable"])


if __name__ == "__main__":
    unittest.main()
