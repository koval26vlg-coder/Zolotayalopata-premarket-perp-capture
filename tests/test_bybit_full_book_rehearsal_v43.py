"""Offline contract for the unbound Bybit full-order-book v43 rehearsal.

The rehearsal is deliberately narrower than a capture: every byte is supplied by
an exact in-memory transcript and every artifact is written below a new temporary
directory.  A synchronized book proves only the REST/WS continuity algorithm; it
does not create replay, execution, acceptance, or activation authority.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MODULE_PATH = SRC / "bybit_full_book_rehearsal_v43.py"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    rehearsal = importlib.import_module("bybit_full_book_rehearsal_v43")
except ModuleNotFoundError:
    rehearsal = None


SYMBOL = "ABCUSDT"
BASE_MS = 1_700_000_000_000
BASE_TS = BASE_MS / 1000.0


def ws_delta_bytes(
    update_id: int,
    sequence: int,
    *,
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
) -> bytes:
    return json.dumps(
        {
            "topic": f"orderbook.full.{SYMBOL}",
            "type": "delta",
            "ts": BASE_MS + update_id,
            "cts": BASE_MS + update_id - 1,
            "data": {
                "s": SYMBOL,
                "b": [] if bids is None else bids,
                "a": [] if asks is None else asks,
                "u": update_id,
                "seq": sequence,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def rest_snapshot_bytes(
    update_id: int,
    sequence: int,
    *,
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
) -> bytes:
    return json.dumps(
        {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "s": SYMBOL,
                "b": [["10", "2"]] if bids is None else bids,
                "a": [["11", "3"]] if asks is None else asks,
                "ts": BASE_MS + update_id + 3,
                "cts": BASE_MS + update_id + 2,
                "u": update_id,
                "seq": sequence,
            },
            "time": BASE_MS + update_id + 4,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class BybitFullBookRehearsalV43Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(rehearsal, "offline full-book rehearsal module is missing")
        assert rehearsal is not None
        self.temp = tempfile.TemporaryDirectory(prefix="full-book-rehearsal-test-")
        self.addCleanup(self.temp.cleanup)
        self.temp_root = Path(self.temp.name)

    def spec(self, **overrides: Any):
        values = {
            "rehearsal_id": "bybit-full-book-v43-fixture",
            "contract_id": SYMBOL,
            "max_levels_per_side": 50,
            "max_buffered_deltas": 100,
        }
        values.update(overrides)
        return rehearsal.FullBookRehearsalSpec(**values)

    @staticmethod
    def open(epoch: int):
        return rehearsal.FullBookTranscriptRecord.open(epoch)

    @staticmethod
    def close(epoch: int):
        return rehearsal.FullBookTranscriptRecord.close(epoch, reason="fixture_eof")

    @staticmethod
    def ws(
        epoch: int,
        raw: bytes,
        *,
        received_ts: float,
        monotonic_ns: int,
    ):
        return rehearsal.FullBookTranscriptRecord.ws_delta(
            epoch,
            raw,
            received_ts=received_ts,
            monotonic_ns=monotonic_ns,
        )

    @staticmethod
    def rest_attempt(epoch: int, *, received_ts: float, monotonic_ns: int):
        return rehearsal.FullBookTranscriptRecord.rest_attempt(
            epoch,
            received_ts=received_ts,
            monotonic_ns=monotonic_ns,
        )

    @staticmethod
    def rest_snapshot(
        epoch: int,
        raw: bytes,
        *,
        received_ts: float,
        monotonic_ns: int,
    ):
        return rehearsal.FullBookTranscriptRecord.rest_snapshot(
            epoch,
            raw,
            received_ts=received_ts,
            monotonic_ns=monotonic_ns,
        )

    def happy_records(self) -> list[Any]:
        return [
            self.open(1),
            self.ws(
                1,
                ws_delta_bytes(100, 900),
                received_ts=BASE_TS + 0.100,
                monotonic_ns=1_000_000_000,
            ),
            self.ws(
                1,
                ws_delta_bytes(101, 905, bids=[["10.5", "1"]]),
                received_ts=BASE_TS + 0.110,
                monotonic_ns=1_100_000_000,
            ),
            self.rest_attempt(
                1,
                received_ts=BASE_TS + 0.120,
                monotonic_ns=1_200_000_000,
            ),
            self.rest_snapshot(
                1,
                rest_snapshot_bytes(100, 900),
                received_ts=BASE_TS + 0.130,
                monotonic_ns=1_300_000_000,
            ),
            self.close(1),
        ]

    def run_records(
        self,
        records: list[Any],
        *,
        name: str = "bundle",
        spec: Any | None = None,
    ) -> tuple[dict[str, Any], Path]:
        target = self.temp_root / name
        manifest = rehearsal.run_unbound_bybit_full_book_rehearsal(
            self.spec() if spec is None else spec,
            output_dir=target,
            transcript=rehearsal.StaticBybitFullBookTranscript(records),
        )
        return manifest, target

    @staticmethod
    def jsonl(path: Path) -> list[dict[str, Any]]:
        raw = path.read_bytes()
        if not raw:
            return []
        return [json.loads(line) for line in raw.decode("utf-8").splitlines()]

    def test_exact_rest_bridge_applies_already_buffered_delta(self) -> None:
        manifest, target = self.run_records(self.happy_records())

        self.assertEqual(
            {path.name for path in target.iterdir()},
            {
                "raw-ingress.jsonl",
                "sync-decisions.jsonl",
                "normalized-depth.jsonl",
                "manifest.json",
                "terminal-receipt.json",
            },
        )
        self.assertEqual(manifest["status"], "FULL_BOOK_SYNC_ONLY")
        self.assertEqual(manifest["completion_scope"], "OFFLINE_UNBOUND_FULL_BOOK_SYNC_V43")
        self.assertEqual(manifest["termination_reason"], "source_exhausted")
        self.assertEqual(manifest["rest_request_path"], "/v5/market/full_orderbook")
        self.assertEqual(manifest["rest_request_category"], "linear")
        self.assertEqual(
            manifest["rest_request_provenance"],
            "UNBOUND_EXACT_REQUEST_DECLARATION",
        )
        self.assertEqual(manifest["ws_connection_path"], "/v5/public/linear")
        self.assertEqual(manifest["ws_connection_category"], "linear")
        self.assertEqual(
            manifest["ws_connection_provenance"],
            "UNBOUND_EXACT_CONNECTION_DECLARATION",
        )
        self.assertEqual(manifest["rpi_coverage"], "RPI_EXCLUDED_BY_BYBIT_PUBLIC_API")
        self.assertFalse(manifest["replay_ready"])
        self.assertFalse(manifest["execution_bundle_ready"])
        self.assertFalse(manifest["acceptance_capable"])

        raw_rows = self.jsonl(target / "raw-ingress.jsonl")
        decisions = self.jsonl(target / "sync-decisions.jsonl")
        depths = self.jsonl(target / "normalized-depth.jsonl")
        self.assertEqual([row["record_kind"] for row in raw_rows], [
            "OPEN",
            "WS_DELTA",
            "WS_DELTA",
            "REST_ATTEMPT",
            "REST_SNAPSHOT",
            "CLOSE",
        ])
        self.assertEqual([row["transport_epoch"] for row in raw_rows], [1] * 6)
        self.assertEqual([row["book_generation"] for row in raw_rows], [1] * 6)
        self.assertEqual(raw_rows[0]["connection_path"], "/v5/public/linear")
        self.assertEqual(raw_rows[1]["connection_category"], "linear")
        self.assertEqual(
            raw_rows[1]["connection_provenance"],
            "UNBOUND_EXACT_CONNECTION_DECLARATION",
        )
        self.assertEqual(raw_rows[3]["request_path"], "/v5/market/full_orderbook")
        self.assertEqual(raw_rows[3]["request_category"], "linear")
        self.assertEqual(
            raw_rows[3]["request_provenance"],
            "UNBOUND_EXACT_REQUEST_DECLARATION",
        )
        self.assertEqual(
            base64.b64decode(raw_rows[1]["raw_payload_b64"]),
            ws_delta_bytes(100, 900),
        )
        self.assertEqual(
            raw_rows[1]["raw_payload_sha256"],
            hashlib.sha256(ws_delta_bytes(100, 900)).hexdigest(),
        )
        self.assertTrue(any(row["decision"] == "SYNCHRONIZED" for row in decisions))
        self.assertEqual(len(depths), 1)
        self.assertEqual(depths[0]["update_id"], 101)
        self.assertEqual(depths[0]["cross_sequence"], 905)
        self.assertEqual(depths[0]["bids"], [["10.5", "1"], ["10", "2"]])
        self.assertEqual(depths[0]["asks"], [["11", "3"]])
        self.assertEqual(depths[0]["transport_epoch"], 1)
        self.assertEqual(depths[0]["book_generation"], 1)
        self.assertTrue(depths[0]["book_structurally_ready"])
        self.assertFalse(depths[0]["book_execution_ready"])
        self.assertEqual(depths[0]["rpi_coverage"], "RPI_EXCLUDED_BY_BYBIT_PUBLIC_API")
        self.assertRegex(depths[0]["evidence_chain_sha256"], r"^[0-9a-f]{64}$")
        for rows, previous_key, hash_key in (
            (raw_rows, "previous_record_hash", "record_hash"),
            (decisions, "previous_decision_hash", "decision_hash"),
            (depths, "previous_depth_hash", "depth_hash"),
        ):
            self.assertIsNone(rows[0][previous_key])
            self.assertTrue(all(len(row[hash_key]) == 64 for row in rows))

    def test_update_gap_invalidates_and_commits_incomplete_terminal_bundle(self) -> None:
        records = self.happy_records()[:-1]
        records.extend(
            [
                self.ws(
                    1,
                    ws_delta_bytes(103, 910),
                    received_ts=BASE_TS + 0.140,
                    monotonic_ns=1_400_000_000,
                ),
                self.close(1),
            ]
        )
        manifest, target = self.run_records(records, name="gap")

        self.assertEqual(manifest["status"], "STOPPED_INCOMPLETE")
        self.assertEqual(manifest["termination_reason"], "WS_U_GAP")
        self.assertTrue(manifest["resync_required"])
        self.assertEqual(manifest["final_sync_status"], "RESYNC_REQUIRED")
        self.assertTrue((target / "terminal-receipt.json").is_file())
        self.assertEqual(len(self.jsonl(target / "normalized-depth.jsonl")), 1)

    def test_update_id_one_forces_resync_and_cannot_remain_ready(self) -> None:
        records = self.happy_records()[:-1]
        records.extend(
            [
                self.ws(
                    1,
                    ws_delta_bytes(1, 911),
                    received_ts=BASE_TS + 0.140,
                    monotonic_ns=1_400_000_000,
                ),
                self.close(1),
            ]
        )
        manifest, _target = self.run_records(records, name="u-one")

        self.assertEqual(manifest["status"], "STOPPED_INCOMPLETE")
        self.assertEqual(manifest["termination_reason"], "WS_U_ONE_RESET")
        self.assertTrue(manifest["resync_required"])
        self.assertFalse(manifest["replay_ready"])

    def test_reconnect_advances_epoch_and_generation_then_requires_new_bridge(self) -> None:
        records = self.happy_records()
        records.extend([self.open(2), self.close(2)])
        manifest, target = self.run_records(records, name="reconnect")

        self.assertEqual(manifest["status"], "STOPPED_INCOMPLETE")
        self.assertEqual(manifest["termination_reason"], "RECONNECT_REQUIRES_RESYNC")
        self.assertEqual(manifest["final_transport_epoch"], 2)
        self.assertEqual(manifest["final_book_generation"], 2)
        raw_rows = self.jsonl(target / "raw-ingress.jsonl")
        self.assertEqual(raw_rows[-2]["record_kind"], "OPEN")
        self.assertEqual(raw_rows[-2]["transport_epoch"], 2)
        self.assertEqual(raw_rows[-2]["book_generation"], 2)
        self.assertFalse(manifest["replay_ready"])

    def test_malformed_raw_is_persisted_before_parse_and_gets_terminal_receipt(self) -> None:
        malformed = b'{"topic": "orderbook.full.ABCUSDT", bad json'
        manifest, target = self.run_records(
            [
                self.open(1),
                self.ws(
                    1,
                    malformed,
                    received_ts=BASE_TS + 0.1,
                    monotonic_ns=1_000_000_000,
                ),
            ],
            name="malformed",
        )

        self.assertEqual(manifest["status"], "STOPPED_INCOMPLETE")
        self.assertEqual(manifest["termination_reason"], "PARSE_OR_SYNC_ERROR")
        self.assertEqual(manifest["error_class"], "BybitFullBookSchemaError")
        raw_rows = self.jsonl(target / "raw-ingress.jsonl")
        self.assertEqual(len(raw_rows), 2)
        self.assertEqual(base64.b64decode(raw_rows[1]["raw_payload_b64"]), malformed)
        self.assertEqual(
            raw_rows[1]["raw_payload_sha256"], hashlib.sha256(malformed).hexdigest()
        )
        decisions = self.jsonl(target / "sync-decisions.jsonl")
        self.assertEqual(decisions[-1]["decision"], "ERROR")
        self.assertEqual(decisions[-1]["source_record_sequence"], 2)
        self.assertTrue((target / "manifest.json").is_file())
        self.assertTrue((target / "terminal-receipt.json").is_file())

    def test_control_record_is_persisted_before_state_validation(self) -> None:
        manifest, target = self.run_records(
            [self.open(1), self.open(1)],
            name="invalid-second-open",
        )

        self.assertEqual(manifest["status"], "STOPPED_INCOMPLETE")
        self.assertEqual(manifest["termination_reason"], "PARSE_OR_SYNC_ERROR")
        raw_rows = self.jsonl(target / "raw-ingress.jsonl")
        self.assertEqual([row["record_kind"] for row in raw_rows], ["OPEN", "OPEN"])
        self.assertEqual([row["record_sequence"] for row in raw_rows], [1, 2])
        self.assertTrue((target / "terminal-receipt.json").is_file())

    def test_semantic_result_hash_is_deterministic_across_output_directories(self) -> None:
        first, first_dir = self.run_records(self.happy_records(), name="deterministic-a")
        second, second_dir = self.run_records(self.happy_records(), name="deterministic-b")

        self.assertEqual(first["semantic_result_hash"], second["semantic_result_hash"])
        self.assertEqual(
            {path.name: path.read_bytes() for path in first_dir.iterdir()},
            {path.name: path.read_bytes() for path in second_dir.iterdir()},
        )

    def test_manifest_and_receipt_are_exactly_read_back_and_research_only(self) -> None:
        manifest, target = self.run_records(self.happy_records(), name="terminal")
        manifest_raw = (target / "manifest.json").read_bytes()
        receipt = json.loads((target / "terminal-receipt.json").read_text(encoding="utf-8"))

        self.assertEqual(receipt["status"], "FULL_BOOK_SYNC_ONLY")
        self.assertEqual(receipt["manifest_sha256"], hashlib.sha256(manifest_raw).hexdigest())
        self.assertEqual(receipt["manifest_hash"], manifest["manifest_hash"])
        self.assertEqual(receipt["semantic_result_hash"], manifest["semantic_result_hash"])
        for payload in (manifest, receipt):
            self.assertFalse(payload["network_used"])
            self.assertEqual(payload["orders_created"], 0)
            self.assertFalse(payload["private_api_used"])
            self.assertFalse(payload["claim_used"])
            self.assertFalse(payload["capture_token_used"])
            self.assertFalse(payload["plan_activated"])
            self.assertFalse(payload["replay_ready"])
            self.assertFalse(payload["execution_bundle_ready"])
            self.assertFalse(payload["acceptance_capable"])

        for file_name, expected in manifest["file_sha256"].items():
            self.assertEqual(
                hashlib.sha256((target / file_name).read_bytes()).hexdigest(), expected
            )

    def test_output_must_be_new_external_plain_temporary_path(self) -> None:
        existing = self.temp_root / "already-exists"
        existing.mkdir()
        with self.assertRaisesRegex(rehearsal.FullBookRehearsalError, "new|exist"):
            rehearsal.run_unbound_bybit_full_book_rehearsal(
                self.spec(),
                output_dir=existing,
                transcript=rehearsal.StaticBybitFullBookTranscript([]),
            )

        inside_repo = ROOT / "must-not-be-created-by-full-book-rehearsal"
        with self.assertRaisesRegex(rehearsal.FullBookRehearsalError, "repository|protected"):
            rehearsal.run_unbound_bybit_full_book_rehearsal(
                self.spec(),
                output_dir=inside_repo,
                transcript=rehearsal.StaticBybitFullBookTranscript([]),
            )
        self.assertFalse(inside_repo.exists())

        control_root = Path(
            os.environ.get("PREMARKET_CAPTURE_CONTROL_ROOT", "C:/Users/koval/Documents/ZolotyayLopata")
        )
        protected = control_root / "must-not-be-created-by-full-book-rehearsal"
        with self.assertRaisesRegex(rehearsal.FullBookRehearsalError, "production|protected"):
            rehearsal.run_unbound_bybit_full_book_rehearsal(
                self.spec(),
                output_dir=protected,
                transcript=rehearsal.StaticBybitFullBookTranscript([]),
            )
        self.assertFalse(protected.exists())

    def test_partial_output_open_failure_retains_forensic_bundle_without_delete(self) -> None:
        target = self.temp_root / "partial-open"
        original_open = Path.open

        def fail_second(path: Path, *args: Any, **kwargs: Any):
            if path.name == "sync-decisions.jsonl":
                raise OSError("fixture open failure")
            return original_open(path, *args, **kwargs)

        with (
            mock.patch.object(Path, "open", new=fail_second),
            mock.patch.object(
                Path,
                "unlink",
                side_effect=AssertionError("failure cleanup must never unlink by path"),
            ),
            mock.patch.object(
                Path,
                "rmdir",
                side_effect=AssertionError("failure cleanup must retain its temp directory"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "fixture open failure"):
                rehearsal.run_unbound_bybit_full_book_rehearsal(
                    self.spec(),
                    output_dir=target,
                    transcript=rehearsal.StaticBybitFullBookTranscript([]),
                )

        self.assertTrue(target.is_dir())
        self.assertEqual((target / "raw-ingress.jsonl").read_bytes(), b"")
        self.assertFalse((target / "sync-decisions.jsonl").exists())

    def test_partial_open_cleanup_preserves_foreign_raced_file(self) -> None:
        target = self.temp_root / "foreign-race"
        original_open = Path.open

        def create_foreign_then_fail(path: Path, *args: Any, **kwargs: Any):
            mode = args[0] if args else kwargs.get("mode", "r")
            if path.name == "sync-decisions.jsonl" and mode in {"xb", "x+b"}:
                descriptor = os.open(
                    path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                try:
                    os.write(descriptor, b"FOREIGN-SENTINEL")
                finally:
                    os.close(descriptor)
                raise FileExistsError("foreign file won the create race")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", new=create_foreign_then_fail):
            with self.assertRaisesRegex(FileExistsError, "foreign file"):
                rehearsal.run_unbound_bybit_full_book_rehearsal(
                    self.spec(),
                    output_dir=target,
                    transcript=rehearsal.StaticBybitFullBookTranscript([]),
                )

        sentinel = target / "sync-decisions.jsonl"
        self.assertEqual(sentinel.read_bytes(), b"FOREIGN-SENTINEL")
        self.assertEqual((target / "raw-ingress.jsonl").read_bytes(), b"")

    @unittest.skipUnless(os.name == "nt", "junction race is Windows-specific")
    def test_parent_junction_swap_during_create_cannot_redirect_bundle(self) -> None:
        parent = self.temp_root / "plain-parent"
        backup = self.temp_root / "plain-parent-backup"
        redirect = self.temp_root / "redirect-destination"
        parent.mkdir()
        redirect.mkdir()
        target = parent / "bundle"
        original_mkdir = Path.mkdir

        def swap_parent_then_create(path: Path, *args: Any, **kwargs: Any):
            if path == target:
                parent.rename(backup)
                created = subprocess.run(
                    [
                        r"C:\Windows\System32\cmd.exe",
                        "/d",
                        "/c",
                        "mklink",
                        "/J",
                        str(parent),
                        str(redirect),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if created.returncode != 0:
                    raise OSError(f"junction fixture failed: {created.stderr}")
            return original_mkdir(path, *args, **kwargs)

        try:
            with mock.patch.object(Path, "mkdir", new=swap_parent_then_create):
                with self.assertRaises(rehearsal.FullBookRehearsalError):
                    rehearsal.run_unbound_bybit_full_book_rehearsal(
                        self.spec(),
                        output_dir=target,
                        transcript=rehearsal.StaticBybitFullBookTranscript([]),
                    )
            self.assertFalse((redirect / "bundle").exists())
        finally:
            if getattr(os.path, "isjunction", lambda _path: False)(parent):
                parent.rmdir()
            if backup.exists() and not parent.exists():
                backup.rename(parent)

    @unittest.skipUnless(os.name == "nt", "junction race is Windows-specific")
    def test_parent_cannot_be_relocated_after_bundle_creation(self) -> None:
        parent = self.temp_root / "held-parent"
        redirect_root = self.temp_root / "relocated"
        parent.mkdir()
        redirect_root.mkdir()
        target = parent / "bundle"
        relocated_parent = redirect_root / parent.name
        original_open = Path.open
        attempted = False

        def relocate_parent_before_first_artifact(path: Path, *args: Any, **kwargs: Any):
            nonlocal attempted
            if path.name == "raw-ingress.jsonl" and not attempted:
                attempted = True
                parent.rename(relocated_parent)
                created = subprocess.run(
                    [
                        r"C:\Windows\System32\cmd.exe",
                        "/d",
                        "/c",
                        "mklink",
                        "/J",
                        str(parent),
                        str(relocated_parent),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if created.returncode != 0:
                    raise OSError(f"junction fixture failed: {created.stderr}")
            return original_open(path, *args, **kwargs)

        try:
            with mock.patch.object(Path, "open", new=relocate_parent_before_first_artifact):
                with self.assertRaises((OSError, rehearsal.FullBookRehearsalError)):
                    rehearsal.run_unbound_bybit_full_book_rehearsal(
                        self.spec(),
                        output_dir=target,
                        transcript=rehearsal.StaticBybitFullBookTranscript(
                            self.happy_records()
                        ),
                    )
            self.assertTrue(attempted)
            self.assertFalse((relocated_parent / "bundle").exists())
        finally:
            if getattr(os.path, "isjunction", lambda _path: False)(parent):
                parent.rmdir()
            if relocated_parent.exists() and not parent.exists():
                relocated_parent.rename(parent)

    @unittest.skipUnless(os.name == "nt", "junction race is Windows-specific")
    def test_created_bundle_directory_is_pinned_against_child_redirect(self) -> None:
        target = self.temp_root / "held-bundle"
        moved = self.temp_root / "held-bundle-moved"
        redirect = self.temp_root / "child-redirect"
        redirect.mkdir()
        original_open = Path.open
        attempted = False

        def redirect_bundle_before_first_artifact(path: Path, *args: Any, **kwargs: Any):
            nonlocal attempted
            if path.name == "raw-ingress.jsonl" and not attempted:
                attempted = True
                target.rename(moved)
                created = subprocess.run(
                    [
                        r"C:\Windows\System32\cmd.exe",
                        "/d",
                        "/c",
                        "mklink",
                        "/J",
                        str(target),
                        str(redirect),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if created.returncode != 0:
                    raise OSError(f"junction fixture failed: {created.stderr}")
            return original_open(path, *args, **kwargs)

        try:
            with mock.patch.object(Path, "open", new=redirect_bundle_before_first_artifact):
                with self.assertRaises((OSError, rehearsal.FullBookRehearsalError)):
                    rehearsal.run_unbound_bybit_full_book_rehearsal(
                        self.spec(),
                        output_dir=target,
                        transcript=rehearsal.StaticBybitFullBookTranscript(
                            self.happy_records()
                        ),
                    )
            self.assertTrue(attempted)
            self.assertFalse((redirect / "raw-ingress.jsonl").exists())
        finally:
            if getattr(os.path, "isjunction", lambda _path: False)(target):
                target.rmdir()
            if moved.exists() and not target.exists():
                moved.rename(target)

    def test_child_pin_rejects_plain_directory_substitution_before_enter(self) -> None:
        target = self.temp_root / "plain-substitution"
        moved = self.temp_root / "plain-substitution-owned"
        original_enter = rehearsal._PinnedPlainDirectory.__enter__
        substituted = False

        def substitute_before_child_pin(pin: Any):
            nonlocal substituted
            if pin.path == target and not substituted:
                substituted = True
                target.rename(moved)
                target.mkdir()
            return original_enter(pin)

        with mock.patch.object(
            rehearsal._PinnedPlainDirectory,
            "__enter__",
            new=substitute_before_child_pin,
        ):
            with self.assertRaises(rehearsal.FullBookRehearsalError):
                rehearsal.run_unbound_bybit_full_book_rehearsal(
                    self.spec(),
                    output_dir=target,
                    transcript=rehearsal.StaticBybitFullBookTranscript(
                        self.happy_records()
                    ),
                )

        self.assertTrue(substituted)
        self.assertFalse((target / "raw-ingress.jsonl").exists())

    def test_evidence_hashes_cannot_be_sealed_from_replaced_path_bytes(self) -> None:
        target = self.temp_root / "replacement-race"
        original_read_bytes = Path.read_bytes
        replaced = False

        def replace_raw_before_path_read(path: Path) -> bytes:
            nonlocal replaced
            if path.name == "raw-ingress.jsonl" and not replaced:
                replaced = True
                path.write_bytes(b"FOREIGN-SENTINEL\n")
            return original_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", new=replace_raw_before_path_read):
            manifest = rehearsal.run_unbound_bybit_full_book_rehearsal(
                self.spec(),
                output_dir=target,
                transcript=rehearsal.StaticBybitFullBookTranscript(self.happy_records()),
            )

        self.assertFalse(replaced, "owned evidence must be read from its pinned handle")
        self.assertEqual(manifest["status"], "FULL_BOOK_SYNC_ONLY")
        self.assertNotEqual((target / "raw-ingress.jsonl").read_bytes(), b"FOREIGN-SENTINEL\n")

    def test_in_place_evidence_mutation_before_manifest_fails_closed(self) -> None:
        target = self.temp_root / "in-place-race"
        original_write_exclusive = rehearsal._write_exclusive
        attempted = False

        def mutate_before_manifest(path: Path, payload: Any, **kwargs: Any):
            nonlocal attempted
            if path.name == "manifest.json" and not attempted:
                attempted = True
                (path.parent / "raw-ingress.jsonl").write_bytes(b"FOREIGN-IN-PLACE\n")
            return original_write_exclusive(path, payload, **kwargs)

        with mock.patch.object(
            rehearsal,
            "_write_exclusive",
            new=mutate_before_manifest,
        ):
            with self.assertRaises(OSError):
                rehearsal.run_unbound_bybit_full_book_rehearsal(
                    self.spec(),
                    output_dir=target,
                    transcript=rehearsal.StaticBybitFullBookTranscript(
                        self.happy_records()
                    ),
                )

        self.assertTrue(attempted)
        self.assertFalse((target / "terminal-receipt.json").exists())

    def test_manifest_is_pinned_until_terminal_receipt_is_sealed(self) -> None:
        target = self.temp_root / "manifest-race"
        original_write_exclusive = rehearsal._write_exclusive
        attempted = False

        def mutate_manifest_before_receipt(path: Path, payload: Any, **kwargs: Any):
            nonlocal attempted
            if path.name == "terminal-receipt.json" and not attempted:
                attempted = True
                (path.parent / "manifest.json").write_bytes(b"FOREIGN-MANIFEST\n")
            return original_write_exclusive(path, payload, **kwargs)

        with mock.patch.object(
            rehearsal,
            "_write_exclusive",
            new=mutate_manifest_before_receipt,
        ):
            with self.assertRaises(OSError):
                rehearsal.run_unbound_bybit_full_book_rehearsal(
                    self.spec(),
                    output_dir=target,
                    transcript=rehearsal.StaticBybitFullBookTranscript(
                        self.happy_records()
                    ),
                )

        self.assertTrue(attempted)

    def test_terminal_receipt_is_read_back_after_exclusive_write(self) -> None:
        target = self.temp_root / "receipt-race"
        original_write_exclusive = rehearsal._write_exclusive
        attempted = False

        def mutate_receipt_after_write(path: Path, payload: Any, **kwargs: Any):
            nonlocal attempted
            result = original_write_exclusive(path, payload, **kwargs)
            if path.name == "terminal-receipt.json" and not attempted:
                attempted = True
                path.write_bytes(b"FOREIGN-RECEIPT\n")
            return result

        with mock.patch.object(
            rehearsal,
            "_write_exclusive",
            new=mutate_receipt_after_write,
        ):
            with self.assertRaises(rehearsal.FullBookRehearsalError):
                rehearsal.run_unbound_bybit_full_book_rehearsal(
                    self.spec(),
                    output_dir=target,
                    transcript=rehearsal.StaticBybitFullBookTranscript(
                        self.happy_records()
                    ),
                )

        self.assertTrue(attempted)

    def test_transcript_records_are_detached_before_raw_and_sync_processing(self) -> None:
        records = self.happy_records()
        caller_delta = records[2]
        transcript = rehearsal.StaticBybitFullBookTranscript(records)
        original_append = rehearsal._append_durable
        mutated = False

        def mutate_caller_after_raw_write(handle: Any, payload: Any) -> bytes:
            nonlocal mutated
            raw = original_append(handle, payload)
            if payload.get("record_kind") == "WS_DELTA" and payload.get("record_sequence") == 3:
                object.__setattr__(
                    caller_delta,
                    "raw_payload",
                    ws_delta_bytes(101, 905, bids=[["10.5", "999"]]),
                )
                mutated = True
            return raw

        target = self.temp_root / "transcript-alias"
        with mock.patch.object(rehearsal, "_append_durable", new=mutate_caller_after_raw_write):
            manifest = rehearsal.run_unbound_bybit_full_book_rehearsal(
                self.spec(),
                output_dir=target,
                transcript=transcript,
            )

        self.assertTrue(mutated)
        self.assertEqual(manifest["status"], "FULL_BOOK_SYNC_ONLY")
        depths = self.jsonl(target / "normalized-depth.jsonl")
        self.assertEqual(depths[0]["bids"][0], ["10.5", "1"])

    def test_public_api_and_imports_have_no_network_plan_claim_token_or_order_authority(self) -> None:
        signature = inspect.signature(rehearsal.run_unbound_bybit_full_book_rehearsal)
        self.assertEqual(set(signature.parameters), {"spec", "output_dir", "transcript"})

        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertTrue(
            {
                "socket",
                "urllib",
                "requests",
                "httpx",
                "public_http",
                "public_ws",
                "risk_gate",
                "global_market_writer_claim",
            }.isdisjoint(imports)
        )
        source = MODULE_PATH.read_text(encoding="utf-8").lower()
        self.assertNotIn('os.environ.get("premarket_capture_control_root"', source)
        self.assertNotIn('os.environ.get("premarket_capture_root"', source)
        for forbidden in ("place_order", "submit_order", "api_key", "api_secret"):
            self.assertNotIn(forbidden, source)

        import project_config

        self.assertNotIn(
            "src/bybit_full_book_rehearsal_v43.py",
            {path for _role, path in project_config.BOUND_RUNTIME_FILES},
        )


if __name__ == "__main__":
    unittest.main()
