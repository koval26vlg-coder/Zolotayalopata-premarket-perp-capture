from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import execution_replay  # noqa: E402
from tests.test_execution_replay_v39 import complete_request  # noqa: E402


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_hash(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _runtime() -> types.ModuleType:
    path = SRC / "v43_verified_replay.py"
    if not path.is_file():
        raise AssertionError("src/v43_verified_replay.py must exist")
    name = "v43_verified_replay"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError("v43 verified replay runtime cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sealed_request(request: dict[str, Any] | None = None) -> dict[str, Any]:
    sealed = copy.deepcopy(request if request is not None else complete_request())
    sealed["event"].update(
        {
            "evidence_class": "SEALED_L2_CAPTURE",
            "t0_precision_sec": 1,
            "official_record_hash": "4" * 64,
            "official_source_url": "https://announcements.bybit.com/en/article/test",
        }
    )
    sealed.update(
        {
            "sealed": True,
            "evidence_class": "SEALED_L2_CAPTURE",
            "capture_manifest_sha256": "a" * 64,
            "trusted_loader_verification": {
                "schema": "premarket_perp_l2_loader_verification_v43",
                "status": "INTERNAL_CHAIN_ONLY_NOT_TRUSTED",
                "capture_id": "capture_" + "c" * 64,
                "manifest_hash": "b" * 64,
                "terminal_receipt_hash": "d" * 64,
                "raw_frames_sha256": "e" * 64,
                "normalized_depth_sha256": "f" * 64,
                "lineage_hashes": {
                    "bundle": "1" * 64,
                    "plan": "2" * 64,
                    "event": "3" * 64,
                    "arming": "4" * 64,
                    "lifecycle": "5" * 64,
                    "claim_release": "6" * 64,
                },
                "exact_readback": True,
                "gap_free": True,
                "acceptance_capable": False,
                "external_authority_verified": False,
                "trusted_replay_handoff": False,
            },
            "orders_created": 0,
            "private_api_used": False,
            "live_execution": False,
        }
    )
    payload = copy.deepcopy(sealed)
    payload.pop("evidence_envelope", None)
    sealed["evidence_envelope"] = {
        "schema": "premarket_perp_execution_evidence_envelope_v1",
        "sealed": True,
        "evidence_class": "SEALED_L2_CAPTURE",
        "capture_manifest_sha256": sealed["capture_manifest_sha256"],
        "payload_sha256": execution_replay.canonical_result_hash(payload),
    }
    return sealed


def _authority_receipt(request: dict[str, Any]) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema": "premarket_perp_v43_fixture_external_authority_verification_v1",
        "status": "FIXTURE_AUTHORITY_CHAIN_OK_NO_CAPTURE_AUTHORITY",
        "fixture_only": True,
        "fixture_external_chain_verified": True,
        "production_external_authority_verified": False,
        "capture_authorized": False,
        "capture_token_issued": False,
        "network_allowed": False,
        "orders_allowed": False,
        "acceptance_capable": False,
        "plan_id": "premarket_perp_capture_20260822_v43_fixture",
        "plan_hash": "7" * 64,
        "plan_file_sha256": "8" * 64,
        "event_id": request["event"]["event_id"],
        "capture_id": request["trusted_loader_verification"]["capture_id"],
        "hashes": {
            "authority": "9" * 64,
            "arming": "a" * 64,
            "proposal": "b" * 64,
            "lifecycle": "c" * 64,
            "approval": "d" * 64,
            "attempt": "e" * 64,
            "claim_terminal": "f" * 64,
            "manifest": request["capture_manifest_sha256"],
            "terminal_receipt": "1" * 64,
            "lineage": "2" * 64,
        },
    }
    receipt["verification_hash"] = _canonical_hash(receipt)
    return receipt


def _fake_authority_module() -> types.ModuleType:
    module = types.ModuleType("v43_fixture_authority")

    class FixtureAuthorityError(RuntimeError):
        pass

    @dataclasses.dataclass(frozen=True)
    class VerifiedFixtureHandoff:
        schema: str
        status: str
        capability_id: str
        verification_hash: str

    @dataclasses.dataclass(frozen=True)
    class VerifiedReplayRecord:
        schema: str
        status: str
        record_id: str
        verification_hash: str

    store: dict[str, tuple[VerifiedFixtureHandoff, dict[str, Any], dict[str, Any]]] = {}
    replay_store: dict[str, tuple[VerifiedReplayRecord, dict[str, Any], dict[str, Any]]] = {}
    counter = 0

    def issue(request: dict[str, Any]) -> VerifiedFixtureHandoff:
        nonlocal counter
        counter += 1
        receipt = _authority_receipt(request)
        handoff = VerifiedFixtureHandoff(
            schema="premarket_perp_v43_verified_fixture_handoff_v1",
            status="VERIFIED_FIXTURE_HANDOFF_READY",
            capability_id=f"fixture-capability-{counter}",
            verification_hash=receipt["verification_hash"],
        )
        store[handoff.capability_id] = (
            handoff,
            copy.deepcopy(request),
            copy.deepcopy(receipt),
        )
        return handoff

    def consume_fixture_handoff(handoff: object) -> dict[str, Any]:
        if type(handoff) is not VerifiedFixtureHandoff:
            raise FixtureAuthorityError("VERIFIED_FIXTURE_HANDOFF_REQUIRED")
        record = store.pop(handoff.capability_id, None)
        if record is None or record[0] != handoff:
            raise FixtureAuthorityError("FIXTURE_HANDOFF_INVALID_OR_CONSUMED")
        replay_record = VerifiedReplayRecord(
            schema="premarket_perp_v43_verified_replay_record_v1",
            status="VERIFIED_FIXTURE_REPLAY_RECORD_SINGLE_USE",
            record_id="replay-" + handoff.capability_id,
            verification_hash=handoff.verification_hash,
        )
        replay_store[replay_record.record_id] = (
            replay_record,
            record[1],
            record[2],
        )
        runtime = sys.modules.get("v43_verified_replay")
        if runtime is None:
            raise FixtureAuthorityError("VERIFIED_REPLAY_RUNTIME_NOT_LOADED")
        return runtime._execute_verified_fixture_record(replay_record)

    def consume_verified_replay_record(
        replay_record: object,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if type(replay_record) is not VerifiedReplayRecord:
            raise FixtureAuthorityError("VERIFIED_REPLAY_RECORD_REQUIRED")
        record = replay_store.pop(replay_record.record_id, None)
        if record is None or record[0] != replay_record:
            raise FixtureAuthorityError("VERIFIED_REPLAY_RECORD_INVALID_OR_CONSUMED")
        return copy.deepcopy(record[1]), copy.deepcopy(record[2])

    module.FixtureAuthorityError = FixtureAuthorityError
    module.VerifiedFixtureHandoff = VerifiedFixtureHandoff
    module.VerifiedReplayRecord = VerifiedReplayRecord
    module.consume_fixture_handoff = consume_fixture_handoff
    module.consume_verified_replay_record = consume_verified_replay_record
    module.issue_test_handoff = issue
    return module


@contextmanager
def _authority_context() -> Iterator[types.ModuleType]:
    authority = _fake_authority_module()
    with patch.dict(sys.modules, {"v43_fixture_authority": authority}):
        yield authority


class V43VerifiedReplayTests(unittest.TestCase):
    def test_real_external_authority_chain_consumes_into_fixture_replay(self) -> None:
        authority_spec = importlib.util.find_spec("v43_fixture_authority")
        self.assertIsNotNone(
            authority_spec,
            "src/v43_fixture_authority.py must exist for the real handoff integration",
        )
        import v43_fixture_authority as authority
        from tests.test_v43_fixture_authority import make_authority_bundle

        runtime = _runtime()
        with tempfile.TemporaryDirectory() as raw_root:
            bundle, plan_file_sha = make_authority_bundle(Path(raw_root))
            handoff = authority.verify_fixture_authority_bundle(
                bundle,
                expected_plan_file_sha256=plan_file_sha,
            )
            result = runtime.replay_verified_fixture(handoff)

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["evidence_mode"], "FIXTURE_REHEARSAL_ONLY")
        self.assertEqual(
            [row["offset_sec"] for row in result["horizons"]], [0, 5, 15, 60]
        )
        self.assertFalse(result["production_external_authority_verified"])
        self.assertFalse(result["acceptance_capable"])
        self.assertEqual(result["orders_created"], 0)

    def test_real_authority_rejects_dict_and_consumes_a_failed_callback(self) -> None:
        import v43_fixture_authority as authority
        from tests.test_v43_fixture_authority import make_authority_bundle

        runtime = _runtime()
        with self.assertRaises(runtime.V43VerifiedReplayError):
            runtime.replay_verified_fixture(_sealed_request())

        with tempfile.TemporaryDirectory() as raw_root:
            bundle, plan_file_sha = make_authority_bundle(Path(raw_root))
            handoff = authority.verify_fixture_authority_bundle(
                bundle,
                expected_plan_file_sha256=plan_file_sha,
            )
            with patch.object(
                runtime,
                "_execute_verified_fixture_record",
                side_effect=runtime.V43VerifiedReplayError("INJECTED_CALLBACK_FAILURE"),
            ):
                with self.assertRaisesRegex(
                    runtime.V43VerifiedReplayError,
                    "INJECTED_CALLBACK_FAILURE",
                ):
                    runtime.replay_verified_fixture(handoff)
            with self.assertRaisesRegex(
                runtime.V43VerifiedReplayError,
                "already consumed",
            ):
                runtime.replay_verified_fixture(handoff)

    def test_two_real_authority_bundles_have_deterministic_replay_results(self) -> None:
        import v43_fixture_authority as authority
        from tests.test_v43_fixture_authority import make_authority_bundle

        runtime = _runtime()
        with tempfile.TemporaryDirectory() as raw_root:
            parent = Path(raw_root)
            first_parent = parent / "first"
            second_parent = parent / "second"
            first_parent.mkdir()
            second_parent.mkdir()
            first_bundle, first_plan_sha = make_authority_bundle(first_parent)
            second_bundle, second_plan_sha = make_authority_bundle(second_parent)
            first = runtime.replay_verified_fixture(
                authority.verify_fixture_authority_bundle(
                    first_bundle,
                    expected_plan_file_sha256=first_plan_sha,
                )
            )
            second = runtime.replay_verified_fixture(
                authority.verify_fixture_authority_bundle(
                    second_bundle,
                    expected_plan_file_sha256=second_plan_sha,
                )
            )

        self.assertEqual(first, second)
        self.assertEqual(first["result_hash"], second["result_hash"])

    def test_valid_handoff_runs_four_fixed_horizons_and_relabels_fixture_result(self) -> None:
        runtime = _runtime()
        request = _sealed_request()
        with _authority_context() as authority:
            result = runtime.replay_verified_fixture(authority.issue_test_handoff(request))

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(
            [row["offset_sec"] for row in result["horizons"]], [0, 5, 15, 60]
        )
        self.assertEqual(result["evidence_mode"], "FIXTURE_REHEARSAL_ONLY")
        self.assertFalse(result["acceptance_capable"])
        self.assertFalse(result["production_external_authority_verified"])
        self.assertEqual(result["orders_created"], 0)
        self.assertFalse(result["private_api_used"])
        self.assertFalse(result["live_execution"])
        self.assertFalse(result["network_allowed"])
        self.assertFalse(result["orders_allowed"])
        self.assertFalse(result["capture_authorized"])
        self.assertFalse(result["capture_token_issued"])
        self.assertTrue(_SHA256_RE.fullmatch(result["fixture_authority_verification_hash"]))
        material = dict(result)
        claimed = material.pop("result_hash")
        self.assertEqual(claimed, execution_replay.canonical_result_hash(material))

    def test_private_callback_rejects_raw_dict_and_forged_replay_record(self) -> None:
        runtime = _runtime()
        request = _sealed_request()
        for name in (
            "_ReplayPermit",
            "_ACTIVE_REPLAY_PERMIT",
            "_ISSUED_REPLAY_PERMITS",
            "_issue_replay_permit",
            "_build_replay_entrypoints",
        ):
            self.assertFalse(
                hasattr(runtime, name),
                f"module surface must not expose permit state: {name}",
            )
        self.assertFalse(hasattr(runtime, "_execute_verified_fixture_request"))
        with _authority_context() as authority:
            with self.assertRaisesRegex(
                runtime.V43VerifiedReplayError,
                "VERIFIED_REPLAY_RECORD_REQUIRED",
            ):
                runtime._execute_verified_fixture_record(request)
            forged = authority.VerifiedReplayRecord(
                schema="premarket_perp_v43_verified_replay_record_v1",
                status="VERIFIED_FIXTURE_REPLAY_RECORD_SINGLE_USE",
                record_id="replay-forged",
                verification_hash="0" * 64,
            )
            with self.assertRaisesRegex(
                runtime.V43VerifiedReplayError,
                "INVALID_OR_CONSUMED",
            ):
                runtime._execute_verified_fixture_record(forged)

    def test_unsafe_engine_flags_are_overwritten_before_result_is_hashed(self) -> None:
        runtime = _runtime()
        unsafe_engine_result = {
            "schema": "premarket_perp_execution_replay_result_v1",
            "status": "COMPLETE",
            "event": copy.deepcopy(_sealed_request()["event"]),
            "horizons": [],
            "network_allowed": True,
            "orders_allowed": True,
            "capture_authorized": True,
            "capture_token_issued": True,
            "orders_created": 9,
            "private_api_used": True,
            "live_execution": True,
            "acceptance_capable": True,
        }
        with _authority_context() as authority, patch.object(
            execution_replay,
            "replay_fixed_long",
            return_value=unsafe_engine_result,
        ):
            result = runtime.replay_verified_fixture(
                authority.issue_test_handoff(_sealed_request())
            )

        for field in (
            "network_allowed",
            "orders_allowed",
            "capture_authorized",
            "capture_token_issued",
            "private_api_used",
            "live_execution",
            "acceptance_capable",
            "production_external_authority_verified",
        ):
            self.assertFalse(result[field])
        self.assertEqual(result["orders_created"], 0)
        material = dict(result)
        claimed = material.pop("result_hash")
        self.assertEqual(claimed, execution_replay.canonical_result_hash(material))

    def test_handoff_is_single_use_and_direct_dict_is_rejected(self) -> None:
        runtime = _runtime()
        request = _sealed_request()
        with _authority_context() as authority:
            handoff = authority.issue_test_handoff(request)
            runtime.replay_verified_fixture(handoff)
            with self.assertRaisesRegex(
                runtime.V43VerifiedReplayError, "INVALID_OR_CONSUMED"
            ):
                runtime.replay_verified_fixture(handoff)
            with self.assertRaisesRegex(
                runtime.V43VerifiedReplayError, "HANDOFF_REQUIRED"
            ):
                runtime.replay_verified_fixture(request)

    def test_direct_sealed_request_remains_fail_closed_in_existing_engine(self) -> None:
        report = execution_replay.replay_fixed_long(_sealed_request())
        self.assertEqual(report["status"], "NOT_RUN_TRUSTED_EVIDENCE_LOADER_REQUIRED")
        self.assertEqual(report["virtual_positions_created"], 0)
        self.assertIsNone(report["net_pnl_usdt"])

    def test_equivalent_capabilities_produce_the_same_deterministic_result(self) -> None:
        runtime = _runtime()
        request = _sealed_request()
        with _authority_context() as authority:
            first = runtime.replay_verified_fixture(authority.issue_test_handoff(request))
            second = runtime.replay_verified_fixture(authority.issue_test_handoff(request))
        self.assertEqual(first, second)
        self.assertEqual(first["result_hash"], second["result_hash"])

    def test_tampered_process_capability_is_rejected_upstream(self) -> None:
        runtime = _runtime()
        with _authority_context() as authority:
            handoff = authority.issue_test_handoff(_sealed_request())
            tampered = dataclasses.replace(handoff, verification_hash="0" * 64)
            with self.assertRaisesRegex(
                runtime.V43VerifiedReplayError, "INVALID_OR_CONSUMED"
            ):
                runtime.replay_verified_fixture(tampered)

    def test_partial_and_unfilled_entry_semantics_are_preserved(self) -> None:
        runtime = _runtime()
        partial_request = _sealed_request(complete_request(entry_asks=[[100.0, 0.10]]))
        unfilled = complete_request()
        unfilled["depth_snapshots"] = [
            row
            for row in unfilled["depth_snapshots"]
            if not row["snapshot_id"].startswith("entry-")
        ]
        unfilled_request = _sealed_request(unfilled)

        with _authority_context() as authority:
            partial = runtime.replay_verified_fixture(
                authority.issue_test_handoff(partial_request)
            )
            no_position = runtime.replay_verified_fixture(
                authority.issue_test_handoff(unfilled_request)
            )

        self.assertEqual(partial["entry"]["status"], "PARTIAL")
        self.assertAlmostEqual(partial["entry"]["notional_utilization"], 0.4)
        self.assertIsNotNone(partial["horizons"][0]["net_pnl_usdt"])
        self.assertEqual(no_position["status"], "NO_POSITION_ENTRY_UNFILLED")
        self.assertEqual(no_position["virtual_positions_created"], 0)
        self.assertTrue(all(row["net_pnl_usdt"] is None for row in no_position["horizons"]))
        self.assertEqual(no_position["evidence_mode"], "FIXTURE_REHEARSAL_ONLY")

    def test_wrapper_has_no_network_files_private_api_or_order_capability(self) -> None:
        runtime = _runtime()
        source_path = Path(runtime.__file__).resolve()
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        imported: set[str] = set()
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)

        self.assertFalse(
            imported
            & {
                "http",
                "httpx",
                "os",
                "pathlib",
                "public_http",
                "requests",
                "socket",
                "subprocess",
                "urllib",
            }
        )
        self.assertFalse(
            called
            & {
                "capture_event",
                "mkdir",
                "open",
                "place_order",
                "write_bytes",
                "write_text",
            }
        )
        source = source_path.read_text(encoding="utf-8").lower()
        for marker in (
            "api_key",
            "api_secret",
            "/v5/order",
            "place-order",
            "cancel-order",
        ):
            self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
