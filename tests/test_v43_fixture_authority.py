"""External, fixture-only authority contract for an offline v43 rehearsal.

These tests deliberately build the authority bundle outside the checkout.  The
contract may authorize exactly one in-process *offline fixture replay*.  It may
not arm capture, mint a production token, use a network, or create an order.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from dataclasses import fields
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from canonical_hash import canonical_json_bytes  # noqa: E402
import project_config as config  # noqa: E402


def _load_l2_fixture_helper() -> Any:
    path = ROOT / "tests" / "test_l2_evidence_v43.py"
    spec = importlib.util.spec_from_file_location("_v43_l2_fixture_helper", path)
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load the existing synthetic L2 fixture helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


L2 = _load_l2_fixture_helper()
T0 = L2.T0
EVENT_ID = L2.EVENT_ID
CAPTURE_ID = L2.CAPTURE_ID
CONTRACT_ID = L2.CONTRACT_ID
PLAN_HASH = L2.PLAN_HASH
PLAN_ID = "premarket-perp-capture-event-bound-v43"


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def claimed_hash(payload: dict[str, Any], field: str) -> str:
    material = copy.deepcopy(payload)
    material.pop(field, None)
    return sha256(canonical_json_bytes(material))


def seal(payload: dict[str, Any], field: str) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    result[field] = claimed_hash(result, field)
    return result


def canonical_write(path: Path, payload: object) -> None:
    path.write_bytes(canonical_json_bytes(payload) + b"\n")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


INVARIANT_CHAIN = [
    "external_plan_file_sha256->plan",
    "plan->event",
    "event->arming",
    "arming->proposal",
    "proposal->lifecycle",
    "lifecycle->approval",
    "approval->attempt",
    "attempt->claim_terminal",
    "claim_terminal->capture_manifest",
    "claim_terminal->terminal_receipt",
    "capture_lineage->plan+event+arming+lifecycle+claim_release",
]


def make_authority_bundle(parent: Path) -> tuple[Path, str]:
    """Create one complete external fixture authority bundle."""

    bundle = parent / "fixture-v43-authority"
    bundle.mkdir(parents=True)
    staging = parent / "l2-staging"
    capture_dir = L2.make_bundle(staging)
    capture_dir.rename(bundle / "capture")
    staging.rmdir()
    capture_dir = bundle / "capture"

    lineage = read_json(capture_dir / "lineage.json")
    manifest = read_json(capture_dir / "manifest.json")
    receipt = read_json(capture_dir / "terminal-receipt.json")
    event = lineage["event"]
    claim = lineage["claim_release"]

    plan = seal(
        {
            "schema": "premarket_perp_capture_planonly_v43_fixture_v1",
            "plan_id": PLAN_ID,
            "plan_hash": PLAN_HASH,
            "status": "FIXTURE_EVENT_BOUND_NO_CAPTURE_AUTHORITY",
            "fixture_only": True,
            "production_capture_authorized": False,
            "market_data_capture_write_class_authorized": False,
            "public_data_only": True,
            "network_allowed": False,
            "orders_allowed": False,
            "private_api_allowed": False,
            "live_execution_allowed": False,
            "acceptance_capable": False,
            "event_id": EVENT_ID,
            "event_lineage_hash": event["lineage_hash"],
            "venue": "bybit",
            "contract_id": CONTRACT_ID,
            "official_spot_t0": T0,
            "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
            "t0_precision_sec": 1,
            "official_record_hash": event["official_record_hash"],
            "official_source_url": event["official_source_url"],
            "capture_relative_path": "capture",
            "max_attempts": 1,
        },
        "plan_content_hash",
    )
    canonical_write(bundle / "plan.json", plan)
    plan_file_sha = sha256((bundle / "plan.json").read_bytes())

    arming = seal(
        {
            "schema": "premarket_perp_v43_fixture_arming_v1",
            "status": "FIXTURE_ARMED_NO_CAPTURE_AUTHORITY",
            "fixture_only": True,
            "plan_id": PLAN_ID,
            "plan_hash": PLAN_HASH,
            "plan_file_sha256": plan_file_sha,
            "event_id": EVENT_ID,
            "event_lineage_hash": event["lineage_hash"],
            "official_record_hash": event["official_record_hash"],
            "official_spot_t0": T0,
            "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
            "t0_precision_sec": 1,
            "capture_arming_receipt_hash": lineage["arming"]["arming_receipt_hash"],
            "capture_arming_lineage_hash": lineage["arming"]["lineage_hash"],
        },
        "arming_receipt_hash",
    )
    canonical_write(bundle / "arming.json", arming)

    proposal = seal(
        {
            "schema": "premarket_perp_v43_fixture_proposal_v1",
            "status": "FIXTURE_PROPOSAL_ONLY",
            "fixture_only": True,
            "plan_id": PLAN_ID,
            "plan_hash": PLAN_HASH,
            "event_id": EVENT_ID,
            "event_lineage_hash": event["lineage_hash"],
            "arming_receipt_hash": arming["arming_receipt_hash"],
            "official_spot_t0": T0,
        },
        "proposal_hash",
    )
    canonical_write(bundle / "proposal.json", proposal)

    lifecycle = seal(
        {
            "schema": "premarket_perp_v43_fixture_lifecycle_v1",
            "status": "FIXTURE_TERMINAL_LIFECYCLE",
            "fixture_only": True,
            "plan_id": PLAN_ID,
            "plan_hash": PLAN_HASH,
            "event_id": EVENT_ID,
            "event_lineage_hash": event["lineage_hash"],
            "proposal_hash": proposal["proposal_hash"],
            "contract_id": CONTRACT_ID,
            "official_spot_t0": T0,
            "phase_at_entry": lineage["lifecycle"]["phase_at_entry"],
            "terminal_phase": lineage["lifecycle"]["terminal_phase"],
            "transition_ts": lineage["lifecycle"]["transition_ts"],
            "lifecycle_record_hash": lineage["lifecycle"]["lifecycle_record_hash"],
            "capture_lifecycle_lineage_hash": lineage["lifecycle"]["lineage_hash"],
        },
        "lifecycle_hash",
    )
    canonical_write(bundle / "lifecycle.json", lifecycle)

    approval = seal(
        {
            "schema": "premarket_perp_v43_fixture_approval_v1",
            "status": "APPROVED_FIXTURE_REPLAY_ONLY",
            "approval_scope": "OFFLINE_FIXTURE_REPLAY_ONLY",
            "fixture_only": True,
            "one_shot": True,
            "plan_id": PLAN_ID,
            "plan_hash": PLAN_HASH,
            "event_id": EVENT_ID,
            "event_lineage_hash": event["lineage_hash"],
            "proposal_hash": proposal["proposal_hash"],
            "lifecycle_hash": lifecycle["lifecycle_hash"],
            "public_data_only": True,
            "network_allowed": False,
            "orders_allowed": False,
            "private_api_allowed": False,
            "live_execution_allowed": False,
            "acceptance_capable": False,
        },
        "approval_hash",
    )
    canonical_write(bundle / "approval.json", approval)

    attempt = seal(
        {
            "schema": "premarket_perp_v43_fixture_attempt_v1",
            "status": "CONSUMED_FIXTURE_ONLY",
            "fixture_only": True,
            "plan_id": PLAN_ID,
            "plan_hash": PLAN_HASH,
            "event_id": EVENT_ID,
            "event_lineage_hash": event["lineage_hash"],
            "proposal_hash": proposal["proposal_hash"],
            "lifecycle_hash": lifecycle["lifecycle_hash"],
            "approval_hash": approval["approval_hash"],
            "attempt_number": 1,
            "max_attempts": 1,
            "fixture_token_consumed": True,
            "fixture_token_hash": "f" * 64,
            "claim_id": claim["claim_id"],
            "claim_record_hash": claim["claim_record_hash"],
        },
        "attempt_hash",
    )
    canonical_write(bundle / "attempt.json", attempt)

    claim_terminal = seal(
        {
            "schema": "premarket_perp_v43_fixture_claim_terminal_archive_v1",
            "status": "RELEASED",
            "final_status": "COMPLETED",
            "fixture_only": True,
            "released_after_terminal_record": True,
            "plan_id": PLAN_ID,
            "plan_hash": PLAN_HASH,
            "event_id": EVENT_ID,
            "event_lineage_hash": event["lineage_hash"],
            "attempt_hash": attempt["attempt_hash"],
            "capture_id": CAPTURE_ID,
            "claim_id": claim["claim_id"],
            "claim_record_hash": claim["claim_record_hash"],
            "capture_terminal_record_hash": claim["capture_terminal_record_hash"],
            "release_record_hash": claim["release_record_hash"],
            "manifest_sha256": sha256((capture_dir / "manifest.json").read_bytes()),
            "manifest_hash": manifest["manifest_hash"],
            "terminal_receipt_sha256": sha256(
                (capture_dir / "terminal-receipt.json").read_bytes()
            ),
            "terminal_receipt_hash": receipt["receipt_hash"],
        },
        "claim_terminal_hash",
    )
    canonical_write(bundle / "claim-terminal.json", claim_terminal)

    artifact_paths = {
        "plan": bundle / "plan.json",
        "arming": bundle / "arming.json",
        "proposal": bundle / "proposal.json",
        "lifecycle": bundle / "lifecycle.json",
        "approval": bundle / "approval.json",
        "attempt": bundle / "attempt.json",
        "claim_terminal": bundle / "claim-terminal.json",
        "capture_lineage": capture_dir / "lineage.json",
        "capture_manifest": capture_dir / "manifest.json",
        "capture_terminal_receipt": capture_dir / "terminal-receipt.json",
    }
    authority = seal(
        {
            "schema": "premarket_perp_v43_fixture_external_authority_v1",
            "status": "FIXTURE_AUTHORITY_SEALED_NO_CAPTURE_AUTHORITY",
            "fixture_only": True,
            "production_authority": False,
            "capture_authorized": False,
            "capture_token_issued": False,
            "network_allowed": False,
            "orders_allowed": False,
            "acceptance_capable": False,
            "invariant_chain": INVARIANT_CHAIN,
            "plan_id": PLAN_ID,
            "plan_hash": PLAN_HASH,
            "plan_file_sha256": plan_file_sha,
            "event_id": EVENT_ID,
            "event_lineage_hash": event["lineage_hash"],
            "official_record_hash": event["official_record_hash"],
            "venue": "bybit",
            "contract_id": CONTRACT_ID,
            "official_spot_t0": T0,
            "capture_id": CAPTURE_ID,
            "artifact_sha256": {
                name: sha256(path.read_bytes()) for name, path in artifact_paths.items()
            },
            "artifact_claim_hashes": {
                "plan_content_hash": plan["plan_content_hash"],
                "arming_receipt_hash": arming["arming_receipt_hash"],
                "proposal_hash": proposal["proposal_hash"],
                "lifecycle_hash": lifecycle["lifecycle_hash"],
                "approval_hash": approval["approval_hash"],
                "attempt_hash": attempt["attempt_hash"],
                "claim_terminal_hash": claim_terminal["claim_terminal_hash"],
                "capture_lineage_hash": lineage["lineage_hash"],
                "capture_manifest_hash": manifest["manifest_hash"],
                "capture_terminal_receipt_hash": receipt["receipt_hash"],
            },
        },
        "authority_hash",
    )
    canonical_write(bundle / "authority.json", authority)
    return bundle, plan_file_sha


def reseal_authority(bundle: Path) -> None:
    authority_path = bundle / "authority.json"
    authority = read_json(authority_path)
    bound_paths = {
        "plan": bundle / "plan.json",
        "arming": bundle / "arming.json",
        "proposal": bundle / "proposal.json",
        "lifecycle": bundle / "lifecycle.json",
        "approval": bundle / "approval.json",
        "attempt": bundle / "attempt.json",
        "claim_terminal": bundle / "claim-terminal.json",
        "capture_lineage": bundle / "capture" / "lineage.json",
        "capture_manifest": bundle / "capture" / "manifest.json",
        "capture_terminal_receipt": bundle / "capture" / "terminal-receipt.json",
    }
    authority["artifact_sha256"] = {
        name: sha256(path.read_bytes()) for name, path in bound_paths.items()
    }
    claim_fields = {
        "plan_content_hash": ("plan.json", "plan_content_hash"),
        "arming_receipt_hash": ("arming.json", "arming_receipt_hash"),
        "proposal_hash": ("proposal.json", "proposal_hash"),
        "lifecycle_hash": ("lifecycle.json", "lifecycle_hash"),
        "approval_hash": ("approval.json", "approval_hash"),
        "attempt_hash": ("attempt.json", "attempt_hash"),
        "claim_terminal_hash": ("claim-terminal.json", "claim_terminal_hash"),
        "capture_lineage_hash": ("capture/lineage.json", "lineage_hash"),
        "capture_manifest_hash": ("capture/manifest.json", "manifest_hash"),
        "capture_terminal_receipt_hash": (
            "capture/terminal-receipt.json",
            "receipt_hash",
        ),
    }
    authority["artifact_claim_hashes"] = {
        name: read_json(bundle / file_name)[field]
        for name, (file_name, field) in claim_fields.items()
    }
    authority["authority_hash"] = claimed_hash(authority, "authority_hash")
    canonical_write(authority_path, authority)


def authority_runtime() -> Any:
    spec = importlib.util.find_spec("v43_fixture_authority")
    if spec is None:
        raise AssertionError("RED: src/v43_fixture_authority.py is missing")
    return importlib.import_module("v43_fixture_authority")


class FixtureAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_valid_chain_returns_only_opaque_single_use_handoff(self) -> None:
        runtime = authority_runtime()
        bundle, plan_sha = make_authority_bundle(self.root)
        handoff = runtime.verify_fixture_authority_bundle(
            bundle, expected_plan_file_sha256=plan_sha
        )

        self.assertEqual(
            [field.name for field in fields(handoff)],
            ["schema", "status", "capability_id", "verification_hash"],
        )
        self.assertFalse(hasattr(handoff, "request"))
        self.assertFalse(hasattr(handoff, "authority_receipt"))
        self.assertFalse(hasattr(handoff, "__dict__"))

        seen: dict[str, Any] = {}
        fake_replay = types.ModuleType("v43_verified_replay")

        def execute(opaque: object) -> dict[str, Any]:
            seen["opaque"] = opaque
            self.assertIs(type(opaque), runtime.VerifiedReplayRecord)
            self.assertEqual(
                [field.name for field in fields(opaque)],
                ["schema", "status", "record_id", "verification_hash"],
            )
            self.assertFalse(hasattr(opaque, "request"))
            self.assertFalse(hasattr(opaque, "receipt"))
            self.assertFalse(hasattr(opaque, "__dict__"))
            request, receipt = runtime.consume_verified_replay_record(opaque)
            seen["request"] = request
            seen["receipt"] = receipt
            return {
                "schema": "fixture-replay-result-v1",
                "status": "OFFLINE_FIXTURE_REPLAYED",
                "verification_hash": receipt["verification_hash"],
            }

        fake_replay._execute_verified_fixture_record = execute  # type: ignore[attr-defined]
        previous = sys.modules.get("v43_verified_replay")
        sys.modules["v43_verified_replay"] = fake_replay
        try:
            result = runtime.consume_fixture_handoff(handoff)
        finally:
            if previous is None:
                sys.modules.pop("v43_verified_replay", None)
            else:
                sys.modules["v43_verified_replay"] = previous

        self.assertEqual(result["status"], "OFFLINE_FIXTURE_REPLAYED")
        self.assertEqual(seen["request"]["event"]["event_id"], EVENT_ID)
        receipt = seen["receipt"]
        self.assertEqual(
            receipt["status"], "FIXTURE_AUTHORITY_CHAIN_OK_NO_CAPTURE_AUTHORITY"
        )
        self.assertTrue(receipt["fixture_only"])
        self.assertTrue(receipt["fixture_external_chain_verified"])
        self.assertFalse(receipt["production_external_authority_verified"])
        for field_name in (
            "capture_authorized",
            "capture_token_issued",
            "network_allowed",
            "orders_allowed",
            "acceptance_capable",
        ):
            self.assertFalse(receipt[field_name])

        with self.assertRaises(runtime.FixtureAuthorityError):
            runtime.consume_verified_replay_record(seen["opaque"])

        with self.assertRaises(runtime.FixtureAuthorityError):
            runtime.consume_fixture_handoff(handoff)

    def test_expected_external_plan_file_sha_is_mandatory_and_exact(self) -> None:
        runtime = authority_runtime()
        bundle, _plan_sha = make_authority_bundle(self.root)
        with self.assertRaises(runtime.FixtureAuthorityError):
            runtime.verify_fixture_authority_bundle(
                bundle, expected_plan_file_sha256="0" * 64
            )

    def test_resealed_artifact_is_rejected_when_authority_is_stale(self) -> None:
        runtime = authority_runtime()
        bundle, plan_sha = make_authority_bundle(self.root)
        approval_path = bundle / "approval.json"
        approval = read_json(approval_path)
        approval["status"] = "RESEALED_BUT_NOT_AUTHORIZED"
        approval["approval_hash"] = claimed_hash(approval, "approval_hash")
        canonical_write(approval_path, approval)

        with self.assertRaises(runtime.FixtureAuthorityError):
            runtime.verify_fixture_authority_bundle(
                bundle, expected_plan_file_sha256=plan_sha
            )

    def test_fully_resealed_authority_cannot_hide_cross_event_mismatch(self) -> None:
        runtime = authority_runtime()
        bundle, plan_sha = make_authority_bundle(self.root)
        lifecycle_path = bundle / "lifecycle.json"
        lifecycle = read_json(lifecycle_path)
        lifecycle["event_id"] = "event_" + "0" * 64
        lifecycle["lifecycle_hash"] = claimed_hash(lifecycle, "lifecycle_hash")
        canonical_write(lifecycle_path, lifecycle)
        reseal_authority(bundle)

        with self.assertRaises(runtime.FixtureAuthorityError):
            runtime.verify_fixture_authority_bundle(
                bundle, expected_plan_file_sha256=plan_sha
            )

    def test_research_only_fields_fail_closed_after_reseal(self) -> None:
        runtime = authority_runtime()
        bundle, plan_sha = make_authority_bundle(self.root)
        approval_path = bundle / "approval.json"
        approval = read_json(approval_path)
        approval["orders_allowed"] = True
        approval["approval_hash"] = claimed_hash(approval, "approval_hash")
        canonical_write(approval_path, approval)
        reseal_authority(bundle)

        with self.assertRaises(runtime.FixtureAuthorityError):
            runtime.verify_fixture_authority_bundle(
                bundle, expected_plan_file_sha256=plan_sha
            )

    def test_production_or_checkout_paths_are_rejected_before_layout_read(self) -> None:
        runtime = authority_runtime()
        for path in (config.PROJECT_ROOT, config.CAPTURE_ROOT, config.CONTROL_ROOT):
            with self.subTest(path=path):
                with self.assertRaises(runtime.FixtureAuthorityError):
                    runtime.verify_fixture_authority_bundle(
                        path, expected_plan_file_sha256="0" * 64
                    )

    def test_forged_or_replayed_capability_is_rejected(self) -> None:
        runtime = authority_runtime()
        fake = runtime.VerifiedFixtureHandoff(
            schema="premarket_perp_v43_verified_fixture_handoff_v1",
            status="VERIFIED_FIXTURE_HANDOFF_SINGLE_USE",
            capability_id="0" * 64,
            verification_hash="1" * 64,
        )
        with self.assertRaises(runtime.FixtureAuthorityError):
            runtime.consume_fixture_handoff(fake)

        fake_record = runtime.VerifiedReplayRecord(
            schema="premarket_perp_v43_verified_replay_record_v1",
            status="VERIFIED_FIXTURE_REPLAY_RECORD_SINGLE_USE",
            record_id="2" * 64,
            verification_hash="3" * 64,
        )
        with self.assertRaises(runtime.FixtureAuthorityError):
            runtime.consume_verified_replay_record(fake_record)

    def test_callback_cannot_retain_unconsumed_replay_record_after_return(self) -> None:
        runtime = authority_runtime()
        bundle, plan_sha = make_authority_bundle(self.root)
        handoff = runtime.verify_fixture_authority_bundle(
            bundle, expected_plan_file_sha256=plan_sha
        )
        captured: list[object] = []
        fake_replay = types.ModuleType("v43_verified_replay")

        def capture_only(opaque: object) -> dict[str, Any]:
            captured.append(opaque)
            return {"status": "CALLBACK_RETURNED_WITHOUT_CONSUMING_RECORD"}

        fake_replay._execute_verified_fixture_record = capture_only  # type: ignore[attr-defined]
        previous = sys.modules.get("v43_verified_replay")
        sys.modules["v43_verified_replay"] = fake_replay
        try:
            runtime.consume_fixture_handoff(handoff)
        finally:
            if previous is None:
                sys.modules.pop("v43_verified_replay", None)
            else:
                sys.modules["v43_verified_replay"] = previous

        self.assertEqual(len(captured), 1)
        with self.assertRaises(runtime.FixtureAuthorityError):
            runtime.consume_verified_replay_record(captured[0])
        with self.assertRaises(runtime.FixtureAuthorityError):
            runtime.consume_fixture_handoff(handoff)

    def test_callback_failure_consumes_handoff_and_replay_record(self) -> None:
        runtime = authority_runtime()
        bundle, plan_sha = make_authority_bundle(self.root)
        handoff = runtime.verify_fixture_authority_bundle(
            bundle, expected_plan_file_sha256=plan_sha
        )
        captured: list[object] = []
        fake_replay = types.ModuleType("v43_verified_replay")

        class CallbackFailure(RuntimeError):
            pass

        def fail_after_capture(opaque: object) -> dict[str, Any]:
            captured.append(opaque)
            raise CallbackFailure("intentional fixture callback failure")

        fake_replay._execute_verified_fixture_record = fail_after_capture  # type: ignore[attr-defined]
        previous = sys.modules.get("v43_verified_replay")
        sys.modules["v43_verified_replay"] = fake_replay
        try:
            with self.assertRaises(CallbackFailure):
                runtime.consume_fixture_handoff(handoff)
        finally:
            if previous is None:
                sys.modules.pop("v43_verified_replay", None)
            else:
                sys.modules["v43_verified_replay"] = previous

        self.assertEqual(len(captured), 1)
        with self.assertRaises(runtime.FixtureAuthorityError):
            runtime.consume_verified_replay_record(captured[0])
        with self.assertRaises(runtime.FixtureAuthorityError):
            runtime.consume_fixture_handoff(handoff)

    def test_exact_layout_rejects_unbound_extra_file(self) -> None:
        runtime = authority_runtime()
        bundle, plan_sha = make_authority_bundle(self.root)
        (bundle / "unbound.txt").write_text("not bound", encoding="utf-8")
        with self.assertRaises(runtime.FixtureAuthorityError):
            runtime.verify_fixture_authority_bundle(
                bundle, expected_plan_file_sha256=plan_sha
            )


if __name__ == "__main__":
    unittest.main()
