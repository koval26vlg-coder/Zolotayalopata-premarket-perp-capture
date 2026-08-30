"""Contract for the active no-capture historical-acquisition PlanOnly v40.

The active v38 reserves the next event-bound proposal number for v39.  The historical
The first v39 issue failed its capability scan before activation and remains immutable.
v40 repairs that binding without erasing history and moves the future event-bound plan
to v41. These tests do not perform network I/O or write research artifacts.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from canonical_hash import canonical_hash  # noqa: E402
import event_bound_plan_proposal as proposal  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import project_config as config  # noqa: E402
import risk_gate  # noqa: E402


V38_PATH = ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v38.json"
V38_RELATIVE_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v38.json"
V38_ID = "premarket_perp_capture_20260822_v38"
V38_SCHEMA = "premarket_perp_capture_planonly_v38"
V38_PLAN_HASH = "f2132603e3a3e8403a20cbceeb273cbb9f8c6804b7e5d81aaacac3b97626095b"
V38_FILE_SHA256 = "f7cb1f354c47ecb3b948cbd32c58ec97ba61dcebb22bab73ced8a08d26a0aabd"

V39_RELATIVE_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v39.json"
V39_PATH = ROOT / V39_RELATIVE_PATH
V39_ID = "premarket_perp_capture_20260822_v39"
V39_SCHEMA = "premarket_perp_capture_planonly_v39"
V39_PLAN_HASH = "d3e410f550ccf84c985924120c9970be28c87e3c788dba00ba55cde112406512"
V39_FILE_SHA256 = "4e010bc581bd5a2e6fd53e6a44bccc21eb3114d491eb9dc5f18a236d9a52696f"

V40_RELATIVE_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v40.json"
V40_PATH = ROOT / V40_RELATIVE_PATH
V40_ID = "premarket_perp_capture_20260822_v40"
V40_SCHEMA = "premarket_perp_capture_planonly_v40"
V40_STATUS = "HISTORICAL_ACQUISITION_REPLAY_READY_NO_CAPTURE"

V41_RELATIVE_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v41.json"
V41_ID = "premarket_perp_capture_20260822_v41"
V41_SCHEMA = "premarket_perp_capture_planonly_v41"

HISTORICAL_WRITE_CLASS = "historical_market_data_acquisition"
HISTORICAL_ACQUISITION_ACTION = (
    "acquire bounded historical public pre-market evidence into a separate "
    "append-only namespace"
)
HISTORICAL_REPLAY_ACTION = (
    "run deterministic offline causal replay over sealed historical evidence"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PlanV39HistoricalReplayContractTests(unittest.TestCase):
    def _load_v40(self) -> dict[str, object]:
        self.assertTrue(
            V40_PATH.is_file(),
            "RED: immutable historical bridge PlanOnly v40 has not been issued",
        )
        payload = json.loads(V40_PATH.read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)
        return payload

    def test_v38_and_failed_closed_v39_are_byte_exact_retired_predecessors(self) -> None:
        self.assertEqual(_sha256(V38_PATH), V38_FILE_SHA256)
        v38 = json.loads(V38_PATH.read_text(encoding="utf-8"))
        self.assertEqual(v38["schema"], V38_SCHEMA)
        self.assertEqual(v38["plan_id"], V38_ID)
        self.assertEqual(v38["plan_hash"], V38_PLAN_HASH)
        self.assertEqual(
            v38["plan_hash"],
            canonical_hash({key: value for key, value in v38.items() if key != "plan_hash"}),
        )

        retired = list(trust_root.RETIRED_PLANS)
        self.assertTrue(retired, "v39 must retain the complete immutable lineage")
        self.assertEqual(
            retired[-2],
            {
                "schema": V38_SCHEMA,
                "plan_id": V38_ID,
                "plan_hash": V38_PLAN_HASH,
                "plan_file_sha256": V38_FILE_SHA256,
                "path": V38_RELATIVE_PATH,
            },
        )
        self.assertEqual(_sha256(V39_PATH), V39_FILE_SHA256)
        v39 = json.loads(V39_PATH.read_text(encoding="utf-8"))
        self.assertEqual(v39["plan_hash"], V39_PLAN_HASH)
        self.assertEqual(
            retired[-1],
            {
                "schema": V39_SCHEMA,
                "plan_id": V39_ID,
                "plan_hash": V39_PLAN_HASH,
                "plan_file_sha256": V39_FILE_SHA256,
                "path": V39_RELATIVE_PATH,
            },
        )

    def test_v40_is_the_active_exact_no_capture_bridge(self) -> None:
        plan = self._load_v40()
        self.assertEqual(plan["schema"], V40_SCHEMA)
        self.assertEqual(plan["plan_id"], V40_ID)
        self.assertEqual(plan["status"], V40_STATUS)
        self.assertEqual(plan["supersedes_plan_id"], V39_ID)
        self.assertEqual(plan["supersedes_plan_hash"], V39_PLAN_HASH)
        self.assertEqual(
            plan["supersedes_plan_path"],
            V39_PATH.relative_to(ROOT).as_posix(),
        )
        self.assertEqual(
            plan["plan_hash"],
            canonical_hash({key: value for key, value in plan.items() if key != "plan_hash"}),
        )
        self.assertEqual(trust_root.PLAN_SCHEMA, V40_SCHEMA)
        self.assertEqual(trust_root.PLAN_ID, V40_ID)
        self.assertEqual(trust_root.PLAN_HASH, plan["plan_hash"])
        self.assertEqual(trust_root.PLAN_FILE_SHA256, _sha256(V40_PATH))

    def test_event_bound_proposal_moves_to_v41_without_capture_authority(self) -> None:
        plan = self._load_v40()
        contract = plan["event_bound_plan_proposal"]
        self.assertEqual(proposal.PROPOSED_PLAN_SCHEMA, V41_SCHEMA)
        self.assertEqual(proposal.PROPOSED_PLAN_ID, V41_ID)
        self.assertEqual(proposal.PROPOSED_PLAN_PATH, V41_RELATIVE_PATH)
        self.assertEqual(contract["proposed_plan_schema"], V41_SCHEMA)
        self.assertEqual(contract["proposed_plan_id"], V41_ID)
        self.assertEqual(contract["proposed_plan_path"], V41_RELATIVE_PATH)
        self.assertIs(contract["capture_authorized"], False)
        self.assertIs(contract["capture_token_issued"], False)
        self.assertIs(contract["trust_root_rebound"], False)
        self.assertIs(contract["requires_explicit_user_capture_approval"], True)

    def test_historical_acquisition_is_bounded_shared_gate_global_claim_no_token(self) -> None:
        plan = self._load_v40()
        policy = plan["write_classes"][HISTORICAL_WRITE_CLASS]
        self.assertEqual(policy, config.WRITE_CLASSES[HISTORICAL_WRITE_CLASS])
        self.assertIs(policy["shared_gate_required"], True)
        self.assertIs(policy["exclusive_writer_claim"], True)
        self.assertIs(policy["capture_token"], False)
        self.assertIs(policy["plan_and_capability_scan"], True)
        self.assertIs(policy["endpoint_allow_list"], True)
        self.assertEqual(policy["writer_claim"], "GLOBAL_ACTIVE_MARKET_DATA_WRITER_CLAIM")
        self.assertEqual(
            plan["resolved_path_bindings"]["shared_writer_claim_path"],
            str(config.SHARED_WRITER_CLAIM_PATH.resolve(strict=False)),
        )

        acquisition = plan["historical_acquisition"]
        self.assertEqual(acquisition["venues"], ["bybit", "okx", "gate"])
        bounds = acquisition["bounds"]
        for field in ("max_events_per_run", "max_requests_per_run", "max_runtime_sec"):
            self.assertIsInstance(bounds[field], int)
            self.assertNotIsInstance(bounds[field], bool)
            self.assertGreater(bounds[field], 0)
        self.assertLessEqual(bounds["max_events_per_run"], 100)
        self.assertLessEqual(bounds["max_requests_per_run"], 20_000)
        self.assertLessEqual(bounds["max_runtime_sec"], 3_600)
        self.assertEqual(bounds["max_retries_per_request"], 0)

        roots = acquisition["output_roots"]
        self.assertEqual(set(roots), {"raw", "manifests", "receipts"})
        resolved = [Path(roots[name]).resolve(strict=False) for name in sorted(roots)]
        self.assertEqual(len(resolved), len(set(resolved)))
        capture_root = config.CAPTURE_ROOT.resolve(strict=False)
        production_registry = (
            config.PROJECT_ROOT / "docs/registry/listing-events-v3.jsonl"
        ).resolve(strict=False)
        for root in resolved:
            self.assertFalse(root.is_relative_to(capture_root))
            self.assertNotEqual(root, production_registry)
            self.assertFalse(root.is_relative_to(config.OFFICIAL_T0_ARMING_ROOT))
            self.assertFalse(root.is_relative_to(config.EVENT_BOUND_PLAN_PROPOSAL_ROOT))

    def test_new_runtime_files_are_sha_bound_by_v39(self) -> None:
        plan = self._load_v40()
        expected = {
            "historical_acquisition": "src/historical_acquisition.py",
            "historical_causal_replay": "src/historical_causal_replay.py",
        }
        declared = {
            item["role"]: item
            for item in plan["implementation"]["files"]
        }
        configured = dict(config.BOUND_RUNTIME_FILES)
        for role, relative in expected.items():
            self.assertEqual(configured[role], relative)
            self.assertEqual(declared[role]["repo_path"], relative)
            runtime = ROOT / relative
            self.assertTrue(runtime.is_file(), f"missing v39 runtime: {relative}")
            self.assertEqual(declared[role]["sha256"], _sha256(runtime))

    def test_proxy_history_stays_descriptive_and_capture_remains_unauthorized(self) -> None:
        plan = self._load_v40()
        proxy = plan["event_registry"]["proxy_policy"]
        self.assertEqual(proxy["evidence_use"], "DESCRIPTIVE_ONLY")
        self.assertIs(proxy["capture_eligible"], False)
        self.assertEqual(proxy["acceptance_support"], "FORBIDDEN")

        replay = plan["historical_causal_replay"]
        self.assertEqual(replay["proxy_event_policy"], "DESCRIPTIVE_ONLY")
        self.assertEqual(
            replay["historical_received_at_policy"],
            "RETRIEVAL_TIME_NOT_CONTEMPORANEOUS_MARKET_RECEIVED_TS",
        )
        self.assertEqual(replay["missing_execution_inputs"], "NO_NET_PNL")
        self.assertIs(replay["acceptance_capable"], False)
        self.assertEqual(replay["fixed_model"], dict(config.OFFLINE_PAPER_MODEL))

        self.assertIs(plan["activation_gate"]["capture_authorized"], False)
        self.assertNotIn(risk_gate.CAPTURE_ACTION, plan["authorized_after_gate_green"])
        self.assertIn(HISTORICAL_ACQUISITION_ACTION, plan["authorized_after_gate_green"])
        self.assertIn(HISTORICAL_REPLAY_ACTION, plan["authorized_after_gate_green"])
        status_authority = risk_gate.PLAN_WRITE_AUTHORIZATION[V40_STATUS]
        self.assertNotIn("market_data_capture", status_authority["write_classes"])
        self.assertNotIn(risk_gate.CAPTURE_ACTION, status_authority["authorized_actions"])
        self.assertIn(HISTORICAL_WRITE_CLASS, status_authority["write_classes"])
        self.assertEqual(
            risk_gate.WRITE_CLASS_ACTION[HISTORICAL_WRITE_CLASS],
            HISTORICAL_ACQUISITION_ACTION,
        )
        self.assertIs(plan["risk_contract"]["orders"], False)
        self.assertIs(plan["risk_contract"]["live_execution"], False)
        self.assertIs(plan["risk_contract"]["paper_execution"], False)


if __name__ == "__main__":
    unittest.main()
