"""Replay may use only an exact active or immutable retired PlanOnly identity."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import risk_gate  # noqa: E402


IMPLEMENTATION = {
    "files": [
        {
            "role": "replay",
            "repo_path": "src/replay.py",
            "sha256": "a" * 64,
        }
    ]
}


class PlanIdentityVerificationTests(unittest.TestCase):
    def active_plan(self) -> dict:
        return {
            "plan_id": "plan-v16-test",
            "plan_hash": "b" * 64,
            "implementation": IMPLEMENTATION,
        }

    def test_exact_active_identity_and_implementation_verify(self) -> None:
        with mock.patch.object(
            risk_gate, "load_and_verify_plan", return_value=self.active_plan()
        ):
            report = risk_gate.verify_plan_identity(
                plan_id="plan-v16-test",
                plan_hash="b" * 64,
                implementation=IMPLEMENTATION,
            )
        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "PLAN_IDENTITY_OK")
        self.assertTrue(report["active"])

    def test_plan_id_hash_and_implementation_each_fail_closed(self) -> None:
        cases = (
            ("plan_id", "other-plan"),
            ("plan_hash", "c" * 64),
            ("implementation", {"files": []}),
        )
        for field, value in cases:
            with self.subTest(field=field), mock.patch.object(
                risk_gate, "load_and_verify_plan", return_value=self.active_plan()
            ), mock.patch.object(risk_gate.trust_root, "RETIRED_PLANS", ()):
                kwargs = {
                    "plan_id": "plan-v16-test",
                    "plan_hash": "b" * 64,
                    "implementation": IMPLEMENTATION,
                }
                kwargs[field] = value
                with self.assertRaises(risk_gate.RiskGateError):
                    risk_gate.verify_plan_identity(**kwargs)

    def test_capture_evidence_origin_rejects_a_capture_disabled_plan(self) -> None:
        plan = {
            **self.active_plan(),
            "status": risk_gate.REGISTRY_QUARANTINE_PLAN_STATUS,
            "resolved_path_bindings": {"capture_root": "C:/sealed/captures"},
            "authorized_after_gate_green": sorted(
                risk_gate.PLAN_WRITE_AUTHORIZATION[
                    risk_gate.REGISTRY_QUARANTINE_PLAN_STATUS
                ]["authorized_actions"]
            ),
        }
        with mock.patch.object(
            risk_gate, "load_and_verify_plan", return_value=plan
        ):
            with self.assertRaisesRegex(
                risk_gate.RiskGateError, "market_data_capture"
            ):
                risk_gate.verify_plan_identity(
                    plan_id="plan-v16-test",
                    plan_hash="b" * 64,
                    implementation=IMPLEMENTATION,
                    required_write_class="market_data_capture",
                )

    def test_capture_evidence_origin_reports_exact_selected_plan_authority(self) -> None:
        status = "TEST_CAPTURE_AUTHORIZED"
        actions = frozenset({risk_gate.CAPTURE_ACTION})
        plan = {
            **self.active_plan(),
            "status": status,
            "authorized_after_gate_green": [risk_gate.CAPTURE_ACTION],
            "resolved_path_bindings": {"capture_root": "C:/sealed/captures"},
        }
        authorization = {
            "authorized_actions": actions,
            "write_classes": frozenset({"market_data_capture"}),
        }
        with mock.patch.object(
            risk_gate, "load_and_verify_plan", return_value=plan
        ), mock.patch.dict(
            risk_gate.PLAN_WRITE_AUTHORIZATION,
            {status: authorization},
            clear=False,
        ):
            report = risk_gate.verify_plan_identity(
                plan_id="plan-v16-test",
                plan_hash="b" * 64,
                implementation=IMPLEMENTATION,
                required_write_class="market_data_capture",
            )

        self.assertTrue(report["evidence_origin_capture_authorized"])
        self.assertEqual(
            report["evidence_origin_write_class"], "market_data_capture"
        )
        self.assertEqual(
            report["evidence_origin_capture_root"],
            str(Path("C:/sealed/captures").resolve(strict=False)),
        )


if __name__ == "__main__":
    unittest.main()
