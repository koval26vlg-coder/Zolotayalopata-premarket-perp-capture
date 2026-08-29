"""RED contract for the capture-disabled v30 paper-simulation preregistration.

This file intentionally lands before the production implementation and immutable
PlanOnly.  v30 may preregister an offline virtual-position model, but it must not turn
that model into exchange paper execution or grant live market-data capture authority.
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

import frozen_plan_bindings as trust_root  # noqa: E402
import plan_builder  # noqa: E402
import project_config as config  # noqa: E402
import risk_gate  # noqa: E402


V29_RELATIVE_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v29.json"
V29_PLAN_ID = "premarket_perp_capture_20260822_v29"
V29_PLAN_HASH = "63f4173a4d3662e6eed15f9ba1f372c8771f635b84291ed2439e076d6975a8d5"
V29_FILE_SHA256 = "7c93aebec952ec1d52def42ce5ac4165b6b3c8c608436ed702f50dbfb012b822"

V30_RELATIVE_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v30.json"
V30_PLAN_ID = "premarket_perp_capture_20260822_v30"
V30_SCHEMA = "premarket_perp_capture_planonly_v30"
V30_STATUS = "PAPER_SIMULATION_PREREGISTERED_NO_CAPTURE"
V30_PLAN_HASH = "32877c7c731bdf63167b20827f373726e34e1fbc1bcd61db26d6975444067ab5"
V30_FILE_SHA256 = "d68bf90c354063622a33e762a58ec610594af1bcc7359cf8125a28e9f933a192"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class V29PredecessorGuardTests(unittest.TestCase):
    def test_v29_bytes_remain_the_exact_v30_predecessor(self) -> None:
        path = ROOT / V29_RELATIVE_PATH
        self.assertEqual(sha256_file(path), V29_FILE_SHA256)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["plan_id"], V29_PLAN_ID)
        self.assertEqual(payload["plan_hash"], V29_PLAN_HASH)


class V30ImmutableHistoryTests(unittest.TestCase):
    def test_project_config_preserves_v30_and_v29_paths(self) -> None:
        self.assertEqual(config.V29_PLAN_PATH, ROOT / V29_RELATIVE_PATH)
        self.assertEqual(config.V30_PLAN_PATH, ROOT / V30_RELATIVE_PATH)
        self.assertNotEqual(config.V30_PLAN_PATH, config.V29_PLAN_PATH)

    def test_v30_artifact_preserves_exact_v29_lineage(self) -> None:
        payload = json.loads((ROOT / V30_RELATIVE_PATH).read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], V30_SCHEMA)
        self.assertEqual(payload["plan_id"], V30_PLAN_ID)
        self.assertEqual(payload["supersedes_plan_id"], V29_PLAN_ID)
        self.assertEqual(payload["supersedes_plan_hash"], V29_PLAN_HASH)
        self.assertEqual(payload["supersedes_plan_path"], V29_RELATIVE_PATH)
        self.assertEqual(payload["status"], V30_STATUS)

    def test_external_trust_root_retires_exact_v29_and_v30(self) -> None:
        retired = {
            str(item["path"]).replace("\\", "/"): item
            for item in trust_root.RETIRED_PLANS
        }
        self.assertIn(V29_RELATIVE_PATH, retired)
        self.assertEqual(retired[V29_RELATIVE_PATH]["plan_id"], V29_PLAN_ID)
        self.assertEqual(retired[V29_RELATIVE_PATH]["plan_hash"], V29_PLAN_HASH)
        self.assertEqual(
            retired[V29_RELATIVE_PATH]["plan_file_sha256"], V29_FILE_SHA256
        )
        self.assertIn(V30_RELATIVE_PATH, retired)
        self.assertEqual(retired[V30_RELATIVE_PATH]["plan_id"], V30_PLAN_ID)
        self.assertEqual(retired[V30_RELATIVE_PATH]["plan_hash"], V30_PLAN_HASH)
        self.assertEqual(
            retired[V30_RELATIVE_PATH]["plan_file_sha256"], V30_FILE_SHA256
        )

    def test_checked_in_v30_artifact_is_byte_identical(self) -> None:
        self.assertTrue(config.V30_PLAN_PATH.is_file(), "v30 immutable artifact is missing")
        payload = json.loads(config.V30_PLAN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], V30_SCHEMA)
        self.assertEqual(payload["plan_id"], V30_PLAN_ID)
        self.assertEqual(payload["plan_hash"], V30_PLAN_HASH)
        self.assertEqual(sha256_file(config.V30_PLAN_PATH), V30_FILE_SHA256)


class V30PaperRiskContractRedTests(unittest.TestCase):
    def test_offline_paper_simulation_does_not_loosen_execution_risk(self) -> None:
        contract = config.RISK_CONTRACT
        self.assertIs(contract["offline_paper_simulation"], True)
        for key in (
            "paper_execution",
            "orders",
            "private_api",
            "live_execution",
            "uses_leverage",
            "uses_margin",
            "real_capital",
        ):
            with self.subTest(capability=key):
                self.assertIs(contract[key], False)

    def test_paper_replay_is_sha_bound_runtime_not_an_unbound_helper(self) -> None:
        bound = dict(config.BOUND_RUNTIME_FILES)
        self.assertEqual(bound.get("paper_replay"), "src/paper_replay.py")
        self.assertTrue((ROOT / "src/paper_replay.py").is_file())

    def test_v30_status_cannot_authorize_market_data_capture(self) -> None:
        status = risk_gate.PAPER_SIMULATION_PREREGISTERED_PLAN_STATUS
        self.assertEqual(status, V30_STATUS)
        authorization = risk_gate.PLAN_WRITE_AUTHORIZATION[status]
        self.assertNotIn("market_data_capture", authorization["write_classes"])
        self.assertNotIn(risk_gate.CAPTURE_ACTION, authorization["authorized_actions"])


if __name__ == "__main__":
    unittest.main()
