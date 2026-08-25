"""Regression contract for the v27 documentation/identity accuracy reissue."""

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


V26_RELATIVE_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v26.json"
V26_PLAN_HASH = "ed1b5e1d4c5afbc03269905f75a01def09d31afbb5bd87dc387681747afab541"
V26_FILE_SHA256 = "d8a6f89a0af2e0c1c87dc7ae9efcc9b4159eba1738d4e8396d05103f7104dd64"


class V27AccuracyTests(unittest.TestCase):
    def test_v26_is_retired_byte_identical_and_v27_is_active(self) -> None:
        v26_path = ROOT / V26_RELATIVE_PATH
        self.assertEqual(hashlib.sha256(v26_path.read_bytes()).hexdigest(), V26_FILE_SHA256)
        v26 = json.loads(v26_path.read_text(encoding="utf-8"))
        self.assertEqual(v26["plan_hash"], V26_PLAN_HASH)

        retired = {
            str(item["path"]).replace("\\", "/"): item
            for item in trust_root.RETIRED_PLANS
        }
        self.assertIn(V26_RELATIVE_PATH, retired)
        self.assertEqual(retired[V26_RELATIVE_PATH]["plan_hash"], V26_PLAN_HASH)
        self.assertEqual(retired[V26_RELATIVE_PATH]["plan_file_sha256"], V26_FILE_SHA256)
        self.assertEqual(trust_root.PLAN_ID, "premarket_perp_capture_20260822_v27")
        self.assertEqual(config.PLAN_PATH.name, "premarket-perp-capture-planonly-20260822-v27.json")

    def test_v27_names_its_own_identity_without_granting_capture(self) -> None:
        plan = plan_builder.build_plan("2026-08-25T18:00:00.000Z")
        self.assertEqual(plan["schema"], "premarket_perp_capture_planonly_v27")
        self.assertEqual(plan["plan_id"], "premarket_perp_capture_20260822_v27")
        self.assertEqual(plan["supersedes_plan_id"], "premarket_perp_capture_20260822_v26")
        self.assertEqual(plan["supersedes_plan_hash"], V26_PLAN_HASH)
        self.assertFalse(plan["activation_gate"]["capture_authorized"])
        reason = plan["activation_gate"]["reason"]
        self.assertNotIn("v17", reason)
        self.assertIn("v27", reason)

    def test_operator_docs_identify_v27_as_active(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("premarket_perp_capture_20260822_v27", readme)
        self.assertIn("активный immutable план — v27", agents)
        self.assertNotIn("активным PlanOnly v17", readme)
        self.assertNotIn("## Состояние v17", readme)


if __name__ == "__main__":
    unittest.main()
