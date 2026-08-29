"""Write-class-aware preflight contracts for registry refresh and capture."""

from __future__ import annotations

import sys
import inspect
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import risk_gate  # noqa: E402


NO_CAPTURE_STATUS = "AWAIT_CAPTURE_IMPLEMENTATION_AUDIT_NO_CAPTURE"
METADATA_REGISTRY_ACTION = (
    "refresh the public metadata event registry after metadata preflight"
)
OFFLINE_DESCRIPTIVE_ACTION = (
    "verify and materialize descriptive proxy observations offline"
)
OFFICIAL_ATTESTATION_ACTION = (
    "append one human-verified official spot t0 after attestation preflight"
)


def no_capture_plan() -> dict[str, object]:
    return {
        "plan_id": "plan_v2",
        "plan_hash": "abc123",
        "status": NO_CAPTURE_STATUS,
        "authorized_after_gate_green": [
            METADATA_REGISTRY_ACTION,
            OFFLINE_DESCRIPTIVE_ACTION,
            OFFICIAL_ATTESTATION_ACTION,
        ],
        "resolved_path_bindings": risk_gate.resolved_path_bindings(),
    }


class ResolvedPathBindingTests(unittest.TestCase):
    def test_exact_plan_bindings_are_accepted(self) -> None:
        self.assertTrue(hasattr(risk_gate, "verify_resolved_path_bindings"))
        expected = {
            "shared_gate_path": str(risk_gate.config.SHARED_GATE_PATH.resolve(strict=False)),
            "shared_writer_claim_path": str(
                risk_gate.config.SHARED_WRITER_CLAIM_PATH.resolve(strict=False)
            ),
            "capture_root": str(risk_gate.config.CAPTURE_ROOT.resolve(strict=False)),
            "registry_quarantine_root": str(
                risk_gate.config.REGISTRY_QUARANTINE_ROOT.resolve(strict=False)
            ),
            "announcement_state_path": str(
                risk_gate.config.ANNOUNCEMENT_STATE_PATH.resolve(strict=False)
            ),
            "announcement_attempts_path": str(
                risk_gate.config.ANNOUNCEMENT_ATTEMPTS_PATH.resolve(strict=False)
            ),
            "announcement_watch_claim_path": str(
                risk_gate.config.ANNOUNCEMENT_WATCH_CLAIM_PATH.resolve(strict=False)
            ),
            "announcement_watch_claim_archive": str(
                risk_gate.config.ANNOUNCEMENT_WATCH_CLAIM_ARCHIVE.resolve(strict=False)
            ),
            "candidate_alert_ledger_path": str(
                risk_gate.config.CANDIDATE_ALERT_LEDGER_PATH.resolve(strict=False)
            ),
            "official_t0_arming_root": str(
                risk_gate.config.OFFICIAL_T0_ARMING_ROOT.resolve(strict=False)
            ),
            "event_bound_plan_proposal_root": str(
                risk_gate.config.EVENT_BOUND_PLAN_PROPOSAL_ROOT.resolve(strict=False)
            ),
            "windows_powershell_executable": str(
                Path(risk_gate.config.WINDOWS_POWERSHELL_EXECUTABLE).resolve(
                    strict=False
                )
            ),
        }
        self.assertEqual(
            risk_gate.verify_resolved_path_bindings(
                {"resolved_path_bindings": expected}
            ),
            expected,
        )

    def test_environment_resolved_path_drift_is_rejected(self) -> None:
        approved = risk_gate.resolved_path_bindings()
        with mock.patch.object(
            risk_gate.config,
            "SHARED_GATE_PATH",
            Path("C:/unexpected/fake-gate.ps1"),
        ):
            try:
                risk_gate.verify_resolved_path_bindings(
                    {"resolved_path_bindings": approved}
                )
            except Exception as exc:  # asserted below so the RED state is a failure
                self.assertIsInstance(exc, risk_gate.RiskGateError)
                self.assertIn("resolved path bindings", str(exc))
            else:
                self.fail("environment-resolved gate path drift was accepted")

    def test_preflight_blocks_a_plan_bound_to_different_control_paths(self) -> None:
        mismatched = risk_gate.resolved_path_bindings()
        mismatched["shared_gate_path"] = "C:\\unexpected\\fake-gate.ps1"
        plan = {
            "plan_id": "plan_v2",
            "plan_hash": "abc123",
            "resolved_path_bindings": mismatched,
        }
        with (
            mock.patch.object(risk_gate, "load_and_verify_plan", return_value=plan),
            mock.patch.object(
                risk_gate,
                "run_capability_scan",
                return_value={"status": "CAPABILITY_SCAN_CLEAN"},
            ),
            mock.patch.object(
                risk_gate,
                "read_shared_gate",
                return_value={"open": True, "status": "READY_FOR_POSTPROCESS"},
            ),
        ):
            result = risk_gate.preflight(
                write_class="metadata_registry",
                run_id="registry_wrong_paths",
            )

        self.assertFalse(result["verified"])
        self.assertEqual([item["source"] for item in result["blockers"]], ["plan"])


