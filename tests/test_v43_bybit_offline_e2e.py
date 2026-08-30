"""Offline end-to-end proof for the additive Bybit v43 rehearsal package.

This suite intentionally exercises two different evidence classes.  A complete
synthetic sealed snapshot bundle may cross the fixture-only authority boundary and
reach fixed replay exactly once.  A continuous Bybit tape containing a delta whose
predecessor is not provable must stop at DESCRIPTIVE_ONLY and can never enter that
boundary.
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import l2_evidence  # noqa: E402
import project_config as config  # noqa: E402
import risk_gate  # noqa: E402
from tests.test_bybit_l2_writer_v43 import (  # noqa: E402
    EVENT_HASH,
    T0,
    delta_bytes,
    snapshot_bytes,
)
from tests.test_v43_fixture_authority import (  # noqa: E402
    make_authority_bundle,
)


class BybitV43OfflineEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_sealed_bybit_fixture_crosses_authority_once_and_replays_four_horizons(
        self,
    ) -> None:
        authority = importlib.import_module("v43_fixture_authority")
        replay = importlib.import_module("v43_verified_replay")
        bundle, plan_file_sha256 = make_authority_bundle(self.root)

        handoff = authority.verify_fixture_authority_bundle(
            bundle,
            expected_plan_file_sha256=plan_file_sha256,
        )
        result = replay.replay_verified_fixture(handoff)

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(
            [row["offset_sec"] for row in result["horizons"]],
            [0, 5, 15, 60],
        )
        self.assertEqual(result["evidence_mode"], "FIXTURE_REHEARSAL_ONLY")
        self.assertTrue(result["fixture_external_authority_verified"])
        self.assertFalse(result["production_external_authority_verified"])
        self.assertFalse(result["acceptance_capable"])
        self.assertEqual(result["orders_created"], 0)
        self.assertFalse(result["private_api_used"])
        self.assertFalse(result["live_execution"])

        with self.assertRaises(replay.V43VerifiedReplayError):
            replay.replay_verified_fixture(handoff)
        with self.assertRaisesRegex(
            l2_evidence.L2EvidenceError,
            "EXTERNAL_V43_AUTHORITY_VERIFIER_REQUIRED",
        ):
            l2_evidence.load_verified_execution_request(bundle / "capture")

    def test_unverifiable_continuous_delta_stays_descriptive_and_outside_replay(
        self,
    ) -> None:
        writer = importlib.import_module("bybit_l2_writer_v43")
        spec = writer.FixtureCaptureSpec(
            capture_id="bybit-continuous-descriptive",
            contract_id="ABCUSDT",
            event_lineage_hash=EVENT_HASH,
            t0_ts=T0,
            window_start_ts=T0 - 60.0,
            window_end_ts=T0 + 61.0,
            max_runtime_sec=30.0,
            max_messages=10,
        )
        source = writer.StaticBybitL2FixtureSource(
            [
                writer.FixtureWsRecord.open(1),
                writer.FixtureWsRecord.message(
                    1,
                    snapshot_bytes(sequence=100),
                    received_ts=T0 + 0.010,
                    monotonic_ns=1_000_000_000,
                ),
                writer.FixtureWsRecord.message(
                    1,
                    delta_bytes(sequence=101),
                    received_ts=T0 + 0.020,
                    monotonic_ns=1_100_000_000,
                ),
                writer.FixtureWsRecord.close(1, reason="fixture_eof"),
            ]
        )
        output = self.root / "continuous-bybit"

        manifest = writer.run_fixture_bybit_l2_capture(
            spec,
            output_dir=output,
            source=source,
            monotonic=lambda: 0.0,
            should_stop=lambda: False,
        )

        self.assertEqual(manifest["status"], "STOPPED_INCOMPLETE")
        self.assertEqual(manifest["evidence_class"], "DESCRIPTIVE_ONLY")
        self.assertFalse(manifest["replay_ready"])
        self.assertFalse(manifest["execution_bundle_ready"])
        self.assertEqual(manifest["gaps"][0]["gap_signal"], "CONTINUITY_UNVERIFIABLE")
        with self.assertRaises(l2_evidence.L2EvidenceError):
            l2_evidence.inspect_candidate_execution_request(output)

    def test_package_is_unbound_and_active_v42_identity_is_unchanged(self) -> None:
        plan = risk_gate.load_and_verify_plan()
        self.assertEqual(plan["plan_id"], "premarket_perp_capture_20260822_v42")
        self.assertEqual(
            plan["plan_hash"],
            "72acbc1426ddfc5ccb168dd1d75d6414e5af0d30507b80f32fa8d85020691926",
        )
        bound = {path for _role, path in config.BOUND_RUNTIME_FILES}
        self.assertTrue(
            {
                "src/bybit_l2_writer_v43.py",
                "src/v43_fixture_authority.py",
                "src/v43_verified_replay.py",
            }.isdisjoint(bound)
        )


if __name__ == "__main__":
    unittest.main()
