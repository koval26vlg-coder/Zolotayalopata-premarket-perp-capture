"""Contract for an offline, temporary, no-authority operator rehearsal."""

from __future__ import annotations

import importlib
import importlib.util
import hashlib
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import project_config as config  # noqa: E402


def _path_snapshot(path: Path) -> object:
    if not path.exists():
        return None
    if path.is_file():
        return ("file", hashlib.sha256(path.read_bytes()).hexdigest())
    return (
        "dir",
        tuple(
            (
                item.relative_to(path).as_posix(),
                hashlib.sha256(item.read_bytes()).hexdigest(),
            )
            for item in sorted(path.rglob("*"))
            if item.is_file()
        ),
    )


class FixtureRehearsalTests(unittest.TestCase):
    @staticmethod
    def _run(rehearsal: object) -> dict[str, object]:
        with mock.patch.object(
            rehearsal.risk_gate, "load_and_verify_plan", return_value={}
        ) as plan_check, mock.patch.object(
            rehearsal.risk_gate,
            "run_capability_scan",
            return_value={"status": "CAPABILITY_SCAN_CLEAN"},
        ) as capability_scan:
            result = rehearsal.run_fixture_rehearsal(now_ts=2_000_000_000)
        plan_check.assert_called_once_with()
        capability_scan.assert_called_once_with()
        return result

    def test_fixed_visible_launcher_exists_without_capture_or_network_switches(self) -> None:
        launcher = ROOT / "tools/start_premarket_fixture_rehearsal.ps1"
        self.assertTrue(launcher.is_file())
        text = launcher.read_text(encoding="utf-8")
        self.assertIn("src\\fixture_rehearsal.py", text)
        self.assertIn("src\\risk_gate.py", text)
        self.assertIn("--plan-check", text)
        self.assertIn("--capability-scan", text)
        self.assertLess(text.index("--plan-check"), text.index("$runtime @runtimeArgs"))
        self.assertLess(text.index("--capability-scan"), text.index("$runtime @runtimeArgs"))
        self.assertIn("--json", text)
        self.assertNotIn("ScheduledTick", text)
        self.assertNotIn("capture-token", text.lower())

    def test_complete_rehearsal_uses_only_temporary_fixture_surfaces(self) -> None:
        spec = importlib.util.find_spec("fixture_rehearsal")
        self.assertIsNotNone(spec, "src/fixture_rehearsal.py is required")
        rehearsal = importlib.import_module("fixture_rehearsal")

        result = self._run(rehearsal)

        self.assertEqual(result["schema"], "premarket_fixture_rehearsal_v1")
        self.assertEqual(
            result["status"], "FIXTURE_REHEARSAL_COMPLETE_NO_CAPTURE"
        )
        self.assertEqual(
            result["stages"],
            {
                "candidate_alert": "FIXTURE_ALERT_PREVIEW_VALIDATED_NO_TOAST",
                "official_attestation": "FIXTURE_ATTESTATION_VALIDATED_NO_WRITE",
                "official_t0_arming": "ARMED_NO_CAPTURE_AUTHORITY",
                "event_bound_proposal": "FIXTURE_PROPOSAL_VALIDATED_NO_AUTHORITY",
            },
        )
        self.assertIs(result["network_used"], False)
        self.assertIs(result["toast_invoked"], False)
        self.assertIs(result["production_writes"], False)
        self.assertIs(result["temporary_workspace_removed"], True)
        self.assertIs(result["capture_authorized"], False)
        self.assertIs(result["capture_token_issued"], False)
        self.assertIs(result["orders_allowed"], False)
        self.assertIs(result["trust_root_rebound"], False)
        self.assertEqual(result["simulated_notifier_calls"], 1)
        self.assertEqual(result["temporary_artifact_counts"]["alert_ledger_records"], 2)
        self.assertRegex(result["rehearsal_hash"], r"^[0-9a-f]{64}$")

    def test_rehearsal_semantic_hash_is_deterministic(self) -> None:
        rehearsal = importlib.import_module("fixture_rehearsal")
        first = self._run(rehearsal)
        second = self._run(rehearsal)
        self.assertEqual(first["rehearsal_hash"], second["rehearsal_hash"])
        self.assertEqual(first["lineage"], second["lineage"])

    def test_direct_runtime_enforces_plan_and_capability_gate_before_rehearsal(self) -> None:
        rehearsal = importlib.import_module("fixture_rehearsal")
        self._run(rehearsal)

    def test_gate_failure_stops_before_temporary_workspace_creation(self) -> None:
        rehearsal = importlib.import_module("fixture_rehearsal")
        with mock.patch.object(
            rehearsal.risk_gate,
            "load_and_verify_plan",
            side_effect=RuntimeError("plan drift"),
        ), mock.patch.object(
            rehearsal.tempfile, "TemporaryDirectory"
        ) as temporary:
            with self.assertRaisesRegex(RuntimeError, "plan drift"):
                rehearsal.run_fixture_rehearsal(now_ts=2_000_000_000)
            temporary.assert_not_called()

        with mock.patch.object(
            rehearsal.risk_gate, "load_and_verify_plan", return_value={}
        ), mock.patch.object(
            rehearsal.risk_gate,
            "run_capability_scan",
            return_value={"status": "CAPABILITY_SCAN_FAILED"},
        ), mock.patch.object(
            rehearsal.tempfile, "TemporaryDirectory"
        ) as temporary:
            with self.assertRaisesRegex(
                rehearsal.FixtureRehearsalError, "capability preflight"
            ):
                rehearsal.run_fixture_rehearsal(now_ts=2_000_000_000)
            temporary.assert_not_called()

    def test_rehearsal_has_fail_fast_side_effect_guards_and_no_production_delta(self) -> None:
        rehearsal = importlib.import_module("fixture_rehearsal")
        production_paths = (
            config.ANNOUNCEMENT_CANDIDATE_PATH,
            config.CANDIDATE_ALERT_LEDGER_PATH,
            config.OFFICIAL_T0_ARMING_ROOT,
            config.EVENT_BOUND_PLAN_PROPOSAL_ROOT,
            config.CAPTURE_TOKEN_PATH,
        )
        before = {str(path): _path_snapshot(path) for path in production_paths}
        with mock.patch.object(
            rehearsal.discovery.public_http,
            "get_json",
            side_effect=AssertionError("fixture attempted network"),
        ) as http, mock.patch.object(
            rehearsal.candidate_alert.subprocess,
            "run",
            side_effect=AssertionError("fixture attempted toast subprocess"),
        ) as subprocess_run:
            result = self._run(rehearsal)
        http.assert_not_called()
        subprocess_run.assert_not_called()
        self.assertIs(result["temporary_workspace_removed"], True)
        after = {str(path): _path_snapshot(path) for path in production_paths}
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
