"""Plan identity, the trust root, the preflight matrix, and the capture token.

The spot monitor's audit found its guarantees stated in prose and enforced nowhere.
These tests exist so the same sentence cannot be written about this project.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import frozen_plan_bindings as trust_root  # noqa: E402
import project_config as config  # noqa: E402
import risk_gate  # noqa: E402
from canonical_hash import canonical_hash  # noqa: E402


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


class PlanIdentityTests(unittest.TestCase):
    def test_the_checked_in_plan_verifies(self) -> None:
        plan = risk_gate.load_and_verify_plan()
        self.assertEqual(plan["plan_id"], trust_root.PLAN_ID)
        self.assertEqual(plan["plan_hash"], trust_root.PLAN_HASH)

    def test_the_trust_root_is_not_part_of_what_the_plan_binds(self) -> None:
        """Otherwise it is inside the cycle it exists to break."""
        bound = {relative for _, relative in config.BOUND_RUNTIME_FILES}
        self.assertNotIn("src/frozen_plan_bindings.py", bound)

    def _forged(self, mutate) -> Path:
        plan = json.loads(config.PLAN_PATH.read_text(encoding="utf-8"))
        mutate(plan)
        plan.pop("plan_hash", None)
        plan["plan_hash"] = canonical_hash(plan)   # internally consistent again
        path = Path(tempfile.mkdtemp()) / "forged.json"
        path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        return path

    def test_a_self_consistent_but_unapproved_plan_is_refused(self) -> None:
        """The attack the trust root exists for: recompute the hash and it all agrees."""
        forged = self._forged(
            lambda plan: plan["capture_bounds"].update(max_requests_per_capture=10 ** 9)
        )
        with self.assertRaisesRegex(risk_gate.RiskGateError, "approved"):
            risk_gate.load_and_verify_plan(forged)

    def test_a_loosened_risk_contract_is_refused(self) -> None:
        forged = self._forged(lambda plan: plan["risk_contract"].update(orders=True))
        with self.assertRaises(risk_gate.RiskGateError):
            risk_gate.load_and_verify_plan(forged)

    def test_a_widened_endpoint_list_is_refused(self) -> None:
        forged = self._forged(
            lambda plan: plan["allowed_endpoints"].append(["api.binance.com", "/api/v3"])
        )
        with self.assertRaises(risk_gate.RiskGateError):
            risk_gate.load_and_verify_plan(forged)

    def test_an_edited_bound_file_is_refused(self) -> None:
        target = config.PROJECT_ROOT / "src/canonical_hash.py"
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n# drift\n")
            with self.assertRaisesRegex(risk_gate.RiskGateError, "sha256 mismatch"):
                risk_gate.load_and_verify_plan()
        finally:
            target.write_bytes(original)
        risk_gate.load_and_verify_plan()  # restored


class RiskContractTests(unittest.TestCase):
    def test_every_dangerous_capability_is_forbidden(self) -> None:
        contract = config.RISK_CONTRACT
        for key in (
            "private_api", "api_keys", "request_signing", "orders",  # risk-scan: allow api_key
            "paper_execution", "live_execution", "uses_leverage", "uses_margin",
            "real_capital", "withdrawals_or_transfers",
        ):
            with self.subTest(capability=key):
                self.assertIs(contract[key], False)

    def test_the_observed_instrument_class_is_stated(self) -> None:
        """The project watches a leveraged market. Saying so is what makes the
        distinction from using leverage reviewable rather than implicit."""
        self.assertEqual(
            config.RISK_CONTRACT["observed_instrument_class"], "crypto_perpetual_futures"
        )
        self.assertIs(config.RISK_CONTRACT["uses_leverage"], False)

    def test_replay_is_declared_offline(self) -> None:
        self.assertIn("offline", str(config.RISK_CONTRACT["execution_replay"]))

    def test_no_acceptance_decision_is_possible(self) -> None:
        self.assertEqual(config.RISK_CONTRACT["acceptance_decision"], "NONE_CAPTURE_ONLY")


class PreflightMatrixTests(unittest.TestCase):
    OPEN = {"open": True, "status": "READY_FOR_POSTPROCESS"}
    FREE = {"present": False, "blocks": False, "stale": False}

    def _decide(self, **overrides):
        kwargs = {
            "plan_error": None, "capability_error": None,
            "gate": self.OPEN, "claim": self.FREE, "run_record": self.FREE,
        }
        kwargs.update(overrides)
        return risk_gate.evaluate_risk_preflight(**kwargs)

    def test_all_clear_allows(self) -> None:
        decision = self._decide()
        self.assertTrue(decision["ok"])
        self.assertEqual(decision["decision"], "ALLOW_VISIBLE_CAPTURE")

    def test_a_failed_capability_scan_blocks(self) -> None:
        decision = self._decide(capability_error="hmac in src/collector.py")
        self.assertEqual([b["source"] for b in decision["blockers"]], ["capability_scan"])

    def test_a_plan_failure_blocks(self) -> None:
        self.assertEqual(
            [b["source"] for b in self._decide(plan_error="sha mismatch")["blockers"]],
            ["plan"],
        )

    def test_a_closed_gate_blocks(self) -> None:
        decision = self._decide(gate={"open": False, "status": "RUNNING"})
        self.assertEqual([b["source"] for b in decision["blockers"]], ["shared_gate"])

    def test_a_held_writer_claim_blocks(self) -> None:
        decision = self._decide(claim={"blocks": True, "stale": False, "detail": "held"})
        self.assertEqual(
            [b["source"] for b in decision["blockers"]], ["shared_writer_claim"]
        )

    def test_an_active_own_capture_blocks(self) -> None:
        decision = self._decide(run_record={"blocks": True, "stale": False, "detail": "own"})
        self.assertEqual([b["source"] for b in decision["blockers"]], ["own_run_record"])

    def test_every_blocker_is_reported_not_just_the_first(self) -> None:
        decision = self._decide(
            plan_error="p", capability_error="c",
            gate={"open": False, "status": "RUNNING"},
            claim={"blocks": True, "stale": True, "detail": "d"},
            run_record={"blocks": True, "stale": False, "detail": "o"},
        )
        self.assertEqual(
            [b["source"] for b in decision["blockers"]],
            ["plan", "capability_scan", "shared_gate", "shared_writer_claim",
             "own_run_record"],
        )


class SharedWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def _dead_pid(self) -> int:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_a_missing_gate_script_blocks(self) -> None:
        result = risk_gate.read_shared_gate(self.tmp / "absent.ps1")
        self.assertFalse(result["open"])
        self.assertEqual(result["status"], "UNAVAILABLE")

    def test_an_absent_claim_does_not_block(self) -> None:
        self.assertFalse(risk_gate.inspect_claim(self.tmp / "none.json")["blocks"])

    def test_a_live_claim_blocks_and_is_not_stale(self) -> None:
        path = self.tmp / "claim.json"
        path.write_text(json.dumps({"run_id": "r", "owner_pid": os.getpid()}), encoding="utf-8")
        result = risk_gate.inspect_claim(path)
        self.assertTrue(result["blocks"])
        self.assertFalse(result["stale"])

    def test_a_dead_owner_is_reported_stale_and_still_blocks(self) -> None:
        path = self.tmp / "claim.json"
        path.write_text(
            json.dumps({"run_id": "r", "owner_pid": self._dead_pid()}), encoding="utf-8"
        )
        result = risk_gate.inspect_claim(path)
        self.assertTrue(result["stale"])
        self.assertTrue(result["blocks"])   # never cleared automatically

    def test_a_launching_record_blocks(self) -> None:
        path = self.tmp / "run.json"
        path.write_text(json.dumps({
            "status": "LAUNCHING", "run_id": "r", "terminal_pid": os.getpid(),
            "started_at_utc": iso(datetime.now(timezone.utc)),
        }), encoding="utf-8")
        self.assertTrue(risk_gate.inspect_run_record(path)["blocks"])

    def test_a_finished_record_does_not_block(self) -> None:
        path = self.tmp / "run.json"
        path.write_text(json.dumps({"status": "COMPLETED", "run_id": "r"}), encoding="utf-8")
        self.assertFalse(risk_gate.inspect_run_record(path)["blocks"])

    def test_an_old_launching_record_with_a_dead_pid_is_stale(self) -> None:
        path = self.tmp / "run.json"
        stale_moment = datetime.now(timezone.utc) - timedelta(
            seconds=risk_gate.LAUNCH_GRACE_SEC + 60
        )
        path.write_text(json.dumps({
            "status": "LAUNCHING", "run_id": "r", "terminal_pid": self._dead_pid(),
            "started_at_utc": iso(stale_moment),
        }), encoding="utf-8")
        self.assertTrue(risk_gate.inspect_run_record(path)["stale"])


class CaptureTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        patch = mock.patch.object(config, "CAPTURE_TOKEN_PATH", self.tmp / "token.json")
        patch.start()
        self.addCleanup(patch.stop)

    def test_mint_then_consume(self) -> None:
        token = risk_gate.mint_capture_token("run_a")
        taken = risk_gate.consume_capture_token(token=token["token"], run_id="run_a")
        self.assertEqual(taken["run_id"], "run_a")

    def test_a_token_is_consumed_exactly_once(self) -> None:
        token = risk_gate.mint_capture_token("run_a")
        risk_gate.consume_capture_token(token=token["token"], run_id="run_a")
        with self.assertRaisesRegex(risk_gate.RiskGateError, "no capture token"):
            risk_gate.consume_capture_token(token=token["token"], run_id="run_a")

    def test_a_capture_without_a_token_is_refused(self) -> None:
        with self.assertRaisesRegex(risk_gate.RiskGateError, "preflight"):
            risk_gate.consume_capture_token(token="x" * 32, run_id="run_a")

    def test_a_token_for_another_run_is_refused(self) -> None:
        token = risk_gate.mint_capture_token("run_a")
        with self.assertRaisesRegex(risk_gate.RiskGateError, "different run_id"):
            risk_gate.consume_capture_token(token=token["token"], run_id="run_b")

    def test_an_expired_token_is_refused(self) -> None:
        token = risk_gate.mint_capture_token("run_a", ttl_sec=-1)
        with self.assertRaisesRegex(risk_gate.RiskGateError, "expired"):
            risk_gate.consume_capture_token(token=token["token"], run_id="run_a")


class CaptureBoundsTests(unittest.TestCase):
    def test_continuous_capture_is_bounded(self) -> None:
        """Capture near t0 runs while a market moves; it needs ceilings a tick did not."""
        self.assertGreater(config.MAX_CAPTURE_RUNTIME_SEC, 0)
        self.assertGreaterEqual(
            config.MAX_CAPTURE_RUNTIME_SEC,
            config.CAPTURE_WINDOW_BEFORE_SEC + config.CAPTURE_WINDOW_AFTER_SEC,
        )
        self.assertGreater(config.MAX_REQUESTS_PER_CAPTURE, 0)
        self.assertEqual(config.MAX_EVENTS_PER_CAPTURE, 1)

    def test_the_plan_records_the_same_bounds(self) -> None:
        bounds = json.loads(config.PLAN_PATH.read_text(encoding="utf-8"))["capture_bounds"]
        self.assertEqual(bounds["max_runtime_sec"], config.MAX_CAPTURE_RUNTIME_SEC)
        self.assertEqual(bounds["max_events_per_capture"], config.MAX_EVENTS_PER_CAPTURE)
        self.assertTrue(bounds["one_capture_at_a_time"])
        self.assertTrue(bounds["visible_terminal_required"])


if __name__ == "__main__":
    unittest.main()
