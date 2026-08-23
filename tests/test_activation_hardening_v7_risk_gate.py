"""Regression tests for one-shot capture-token authority.

The token file is a capability.  A caller that cannot prove the exact token/run/event
binding must not be able to destroy it, while a caller that does prove it gets one
atomic consume attempt followed by fresh live-policy checks.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import project_config as config  # noqa: E402
import risk_gate  # noqa: E402
from canonical_hash import canonical_hash  # noqa: E402


class CaptureTokenV7Tests(unittest.TestCase):
    EVENT_ID = "episode-token-v7"
    SOURCE_CLASS = "OFFICIAL_ANNOUNCEMENT"
    RUN_ID = "run-token-v7"
    PLAN = {
        "plan_id": "capture-token-v7-plan",
        "plan_hash": "a" * 64,
        "status": "CAPTURE_IMPLEMENTATION_AUDIT_GREEN",
    }
    CAPABILITY = {
        "status": "CAPABILITY_SCAN_CLEAN",
        "report_hash": "c" * 64,
    }

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.token_path = self.tmp / "capture-token.json"
        token_path_patch = mock.patch.object(config, "CAPTURE_TOKEN_PATH", self.token_path)
        token_path_patch.start()
        self.addCleanup(token_path_patch.stop)

    def _preflight(self, run_id: str = RUN_ID) -> dict[str, object]:
        paths = risk_gate.resolved_config()
        return {
            "schema": risk_gate.PREFLIGHT_RESULT_SCHEMA,
            "ok": True,
            "verified": True,
            "decision": "ALLOW_VISIBLE_CAPTURE",
            "write_class": "market_data_capture",
            "action": risk_gate.CAPTURE_ACTION,
            "run_id": run_id,
            "event_id": self.EVENT_ID,
            "source_class": self.SOURCE_CLASS,
            "plan_id": self.PLAN["plan_id"],
            "plan_hash": self.PLAN["plan_hash"],
            "resolved_paths": paths,
            "resolved_paths_hash": canonical_hash(paths),
            "gate_status": "READY_FOR_POSTPROCESS",
            "capability_scan": dict(self.CAPABILITY),
        }

    def _mint(self, run_id: str = RUN_ID) -> dict[str, object]:
        with (
            mock.patch.object(risk_gate, "load_and_verify_plan", return_value=self.PLAN),
            mock.patch.object(risk_gate, "verify_plan_write_authorization", return_value={}),
            mock.patch.object(risk_gate, "verify_resolved_path_bindings", return_value={}),
        ):
            return risk_gate.mint_capture_token(
                run_id,
                event_id=self.EVENT_ID,
                source_class=self.SOURCE_CLASS,
                verified_preflight=self._preflight(run_id),
            )

    def _consume(
        self,
        token: str,
        *,
        run_id: str = RUN_ID,
        event_id: str = EVENT_ID,
        source_class: str = SOURCE_CLASS,
    ) -> dict[str, object]:
        with (
            mock.patch.object(risk_gate, "load_and_verify_plan", return_value=self.PLAN),
            mock.patch.object(risk_gate, "verify_plan_write_authorization", return_value={}),
            mock.patch.object(risk_gate, "verify_resolved_path_bindings", return_value={}),
            mock.patch.object(risk_gate, "run_capability_scan", return_value=self.CAPABILITY),
            mock.patch.object(
                risk_gate,
                "read_shared_gate",
                return_value={"open": True, "status": "READY_FOR_POSTPROCESS"},
            ),
            mock.patch.object(risk_gate, "inspect_claim", return_value={"blocks": False}),
            mock.patch.object(risk_gate, "inspect_run_record", return_value={"blocks": False}),
        ):
            return risk_gate.consume_capture_token(
                token=token,
                run_id=run_id,
                event_id=event_id,
                source_class=source_class,
            )

    def test_mint_refuses_to_overwrite_an_outstanding_token(self) -> None:
        self._mint()
        original = self.token_path.read_bytes()

        with self.assertRaisesRegex(risk_gate.RiskGateError, "already exists|outstanding"):
            self._mint()

        self.assertEqual(self.token_path.read_bytes(), original)

    def test_wrong_token_does_not_destroy_valid_authority(self) -> None:
        minted = self._mint()
        original = self.token_path.read_bytes()

        with self.assertRaisesRegex(risk_gate.RiskGateError, "token mismatch"):
            self._consume("0" * 32)

        self.assertEqual(self.token_path.read_bytes(), original)
        self.assertEqual(self._consume(str(minted["token"]))["run_id"], self.RUN_ID)

    def test_wrong_run_event_or_source_does_not_destroy_valid_authority(self) -> None:
        mismatches = (
            ({"run_id": "other-run"}, "different run_id"),
            ({"event_id": "other-event"}, "different event"),
            ({"source_class": "VENUE_INSTRUMENT_METADATA"}, "different source"),
        )
        for overrides, message in mismatches:
            with self.subTest(overrides=overrides):
                minted = self._mint()
                original = self.token_path.read_bytes()
                with self.assertRaisesRegex(risk_gate.RiskGateError, message):
                    self._consume(str(minted["token"]), **overrides)
                self.assertEqual(self.token_path.read_bytes(), original)
                self._consume(str(minted["token"]))

    def test_malformed_json_is_reported_without_destroying_the_file(self) -> None:
        raw = b"{not-json"
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_bytes(raw)

        with self.assertRaisesRegex(risk_gate.RiskGateError, "unreadable|JSON"):
            self._consume("0" * 32)

        self.assertEqual(self.token_path.read_bytes(), raw)

    def test_binding_hash_mismatch_is_non_destructive(self) -> None:
        minted = self._mint()
        payload = json.loads(self.token_path.read_text(encoding="utf-8"))
        payload["binding_hash"] = "0" * 64
        self.token_path.write_text(json.dumps(payload), encoding="utf-8")
        tampered = self.token_path.read_bytes()

        with self.assertRaisesRegex(risk_gate.RiskGateError, "binding hash mismatch"):
            self._consume(str(minted["token"]))

        self.assertEqual(self.token_path.read_bytes(), tampered)

    def test_malformed_expiry_is_a_non_destructive_gate_error(self) -> None:
        minted = self._mint()
        payload = json.loads(self.token_path.read_text(encoding="utf-8"))
        payload["expires_at_ts"] = "not-an-integer"
        payload["binding_hash"] = canonical_hash(
            {key: value for key, value in payload.items() if key not in {"token", "binding_hash"}}
        )
        self.token_path.write_text(json.dumps(payload), encoding="utf-8")
        malformed = self.token_path.read_bytes()

        with self.assertRaisesRegex(risk_gate.RiskGateError, "expiry"):
            self._consume(str(minted["token"]))

        self.assertEqual(self.token_path.read_bytes(), malformed)

    def test_mint_requires_the_exact_current_resolved_paths_receipt(self) -> None:
        receipt = self._preflight()
        receipt["resolved_paths_hash"] = "b" * 64
        with (
            mock.patch.object(risk_gate, "load_and_verify_plan", return_value=self.PLAN),
            mock.patch.object(risk_gate, "verify_plan_write_authorization", return_value={}),
            mock.patch.object(risk_gate, "verify_resolved_path_bindings", return_value={}),
        ):
            with self.assertRaisesRegex(risk_gate.RiskGateError, "resolved paths"):
                risk_gate.mint_capture_token(
                    self.RUN_ID,
                    event_id=self.EVENT_ID,
                    source_class=self.SOURCE_CLASS,
                    verified_preflight=receipt,
                )
        self.assertFalse(self.token_path.exists())

    def test_consume_rechecks_exact_current_resolved_paths_after_atomic_take(self) -> None:
        minted = self._mint()
        changed = dict(risk_gate.resolved_config())
        changed["capture_root"] = str(self.tmp / "moved-capture-root")

        with mock.patch.object(risk_gate, "resolved_config", return_value=changed):
            with self.assertRaisesRegex(risk_gate.RiskGateError, "resolved paths"):
                self._consume(str(minted["token"]))

        # Live-state failures happen after the atomic take and consume the one-shot token.
        self.assertFalse(self.token_path.exists())


if __name__ == "__main__":
    unittest.main()