class MetadataRegistryPreflightTests(unittest.TestCase):
    def test_preflight_requires_an_explicit_write_class(self) -> None:
        parameter = inspect.signature(risk_gate.preflight).parameters.get("write_class")
        self.assertIsNotNone(parameter)
        self.assertEqual(parameter.default, inspect.Parameter.empty)

    def test_preflight_has_no_market_token_bypass_argument(self) -> None:
        self.assertNotIn("mint", inspect.signature(risk_gate.preflight).parameters)

    def test_unknown_write_class_fails_closed_before_any_checks(self) -> None:
        with mock.patch.object(risk_gate, "load_and_verify_plan") as plan_check:
            try:
                risk_gate.preflight(write_class="other", run_id="bad_1")
            except Exception as exc:  # asserted below so the RED state is a failure
                self.assertIsInstance(exc, risk_gate.RiskGateError)
                self.assertIn("unknown write class", str(exc))
            else:
                self.fail("unknown write class was accepted")
        plan_check.assert_not_called()

    def test_empty_run_id_fails_closed_before_any_checks(self) -> None:
        with mock.patch.object(risk_gate, "load_and_verify_plan") as plan_check:
            try:
                risk_gate.preflight(write_class="metadata_registry", run_id="  ")
            except Exception as exc:  # asserted below so the RED state is a failure
                self.assertIsInstance(exc, risk_gate.RiskGateError)
                self.assertIn("run_id", str(exc))
            else:
                self.fail("empty run_id was accepted")
        plan_check.assert_not_called()

    def test_malformed_plan_verification_result_does_not_become_verified(self) -> None:
        with (
            mock.patch.object(risk_gate, "load_and_verify_plan", return_value={}),
            mock.patch.object(
                risk_gate,
                "run_capability_scan",
                return_value={"status": "CAPABILITY_SCAN_CLEAN"},
            ),
            mock.patch.object(
                risk_gate,
                "read_shared_gate",
                return_value={"open": True, "status": "READY_FOR_POSTPROCESS"},
            ),
        ):
            result = risk_gate.preflight(
                write_class="metadata_registry",
                run_id="registry_bad_plan",
            )

        self.assertFalse(result["verified"])
        self.assertEqual([item["source"] for item in result["blockers"]], ["plan"])

    def test_non_clean_capability_result_does_not_become_verified(self) -> None:
        with (
            mock.patch.object(
                risk_gate,
                "load_and_verify_plan",
                return_value=no_capture_plan(),
            ),
            mock.patch.object(
                risk_gate,
                "run_capability_scan",
                return_value={"status": "UNKNOWN"},
            ),
            mock.patch.object(
                risk_gate,
                "read_shared_gate",
                return_value={"open": True, "status": "READY_FOR_POSTPROCESS"},
            ),
        ):
            result = risk_gate.preflight(
                write_class="metadata_registry",
                run_id="registry_bad_scan",
            )

        self.assertFalse(result["verified"])
        self.assertEqual(
            [item["source"] for item in result["blockers"]],
            ["capability_scan"],
        )

    @mock.patch.object(risk_gate, "mint_capture_token")
    @mock.patch.object(risk_gate, "inspect_run_record")
    @mock.patch.object(risk_gate, "inspect_claim")
    @mock.patch.object(
        risk_gate,
        "read_shared_gate",
        return_value={"open": True, "status": "READY_FOR_POSTPROCESS"},
    )
    @mock.patch.object(
        risk_gate,
        "run_capability_scan",
        return_value={"status": "CAPABILITY_SCAN_CLEAN"},
    )
    @mock.patch.object(
        risk_gate,
        "load_and_verify_plan",
        return_value=no_capture_plan(),
    )
    def test_metadata_preflight_skips_market_writer_claim_and_capture_token(
        self,
        plan_check: mock.Mock,
        scan: mock.Mock,
        gate: mock.Mock,
        inspect_claim: mock.Mock,
        inspect_run_record: mock.Mock,
        mint_token: mock.Mock,
    ) -> None:
        result = risk_gate.preflight(
            write_class="metadata_registry",
            run_id="registry_refresh_1",
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["write_class"], "metadata_registry")
        self.assertEqual(result["decision"], "ALLOW_METADATA_REGISTRY")
        self.assertNotIn("capture_token", result)
        inspect_claim.assert_not_called()
        inspect_run_record.assert_not_called()
        mint_token.assert_not_called()
        plan_check.assert_called_once_with()
        scan.assert_called_once_with()
        gate.assert_called_once_with()

    @mock.patch.object(risk_gate, "mint_capture_token")
    @mock.patch.object(risk_gate, "inspect_run_record")
    @mock.patch.object(risk_gate, "inspect_claim")
    @mock.patch.object(
        risk_gate,
        "read_shared_gate",
        return_value={"open": True, "status": "READY_FOR_POSTPROCESS"},
    )
    @mock.patch.object(
        risk_gate,
        "run_capability_scan",
        return_value={"status": "CAPABILITY_SCAN_CLEAN"},
    )
    @mock.patch.object(
        risk_gate,
        "load_and_verify_plan",
        return_value=no_capture_plan(),
    )
    def test_success_receipt_binds_the_resolved_control_paths(
        self,
        _plan: mock.Mock,
        _scan: mock.Mock,
        _gate: mock.Mock,
        _claim: mock.Mock,
        _run_record: mock.Mock,
        _mint: mock.Mock,
    ) -> None:
        result = risk_gate.preflight(
            write_class="metadata_registry",
            run_id="registry_refresh_2",
        )

        self.assertIn("resolved_paths", result)
        paths = result["resolved_paths"]
        self.assertTrue(all(Path(value).is_absolute() for value in paths.values()))
        self.assertEqual(result["resolved_paths_hash"], risk_gate.canonical_hash(paths))

    @mock.patch.object(risk_gate, "mint_capture_token")
    @mock.patch.object(
        risk_gate,
        "read_shared_gate",
        return_value={"open": True, "status": "READY_FOR_POSTPROCESS"},
    )
    @mock.patch.object(
        risk_gate,
        "run_capability_scan",
        return_value={"status": "CAPABILITY_SCAN_CLEAN"},
    )
    def test_unknown_plan_action_fails_closed(
        self,
        _scan: mock.Mock,
        _gate: mock.Mock,
        mint_token: mock.Mock,
    ) -> None:
        plan = no_capture_plan()
        plan["authorized_after_gate_green"] = [
            METADATA_REGISTRY_ACTION,
            OFFLINE_DESCRIPTIVE_ACTION,
            "perform an unregistered future action",
        ]
        with mock.patch.object(risk_gate, "load_and_verify_plan", return_value=plan):
            result = risk_gate.preflight(
                write_class="metadata_registry",
                run_id="registry_unknown_action",
            )

        self.assertFalse(result["verified"])
        self.assertEqual(result["decision"], "BLOCK")
        self.assertEqual([item["source"] for item in result["blockers"]], ["plan"])
        mint_token.assert_not_called()


