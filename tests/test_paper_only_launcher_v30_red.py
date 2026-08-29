from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "tools" / "start_premarket_perp_paper_only_visible.ps1"
PAPER_CLI = ROOT / "src" / "paper_replay.py"


def no_candidate_report() -> dict[str, object]:
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


class PaperOnlyLauncherNoCaptureTests(unittest.TestCase):
    def launcher_text(self) -> str:
        self.assertTrue(
            LAUNCHER.is_file(),
            "visible paper-only launcher has not been implemented",
        )
        return LAUNCHER.read_text(encoding="utf-8")

    def test_launcher_uses_the_active_v36_plan_and_never_delegates_to_legacy_v5(self) -> None:
        text = self.launcher_text()
        lowered = text.lower()

        self.assertNotIn(
            "premarket-perp-listing-impulse-planonly-20260825-v5.json",
            lowered,
        )
        self.assertNotIn("trading_mvp\\src\\premarket_automation.py", lowered)
        self.assertIn("premarket-perp-capture-planonly-20260822-v36.json", lowered)
        self.assertIn("src\\paper_replay.py", lowered)

    def test_launcher_contains_no_private_or_order_endpoint_markers(self) -> None:
        lowered = self.launcher_text().lower()
        forbidden_markers = (
            "/v5/order",
            "/api/v5/trade/",
            "/futures/usdt/orders",
            "/private/",
            "x-bapi-sign",
            "ok-access-sign",
            "api_key",
            "api_secret",
            "passphrase",
            "hmac",
        )

        for marker in forbidden_markers:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, lowered)

    def test_no_candidate_branch_precedes_any_capture_side_effect(self) -> None:
        lowered = self.launcher_text().lower()
        candidate_check = lowered.find("--candidate-status")
        no_event = lowered.find("no_eligible_event", candidate_check)
        side_effect_offsets = [
            offset
            for marker in (
                "market_data_capture",
                "capture-token",
                "start-process",
            )
            if (offset := lowered.find(marker)) >= 0
        ]

        self.assertGreaterEqual(candidate_check, 0, "launcher must inspect candidates")
        self.assertGreater(
            no_event,
            candidate_check,
            "launcher must have an explicit no-event terminal branch",
        )
        self.assertTrue(
            side_effect_offsets,
            "launcher must expose a separately guarded visible capture path",
        )
        self.assertLess(
            no_event,
            min(side_effect_offsets),
            "NO_ELIGIBLE_EVENT must be decided before token, claim, or visible capture",
        )

    def test_zero_candidate_cli_returns_without_claim_token_capture_or_output(self) -> None:
        self.assertTrue(PAPER_CLI.is_file(), "paper replay CLI module is missing")
        with tempfile.TemporaryDirectory() as raw:
            temp_root = Path(raw)
            candidate_path = temp_root / "candidate-report.json"
            candidate_path.write_text(
                json.dumps(no_candidate_report(), ensure_ascii=False),
                encoding="utf-8",
            )
            claim_path = temp_root / "writer-claim.json"
            token_path = temp_root / "capture-token.json"
            capture_root = temp_root / "captures"
            output_root = temp_root / "paper-output"
            env = os.environ.copy()
            env.update(
                {
                    "PREMARKET_CAPTURE_WRITER_CLAIM_PATH": str(claim_path),
                    "PREMARKET_CAPTURE_TOKEN_PATH": str(token_path),
                    "PREMARKET_CAPTURE_ROOT": str(capture_root),
                    "PREMARKET_PAPER_OUTPUT_ROOT": str(output_root),
                }
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(PAPER_CLI),
                    "--candidate-report",
                    str(candidate_path),
                    "--output-dir",
                    str(output_root),
                    "--json",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(completed.stdout.strip(), "paper CLI returned no JSON")
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "NO_ELIGIBLE_EVENT")
            self.assertEqual(payload["virtual_positions_created"], 0)
            self.assertFalse(payload["capture_started"])
            self.assertFalse(payload["paper_broker_execution"])
            self.assertFalse(claim_path.exists())
            self.assertFalse(token_path.exists())
            self.assertFalse(capture_root.exists())
            self.assertFalse(output_root.exists())


if __name__ == "__main__":
    unittest.main()
