"""Fixture-first contract for the additive Bybit continuous L2 writer.

The writer under test is deliberately not a production capture entrypoint.  Every
record is supplied as immutable bytes, every output lives below a caller-owned
temporary directory, and the result must remain synthetic/non-acceptance evidence.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MODULE_PATH = SRC / "bybit_l2_writer_v43.py"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    writer = importlib.import_module("bybit_l2_writer_v43")
except ModuleNotFoundError:
    writer = None


T0 = 1_700_000_000
EVENT_HASH = "e" * 64


def snapshot_bytes(*, sequence: int, bid: str = "10", ask: str = "11") -> bytes:
    # Deliberate whitespace proves that the writer preserves source bytes rather
    # than hashing a re-serialized Python object.
    return (
        "{ \"topic\": \"orderbook.50.ABCUSDT\", \"type\": \"snapshot\", "
        f"\"ts\": {T0 * 1000 + 10}, \"cts\": {T0 * 1000 + 5}, "
        "\"data\": {\"s\": \"ABCUSDT\", "
        f"\"b\": [[\"{bid}\", \"2\"]], \"a\": [[\"{ask}\", \"3\"]], "
        f"\"u\": {sequence}, \"seq\": {sequence + 800}}} }}"
    ).encode("utf-8")


def delta_bytes(*, sequence: int, bid: str = "10.5") -> bytes:
    return json.dumps(
        {
            "topic": "orderbook.50.ABCUSDT",
            "type": "delta",
            "ts": T0 * 1000 + 20,
            "cts": T0 * 1000 + 15,
            "data": {
                "s": "ABCUSDT",
                "b": [[bid, "1"]],
                "a": [],
                "u": sequence,
                "seq": sequence + 800,
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


class MonotonicTape:
    def __init__(self, values: list[float]) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class BybitL2WriterFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(writer, "fixture-only Bybit L2 writer module is missing")
        assert writer is not None
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.temp_root = Path(self.temp.name)

    def spec(self, **overrides: Any):
        values = {
            "capture_id": "fixture-bybit-v43",
            "contract_id": "ABCUSDT",
            "event_lineage_hash": EVENT_HASH,
            "t0_ts": T0,
            "window_start_ts": T0 - 60.0,
            "window_end_ts": T0 + 61.0,
            "max_runtime_sec": 30.0,
            "max_messages": 20,
            "max_levels": 50,
        }
        values.update(overrides)
        return writer.FixtureCaptureSpec(**values)

    def source(self, records: list[Any]):
        return writer.StaticBybitL2FixtureSource(records)

    def open(self, epoch: int):
        return writer.FixtureWsRecord.open(epoch)

    def message(
        self,
        epoch: int,
        raw: bytes,
        *,
        received_ts: float | None = None,
        monotonic_ns: int | None = None,
    ):
        return writer.FixtureWsRecord.message(
            epoch,
            raw,
            received_ts=T0 + 0.010 if received_ts is None else received_ts,
            monotonic_ns=1_000_000_000 if monotonic_ns is None else monotonic_ns,
        )

    def close(self, epoch: int):
        return writer.FixtureWsRecord.close(epoch, reason="fixture_eof")

    def run_capture(
        self,
        records: list[Any],
        *,
        name: str = "capture",
        spec: Any | None = None,
        monotonic: Callable[[], float] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, Any], Path, Any]:
        target = self.temp_root / name
        source = self.source(records)
        manifest = writer.run_fixture_bybit_l2_capture(
            self.spec() if spec is None else spec,
            output_dir=target,
            source=source,
            monotonic=(lambda: 0.0) if monotonic is None else monotonic,
            should_stop=(lambda: False) if should_stop is None else should_stop,
        )
        return manifest, target, source

    @staticmethod
    def jsonl(path: Path) -> list[dict[str, Any]]:
        raw = path.read_bytes()
        if not raw:
            return []
        return [json.loads(line) for line in raw.decode("utf-8").splitlines()]

    def test_snapshot_preserves_raw_bytes_hash_chain_and_causal_depth(self) -> None:
        raw = snapshot_bytes(sequence=100)
        manifest, target, _source = self.run_capture(
            [self.open(1), self.message(1, raw), self.close(1)]
        )

        self.assertEqual(
            {path.name for path in target.iterdir()},
            {
                "raw-frames.jsonl",
                "normalized-depth.jsonl",
                "manifest.json",
                "terminal-receipt.json",
            },
        )
        frames = self.jsonl(target / "raw-frames.jsonl")
        depths = self.jsonl(target / "normalized-depth.jsonl")
        self.assertEqual(len(frames), 1)
        self.assertEqual(base64.b64decode(frames[0]["raw_payload_b64"]), raw)
        self.assertEqual(frames[0]["raw_payload_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertIsNone(frames[0]["previous_record_hash"])
        self.assertRegex(frames[0]["record_hash"], r"^[0-9a-f]{64}$")

        self.assertEqual(len(depths), 1)
        depth = depths[0]
        self.assertEqual(depth["source_record_hash"], frames[0]["record_hash"])
        self.assertEqual(depth["connection_epoch"], 1)
        self.assertEqual(depth["venue_sequence"], 100)
        self.assertEqual(depth["bids"], [["10", "2"]])
        self.assertEqual(depth["asks"], [["11", "3"]])
        self.assertTrue(depth["gap_free"])
        self.assertTrue(depth["execution_ready"])
        self.assertRegex(depth["frame_chain_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(manifest["status"], "COMPLETED")
        self.assertTrue(manifest["gap_free"])
        self.assertEqual(manifest["completion_scope"], "FIXTURE_L2_TAPE_ONLY")
        self.assertFalse(manifest["execution_bundle_ready"])
        self.assertFalse(
            manifest["replay_ready"],
            "an L2-only tape lacks fixed-offset cost/funding/mark-index evidence",
        )

    def test_unverifiable_bybit_delta_is_descriptive_and_not_normalized(self) -> None:
        manifest, target, _source = self.run_capture(
            [
                self.open(1),
                self.message(1, snapshot_bytes(sequence=100)),
                self.message(
                    1,
                    delta_bytes(sequence=101),
                    received_ts=T0 + 0.020,
                    monotonic_ns=1_100_000_000,
                ),
                self.close(1),
            ]
        )

        frames = self.jsonl(target / "raw-frames.jsonl")
        depths = self.jsonl(target / "normalized-depth.jsonl")
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[1]["gap_signal"], "CONTINUITY_UNVERIFIABLE")
        self.assertEqual(frames[1]["previous_record_hash"], frames[0]["record_hash"])
        self.assertEqual(len(depths), 1, "writer must not invent a delta predecessor")
        self.assertEqual(manifest["status"], "STOPPED_INCOMPLETE")
        self.assertEqual(manifest["evidence_class"], "DESCRIPTIVE_ONLY")
        self.assertFalse(manifest["gap_free"])
        self.assertFalse(manifest["replay_ready"])

    def test_reconnect_requires_new_epoch_snapshot(self) -> None:
        manifest, target, _source = self.run_capture(
            [
                self.open(1),
                self.message(1, snapshot_bytes(sequence=100)),
                self.close(1),
                self.open(2),
                self.message(
                    2,
                    snapshot_bytes(sequence=200, bid="12", ask="13"),
                    received_ts=T0 + 1.0,
                    monotonic_ns=2_000_000_000,
                ),
                self.close(2),
            ]
        )
        depths = self.jsonl(target / "normalized-depth.jsonl")
        self.assertEqual([row["connection_epoch"] for row in depths], [1, 2])
        self.assertEqual([row["venue_sequence"] for row in depths], [100, 200])
        self.assertEqual([row["connection_epoch"] for row in manifest["epochs"]], [1, 2])
        self.assertTrue(all(row["snapshot_seen"] for row in manifest["epochs"]))
        self.assertEqual(manifest["status"], "COMPLETED")

    def test_delta_as_first_message_of_reconnect_cannot_be_ready(self) -> None:
        manifest, target, _source = self.run_capture(
            [
                self.open(1),
                self.message(1, snapshot_bytes(sequence=100)),
                self.close(1),
                self.open(2),
                self.message(
                    2,
                    delta_bytes(sequence=201),
                    received_ts=T0 + 1.0,
                    monotonic_ns=2_000_000_000,
                ),
                self.close(2),
            ]
        )
        depths = self.jsonl(target / "normalized-depth.jsonl")
        self.assertEqual(len(depths), 1)
        self.assertEqual(manifest["status"], "STOPPED_INCOMPLETE")
        self.assertFalse(manifest["epochs"][1]["snapshot_seen"])
        self.assertFalse(manifest["replay_ready"])

    def test_message_budget_stops_before_consuming_an_extra_record(self) -> None:
        source = self.source(
            [
                self.open(1),
                self.message(1, snapshot_bytes(sequence=100)),
                self.message(1, snapshot_bytes(sequence=101), monotonic_ns=1_100_000_000),
            ]
        )
        target = self.temp_root / "budget"
        manifest = writer.run_fixture_bybit_l2_capture(
            self.spec(max_messages=1),
            output_dir=target,
            source=source,
            monotonic=lambda: 0.0,
            should_stop=lambda: False,
        )
        self.assertEqual(manifest["termination_reason"], "max_messages_exceeded")
        self.assertEqual(manifest["status"], "STOPPED_INCOMPLETE")
        self.assertEqual(source.consumed_count, 2)  # OPEN plus exactly one MESSAGE
        self.assertEqual(len(self.jsonl(target / "raw-frames.jsonl")), 1)

    def test_stop_and_deadline_commit_incomplete_terminal_accounting(self) -> None:
        stopped, stopped_dir, stopped_source = self.run_capture(
            [self.open(1), self.message(1, snapshot_bytes(sequence=100))],
            name="stopped",
            should_stop=lambda: True,
        )
        self.assertEqual(stopped["termination_reason"], "stop_requested")
        self.assertEqual(stopped["status"], "STOPPED_INCOMPLETE")
        self.assertEqual(stopped_source.consumed_count, 0)
        self.assertEqual(self.jsonl(stopped_dir / "raw-frames.jsonl"), [])
        self.assertTrue((stopped_dir / "terminal-receipt.json").is_file())

        deadline, deadline_dir, deadline_source = self.run_capture(
            [self.open(1), self.message(1, snapshot_bytes(sequence=100))],
            name="deadline",
            monotonic=MonotonicTape([0.0, 0.0, 2.0]),
            spec=self.spec(max_runtime_sec=1.0),
        )
        self.assertEqual(deadline["termination_reason"], "max_runtime_sec_exceeded")
        self.assertEqual(deadline["status"], "STOPPED_INCOMPLETE")
        self.assertEqual(deadline_source.consumed_count, 1)  # OPEN only
        self.assertEqual(self.jsonl(deadline_dir / "raw-frames.jsonl"), [])

    def test_output_is_exclusive_external_temp_and_never_overwritten(self) -> None:
        import project_config as config

        existing = self.temp_root / "already-exists"
        existing.mkdir()
        with self.assertRaisesRegex(writer.BybitL2WriterError, "new|exist"):
            writer.run_fixture_bybit_l2_capture(
                self.spec(),
                output_dir=existing,
                source=self.source([]),
            )
        with self.assertRaisesRegex(writer.BybitL2WriterError, "outside|repository"):
            writer.run_fixture_bybit_l2_capture(
                self.spec(capture_id="inside-repo"),
                output_dir=ROOT / "must-not-be-created-by-v43-writer-test",
                source=self.source([]),
            )
        self.assertFalse((ROOT / "must-not-be-created-by-v43-writer-test").exists())
        protected_targets = (
            Path(config.CONTROL_ROOT) / "must-not-be-created-by-v43-writer-test",
            Path(config.CAPTURE_ROOT) / "must-not-be-created-by-v43-writer-test",
        )
        for protected in protected_targets:
            with self.subTest(protected=protected):
                with self.assertRaisesRegex(writer.BybitL2WriterError, "production"):
                    writer.run_fixture_bybit_l2_capture(
                        self.spec(capture_id="protected-path"),
                        output_dir=protected,
                        source=self.source([]),
                    )
                self.assertFalse(protected.exists())

    def test_research_only_manifest_receipt_and_api_have_no_live_authority(self) -> None:
        manifest, target, _source = self.run_capture(
            [self.open(1), self.message(1, snapshot_bytes(sequence=100)), self.close(1)]
        )
        receipt = json.loads((target / "terminal-receipt.json").read_text(encoding="utf-8"))
        for payload in (manifest, receipt):
            self.assertEqual(payload["orders_created"], 0)
            self.assertFalse(payload["network_used"])
            self.assertFalse(payload["private_api_used"])
            self.assertFalse(payload["live_execution"])
            self.assertFalse(payload["claim_used"])
            self.assertFalse(payload["capture_token_used"])
            self.assertFalse(payload["plan_activated"])

        signature = inspect.signature(writer.run_fixture_bybit_l2_capture)
        forbidden_parameters = {"url", "socket", "opener", "plan", "token", "claim"}
        self.assertTrue(forbidden_parameters.isdisjoint(signature.parameters))
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertTrue(
            {"socket", "public_ws", "risk_gate", "global_market_writer_claim"}.isdisjoint(imports)
        )

    def test_identical_fixture_outputs_are_byte_deterministic(self) -> None:
        records = [self.open(1), self.message(1, snapshot_bytes(sequence=100)), self.close(1)]
        _first, first, _ = self.run_capture(records, name="deterministic-a")
        _second, second, _ = self.run_capture(records, name="deterministic-b")
        self.assertEqual(
            {path.name: path.read_bytes() for path in first.iterdir()},
            {path.name: path.read_bytes() for path in second.iterdir()},
        )

    def test_writer_remains_unbound_to_active_v42(self) -> None:
        import project_config as config
        import risk_gate

        plan = risk_gate.load_and_verify_plan()
        self.assertEqual(plan["plan_id"], "premarket_perp_capture_20260822_v42")
        self.assertNotIn(
            "src/bybit_l2_writer_v43.py",
            {path for _role, path in config.BOUND_RUNTIME_FILES},
        )


if __name__ == "__main__":
    unittest.main()
