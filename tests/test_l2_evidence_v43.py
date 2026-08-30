"""Contract tests for a future event-bound v43 trusted L2 evidence loader.

The fixture is synthetic and local.  It exercises only immutable public-market
evidence verification and construction of an offline execution-replay request.
It does not arm capture, contact a venue, or authorize any exchange action.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from canonical_hash import canonical_json_bytes  # noqa: E402
from execution_replay import EXPECTED_MODEL, replay_fixed_long  # noqa: E402


T0 = 1_900_000_000
CAPTURE_ID = "capture_" + "c" * 64
EVENT_ID = "event_" + "e" * 64
CONTRACT_ID = "TESTUSDT"
PLAN_HASH = "1" * 64
OFFSETS = (-60, 0, 5, 15, 60)


def loader_runtime() -> ModuleType:
    spec = importlib.util.find_spec("l2_evidence")
    if spec is None:
        raise AssertionError(
            "RED: src/l2_evidence.py is missing; the trusted v43 loader is not implemented"
        )
    return importlib.import_module("l2_evidence")


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


def point_name(offset: int) -> str:
    return "entry" if offset == -60 else f"exit-{offset}"


def make_bundle(root: Path) -> Path:
    capture_dir = root / "synthetic-v43-capture"
    capture_dir.mkdir(parents=True)

    plan = seal(
        {
            "schema": "premarket_perp_l2_plan_lineage_v43",
            "plan_id": "premarket-perp-capture-event-bound-v43",
            "plan_hash": PLAN_HASH,
            "capture_runtime_sha256": "2" * 64,
            "loader_runtime_sha256": "3" * 64,
        },
        "lineage_hash",
    )
    event = seal(
        {
            "schema": "premarket_perp_l2_event_lineage_v43",
            "event_id": EVENT_ID,
            "venue": "bybit",
            "contract_id": CONTRACT_ID,
            "official_spot_t0": T0,
            "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
            "t0_precision_sec": 1,
            "official_record_hash": "4" * 64,
            "official_source_url": "https://announcements.bybit.com/en/article/test-listing",
            "plan_hash": PLAN_HASH,
        },
        "lineage_hash",
    )
    arming = seal(
        {
            "schema": "premarket_perp_l2_arming_lineage_v43",
            "status": "ARMED",
            "arming_receipt_hash": "5" * 64,
            "event_lineage_hash": event["lineage_hash"],
            "plan_hash": PLAN_HASH,
            "official_spot_t0": T0,
        },
        "lineage_hash",
    )
    lifecycle = seal(
        {
            "schema": "premarket_perp_l2_lifecycle_lineage_v43",
            "lifecycle_record_hash": "6" * 64,
            "event_lineage_hash": event["lineage_hash"],
            "contract_id": CONTRACT_ID,
            "phase_at_entry": "CONTINUOUS",
            "terminal_phase": "TRANSITIONED",
            "transition_ts": T0 + 120,
        },
        "lineage_hash",
    )
    claim_release = seal(
        {
            "schema": "premarket_perp_l2_claim_release_lineage_v43",
            "status": "RELEASED",
            "claim_id": "claim_" + "7" * 64,
            "claim_record_hash": "8" * 64,
            "capture_terminal_record_hash": "9" * 64,
            "release_record_hash": "a" * 64,
            "released_after_capture_terminal_record": True,
            "capture_id": CAPTURE_ID,
            "event_lineage_hash": event["lineage_hash"],
            "plan_hash": PLAN_HASH,
        },
        "lineage_hash",
    )
    lineage = seal(
        {
            "schema": "premarket_perp_l2_lineage_bundle_v43",
            "capture_id": CAPTURE_ID,
            "plan": plan,
            "event": event,
            "arming": arming,
            "lifecycle": lifecycle,
            "claim_release": claim_release,
        },
        "lineage_hash",
    )
    canonical_write(capture_dir / "lineage.json", lineage)

    raw_frames: list[dict[str, Any]] = []
    normalized_rows: list[dict[str, Any]] = []
    mark_rows: list[dict[str, Any]] = []
    coverage_points: list[dict[str, Any]] = []
    for sequence, offset in enumerate(OFFSETS, start=1):
        name = point_name(offset)
        target = T0 + offset
        bid = 99.0 + sequence
        ask = bid + 1.0
        frame = seal(
            {
                "schema": "premarket_perp_l2_raw_frame_v43",
                "capture_id": CAPTURE_ID,
                "event_lineage_hash": event["lineage_hash"],
                "sequence": sequence,
                "venue": "bybit",
                "contract_id": CONTRACT_ID,
                "channel": "depth",
                "request_ts": target + 0.100,
                "received_ts": target + 0.250,
                "exchange_ts": target + 0.200,
                "payload": {"bids": [[bid, 1.0]], "asks": [[ask, 1.0]]},
            },
            "frame_hash",
        )
        raw_frames.append(frame)
        snapshot_id = f"depth-{name}"
        normalized_rows.append(
            {
                "schema": "premarket_perp_depth_snapshot_v1",
                "snapshot_id": snapshot_id,
                "capture_id": CAPTURE_ID,
                "event_lineage_hash": event["lineage_hash"],
                "venue": "bybit",
                "contract_id": CONTRACT_ID,
                "source_frame_sequence": sequence,
                "source_frame_hash": frame["frame_hash"],
                "request_ts": target + 0.100,
                "received_ts": target + 0.250,
                "exchange_ts": target + 0.200,
                "bids": [[bid, 1.0]],
                "asks": [[ask, 1.0]],
            }
        )
        mark_rows.append(
            {
                "schema": "premarket_perp_mark_index_observation_v1",
                "observation_id": f"mark-{name}",
                "received_ts": target + 0.250,
                "exchange_ts": target + 0.200,
                "mark_price": (bid + ask) / 2,
                "index_price": (bid + ask) / 2,
            }
        )
        coverage_points.append(
            {
                "role": "entry" if offset == -60 else "exit",
                "offset_sec": offset,
                "snapshot_id": snapshot_id,
                "gap_free": True,
            }
        )

    tape_raw = b"".join(canonical_json_bytes(row) + b"\n" for row in raw_frames)
    (capture_dir / "raw-frames.jsonl").write_bytes(tape_raw)

    canonical_write(
        capture_dir / "normalized-depth.json",
        {
            "schema": "premarket_perp_l2_normalized_depth_v43",
            "capture_id": CAPTURE_ID,
            "event_lineage_hash": event["lineage_hash"],
            "normalizer_sha256": "b" * 64,
            "rows": normalized_rows,
        },
    )
    canonical_write(
        capture_dir / "contract-cost.json",
        {
            "schema": "premarket_perp_l2_contract_cost_v43",
            "capture_id": CAPTURE_ID,
            "event_lineage_hash": event["lineage_hash"],
            "taker_fee_bps": 10.0,
            "fee_evidence": {
                "source_class": "VENUE_PUBLIC_FEE_SCHEDULE",
                "observed_ts": T0 - 3600,
                "raw_sha256": "c" * 64,
            },
            "contract_spec": {
                "schema": "premarket_perp_contract_spec_v1",
                "venue": "bybit",
                "contract_id": CONTRACT_ID,
                "base_currency": "TEST",
                "quote_currency": "USDT",
                "settle_currency": "USDT",
                "size_unit": "BASE",
                "base_per_size_unit": 1.0,
                "price_tick": 0.01,
                "quantity_step_size_units": 0.01,
                "min_quantity_size_units": 0.01,
                "min_notional_usdt": 1.0,
                "maintenance_margin_rate": 0.005,
                "price_limit_low": 1.0,
                "price_limit_high": 1000.0,
                "received_ts": T0 - 60,
                "raw_sha256": "d" * 64,
            },
        },
    )
    canonical_write(
        capture_dir / "funding.json",
        {
            "schema": "premarket_perp_l2_funding_v43",
            "capture_id": CAPTURE_ID,
            "event_lineage_hash": event["lineage_hash"],
            "coverage_start_ts": T0 - 60,
            "coverage_end_ts": T0 + 61,
            "schedule_status": "VERIFIED_COMPLETE",
            "source_raw_sha256": "e" * 64,
            "rows": [],
        },
    )
    canonical_write(
        capture_dir / "mark-index.json",
        {
            "schema": "premarket_perp_l2_mark_index_v43",
            "capture_id": CAPTURE_ID,
            "event_lineage_hash": event["lineage_hash"],
            "coverage_start_ts": T0 - 60,
            "coverage_end_ts": T0 + 61,
            "gap_free": True,
            "source_raw_sha256": "f" * 64,
            "required_offsets_sec": list(OFFSETS),
            "rows": mark_rows,
        },
    )

    file_names = (
        "raw-frames.jsonl",
        "normalized-depth.json",
        "contract-cost.json",
        "funding.json",
        "mark-index.json",
        "lineage.json",
    )
    lineage_hashes = {
        "bundle": lineage["lineage_hash"],
        "plan": plan["lineage_hash"],
        "event": event["lineage_hash"],
        "arming": arming["lineage_hash"],
        "lifecycle": lifecycle["lineage_hash"],
        "claim_release": claim_release["lineage_hash"],
    }
    manifest = seal(
        {
            "schema": "premarket_perp_l2_capture_manifest_v43",
            "capture_id": CAPTURE_ID,
            "status": "COMPLETED",
            "evidence_class": "SEALED_L2_CAPTURE",
            "acceptance_capable": False,
            "file_sha256": {
                name: sha256((capture_dir / name).read_bytes()) for name in file_names
            },
            "lineage_hashes": lineage_hashes,
            "coverage": {
                "status": "COMPLETE",
                "gap_free": True,
                "sequence_start": 1,
                "sequence_end": len(raw_frames),
                "missing_sequences": [],
                "points": coverage_points,
            },
        },
        "manifest_hash",
    )
    canonical_write(capture_dir / "manifest.json", manifest)

    manifest_raw = (capture_dir / "manifest.json").read_bytes()
    receipt = seal(
        {
            "schema": "premarket_perp_l2_terminal_receipt_v43",
            "capture_id": CAPTURE_ID,
            "status": "COMPLETED",
            "manifest_sha256": sha256(manifest_raw),
            "manifest_hash": manifest["manifest_hash"],
            "lineage_hashes": lineage_hashes,
            "claim_released": True,
            "orders_created": 0,
            "private_api_used": False,
            "live_execution": False,
        },
        "receipt_hash",
    )
    canonical_write(capture_dir / "terminal-receipt.json", receipt)
    return capture_dir


def rewrite_json(path: Path, mutator: Callable[[dict[str, Any]], None]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutator(payload)
    canonical_write(path, payload)


def reseal(capture_dir: Path) -> None:
    manifest_path = capture_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name in tuple(manifest["file_sha256"]):
        manifest["file_sha256"][name] = sha256((capture_dir / name).read_bytes())
    manifest["manifest_hash"] = claimed_hash(manifest, "manifest_hash")
    canonical_write(manifest_path, manifest)

    receipt_path = capture_dir / "terminal-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["manifest_sha256"] = sha256(manifest_path.read_bytes())
    receipt["manifest_hash"] = manifest["manifest_hash"]
    receipt["receipt_hash"] = claimed_hash(receipt, "receipt_hash")
    canonical_write(receipt_path, receipt)


class TrustedL2EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def test_module_exists(self) -> None:
        runtime = loader_runtime()
        self.assertTrue(callable(runtime.load_verified_execution_request))
        self.assertTrue(callable(runtime.inspect_candidate_execution_request))

    def test_complete_bundle_builds_only_the_fixed_execution_request(self) -> None:
        runtime = loader_runtime()
        capture_dir = make_bundle(self.tmp)

        request = runtime.inspect_candidate_execution_request(capture_dir)

        self.assertEqual(request["schema"], "premarket_perp_execution_replay_request_v1")
        self.assertEqual(request["event"]["event_id"], EVENT_ID)
        self.assertEqual(request["event"]["official_spot_t0"], T0)
        self.assertEqual(request["event"]["t0_source_class"], "OFFICIAL_ANNOUNCEMENT")
        self.assertEqual(request["model"], {**EXPECTED_MODEL, "taker_fee_bps": 10.0})
        self.assertEqual(request["contract_spec"]["contract_id"], CONTRACT_ID)
        self.assertEqual(len(request["depth_snapshots"]), len(OFFSETS))
        self.assertEqual(request["funding_observations"], [])
        self.assertEqual(len(request["mark_index_observations"]), len(OFFSETS))
        self.assertEqual(
            request["trusted_loader_verification"]["status"],
            "INTERNAL_CHAIN_ONLY_NOT_TRUSTED",
        )
        self.assertFalse(request["trusted_loader_verification"]["external_authority_verified"])
        self.assertTrue(request["trusted_loader_verification"]["exact_readback"])
        self.assertTrue(request["trusted_loader_verification"]["gap_free"])
        self.assertEqual(request["orders_created"], 0)
        self.assertFalse(request["private_api_used"])
        self.assertFalse(request["live_execution"])
        self.assertTrue(request["sealed"])
        self.assertEqual(request["evidence_class"], "SEALED_L2_CAPTURE")
        self.assertEqual(
            replay_fixed_long(request)["status"],
            "NOT_RUN_TRUSTED_EVIDENCE_LOADER_REQUIRED",
        )

    def test_loader_has_no_evidence_or_cost_injection_surface(self) -> None:
        runtime = loader_runtime()
        signature = inspect.signature(runtime.load_verified_execution_request)
        self.assertEqual(tuple(signature.parameters), ("capture_dir",))
        with self.assertRaises(TypeError):
            runtime.load_verified_execution_request(  # type: ignore[call-arg]
                make_bundle(self.tmp), taker_fee_bps=0.0
            )

    def test_public_loader_fails_closed_without_external_event_authority(self) -> None:
        runtime = loader_runtime()
        capture_dir = make_bundle(self.tmp)
        with self.assertRaisesRegex(
            runtime.L2EvidenceError, "EXTERNAL_V43_AUTHORITY_VERIFIER_REQUIRED"
        ):
            runtime.load_verified_execution_request(capture_dir)

    def test_tampering_any_bound_evidence_file_fails_closed(self) -> None:
        runtime = loader_runtime()
        names = (
            "raw-frames.jsonl",
            "normalized-depth.json",
            "contract-cost.json",
            "funding.json",
            "mark-index.json",
            "lineage.json",
        )
        for name in names:
            with self.subTest(name=name):
                case_root = self.tmp / name.replace(".", "-")
                case_root.mkdir()
                capture_dir = make_bundle(case_root)
                with (capture_dir / name).open("ab") as handle:
                    handle.write(b" ")
                with self.assertRaises(runtime.L2EvidenceError):
                    runtime.inspect_candidate_execution_request(capture_dir)

    def test_semantically_missing_cost_funding_mark_or_contract_fails_after_reseal(self) -> None:
        runtime = loader_runtime()
        cases: tuple[tuple[str, Callable[[dict[str, Any]], None]], ...] = (
            ("fee", lambda row: row.pop("taker_fee_bps")),
            ("contract", lambda row: row["contract_spec"].pop("maintenance_margin_rate")),
            ("funding", lambda row: row.pop("schedule_status")),
            ("mark", lambda row: row.pop("gap_free")),
        )
        for label, mutator in cases:
            with self.subTest(label=label):
                case_root = self.tmp / label
                case_root.mkdir()
                capture_dir = make_bundle(case_root)
                target = {
                    "fee": "contract-cost.json",
                    "contract": "contract-cost.json",
                    "funding": "funding.json",
                    "mark": "mark-index.json",
                }[label]
                rewrite_json(capture_dir / target, mutator)
                reseal(capture_dir)
                with self.assertRaises(runtime.L2EvidenceError):
                    runtime.inspect_candidate_execution_request(capture_dir)

    def test_resealed_lineage_mismatch_is_not_promoted(self) -> None:
        runtime = loader_runtime()
        capture_dir = make_bundle(self.tmp)

        def break_arming(lineage: dict[str, Any]) -> None:
            lineage["arming"]["event_lineage_hash"] = "0" * 64
            lineage["arming"]["lineage_hash"] = claimed_hash(
                lineage["arming"], "lineage_hash"
            )
            lineage["lineage_hash"] = claimed_hash(lineage, "lineage_hash")

        rewrite_json(capture_dir / "lineage.json", break_arming)
        manifest = json.loads((capture_dir / "manifest.json").read_text(encoding="utf-8"))
        lineage = json.loads((capture_dir / "lineage.json").read_text(encoding="utf-8"))
        manifest["lineage_hashes"]["bundle"] = lineage["lineage_hash"]
        manifest["lineage_hashes"]["arming"] = lineage["arming"]["lineage_hash"]
        canonical_write(capture_dir / "manifest.json", manifest)
        receipt = json.loads(
            (capture_dir / "terminal-receipt.json").read_text(encoding="utf-8")
        )
        receipt["lineage_hashes"] = manifest["lineage_hashes"]
        canonical_write(capture_dir / "terminal-receipt.json", receipt)
        reseal(capture_dir)

        with self.assertRaises(runtime.L2EvidenceError):
            runtime.inspect_candidate_execution_request(capture_dir)

    def test_incomplete_or_gapped_capture_and_unreleased_claim_fail_closed(self) -> None:
        runtime = loader_runtime()
        cases: tuple[tuple[str, str, Callable[[dict[str, Any]], None]], ...] = (
            ("manifest-status", "manifest.json", lambda row: row.__setitem__("status", "FAILED")),
            (
                "coverage-gap",
                "manifest.json",
                lambda row: row["coverage"].__setitem__("gap_free", False),
            ),
            (
                "missing-exit",
                "manifest.json",
                lambda row: row["coverage"]["points"].pop(),
            ),
            (
                "receipt-status",
                "terminal-receipt.json",
                lambda row: row.__setitem__("status", "FAILED"),
            ),
            (
                "claim-not-released",
                "terminal-receipt.json",
                lambda row: row.__setitem__("claim_released", False),
            ),
        )
        for label, name, mutator in cases:
            with self.subTest(label=label):
                case_root = self.tmp / label
                case_root.mkdir()
                capture_dir = make_bundle(case_root)
                rewrite_json(capture_dir / name, mutator)
                if name == "manifest.json":
                    manifest = json.loads(
                        (capture_dir / name).read_text(encoding="utf-8")
                    )
                    manifest["manifest_hash"] = claimed_hash(manifest, "manifest_hash")
                    canonical_write(capture_dir / name, manifest)
                else:
                    receipt = json.loads(
                        (capture_dir / name).read_text(encoding="utf-8")
                    )
                    receipt["receipt_hash"] = claimed_hash(receipt, "receipt_hash")
                    canonical_write(capture_dir / name, receipt)
                if name == "manifest.json":
                    reseal(capture_dir)
                with self.assertRaises(runtime.L2EvidenceError):
                    runtime.inspect_candidate_execution_request(capture_dir)

    def test_noncanonical_readback_fails_even_when_raw_hashes_are_resealed(self) -> None:
        runtime = loader_runtime()
        capture_dir = make_bundle(self.tmp)
        path = capture_dir / "contract-cost.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        reseal(capture_dir)

        with self.assertRaises(runtime.L2EvidenceError):
            runtime.inspect_candidate_execution_request(capture_dir)

    def test_raw_sequence_or_normalized_source_gap_fails_after_reseal(self) -> None:
        runtime = loader_runtime()
        cases: tuple[tuple[str, Callable[[Path], None]], ...] = (
            (
                "raw-gap",
                lambda directory: self._rewrite_raw_sequence(directory, 3, 30),
            ),
            (
                "source-mismatch",
                lambda directory: rewrite_json(
                    directory / "normalized-depth.json",
                    lambda row: row["rows"][0].__setitem__("source_frame_hash", "0" * 64),
                ),
            ),
        )
        for label, mutator in cases:
            with self.subTest(label=label):
                case_root = self.tmp / label
                case_root.mkdir()
                capture_dir = make_bundle(case_root)
                mutator(capture_dir)
                reseal(capture_dir)
                with self.assertRaises(runtime.L2EvidenceError):
                    runtime.inspect_candidate_execution_request(capture_dir)

    def _rewrite_raw_sequence(self, capture_dir: Path, old: int, new: int) -> None:
        path = capture_dir / "raw-frames.jsonl"
        frames = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for frame in frames:
            if frame["sequence"] == old:
                frame["sequence"] = new
                frame["frame_hash"] = claimed_hash(frame, "frame_hash")
        path.write_bytes(
            b"".join(canonical_json_bytes(frame) + b"\n" for frame in frames)
        )

    def test_symlink_path_escape_and_unexpected_files_are_rejected(self) -> None:
        runtime = loader_runtime()
        extra_bundle = make_bundle(self.tmp / "extra-root")
        (extra_bundle / "not-bound.txt").write_text("untrusted", encoding="utf-8")
        with self.assertRaises(runtime.L2EvidenceError):
            runtime.inspect_candidate_execution_request(extra_bundle)

        link_root = self.tmp / "link-root"
        link_root.mkdir()
        capture_dir = make_bundle(link_root)
        outside = self.tmp / "outside.json"
        outside.write_bytes((capture_dir / "funding.json").read_bytes())
        (capture_dir / "funding.json").unlink()
        try:
            os.symlink(outside, capture_dir / "funding.json")
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable on this host: {exc}")
        with self.assertRaises(runtime.L2EvidenceError):
            runtime.inspect_candidate_execution_request(capture_dir)


if __name__ == "__main__":
    unittest.main()