class PreflightCliTests(unittest.TestCase):
    def test_cli_passes_the_explicit_write_class_to_preflight(self) -> None:
        allowed = {"ok": True, "decision": "ALLOW_METADATA_REGISTRY"}
        with (
            mock.patch.object(risk_gate, "preflight", return_value=allowed) as call,
            mock.patch("builtins.print"),
        ):
            try:
                exit_code = risk_gate.main([
                    "--preflight",
                    "--write-class", "metadata_registry",
                    "--run-id", "registry_cli_1",
                ])
            except SystemExit as exc:
                self.fail(f"write-class-aware CLI was rejected: {exc}")

        self.assertEqual(exit_code, 0)
        call.assert_called_once_with(
            write_class="metadata_registry",
            run_id="registry_cli_1",
        )


class MarketDataCapturePreflightTests(unittest.TestCase):
    @mock.patch.object(
        risk_gate,
        "mint_capture_token",
        return_value={"token": "one-shot", "expires_at_ts": 12345},
    )
    @mock.patch.object(
        risk_gate,
        "inspect_run_record",
        return_value={"present": False, "blocks": False, "stale": False},
    )
    @mock.patch.object(
        risk_gate,
        "inspect_claim",
        return_value={"present": False, "blocks": False, "stale": False},
    )
    @mock.patch.object(
        risk_gate,
        "read_shared_gate",
        return_value={"open": True, "status": "READY_FOR_POSTPROCESS"},
    )
    @mock.patch.object(
        risk_gate,
        "run_capability_scan",
        return_value={"status": "CAPABILITY_SCAN_CLEAN"},
    )
    @mock.patch.object(
        risk_gate,
        "load_and_verify_plan",
        return_value=no_capture_plan(),
    )
    def test_capture_is_blocked_when_plan_does_not_authorize_it_and_mints_no_token(
        self,
        _plan: mock.Mock,
        _scan: mock.Mock,
        _gate: mock.Mock,
        inspect_claim: mock.Mock,
        inspect_run_record: mock.Mock,
        mint_token: mock.Mock,
    ) -> None:
        result = risk_gate.preflight(
            write_class="market_data_capture",
            run_id="capture_1",
            event_id="episode-capture-1",
            source_class="OFFICIAL_ANNOUNCEMENT",
        )

        self.assertFalse(result["verified"])
        self.assertEqual(result["decision"], "BLOCK")
        self.assertNotIn("capture_token", result)
        self.assertEqual([item["source"] for item in result["blockers"]], ["plan"])
        inspect_claim.assert_called_once_with()
        inspect_run_record.assert_called_once_with()
        mint_token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
