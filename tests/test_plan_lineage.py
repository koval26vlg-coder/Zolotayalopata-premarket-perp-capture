"""A plan is immutable, so a reissue is a new file and the old one stays on disk.

This was written in AGENTS.md and enforced nowhere, and the plan at this project's
single plan path was regenerated in place three times under one plan_id, leaving no
record of what replaced what. The version is part of the filename now, and the lineage
is verified rather than described.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import plan_builder  # noqa: E402
import project_config as config  # noqa: E402
import risk_gate  # noqa: E402


class PlanVersioningTests(unittest.TestCase):
    def test_the_plan_path_and_the_plan_id_both_carry_the_version(self):
        self.assertIn(f"-v{config.PLAN_VERSION}.json", config.PLAN_PATH.name)
        self.assertTrue(plan_builder.PLAN_ID.endswith(f"_v{config.PLAN_VERSION}"))

    def test_every_superseded_plan_is_still_on_disk(self):
        for path in config.SUPERSEDED_PLAN_PATHS:
            with self.subTest(plan=path.name):
                self.assertTrue(path.is_file(), f"lineage lost a version: {path}")

    def test_the_current_plan_names_every_plan_it_replaced(self):
        plan = json.loads(config.PLAN_PATH.read_text(encoding="utf-8"))
        recorded = [entry["plan_file"] for entry in plan["supersedes"]]
        self.assertEqual(recorded, [p.name for p in config.SUPERSEDED_PLAN_PATHS])

    def test_the_recorded_hashes_match_the_files_they_name(self):
        plan = json.loads(config.PLAN_PATH.read_text(encoding="utf-8"))
        for entry, path in zip(plan["supersedes"], config.SUPERSEDED_PLAN_PATHS):
            with self.subTest(plan=path.name):
                prior = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(entry["plan_hash"], prior["plan_hash"])
                self.assertEqual(entry["plan_file_sha256"], risk_gate.sha256_file(path))

    def test_the_checked_in_lineage_verifies(self):
        risk_gate.load_and_verify_plan()

    def test_writing_over_a_superseded_plan_is_refused(self):
        # Deleting the current plan and regenerating it is how the versions were lost.
        original = config.PLAN_PATH
        try:
            config.PLAN_PATH = config.SUPERSEDED_PLAN_PATHS[0]
            with self.assertRaises(plan_builder.PlanBuildError):
                plan_builder.write_plan("2026-08-22T00:00:00.000Z")
        finally:
            config.PLAN_PATH = original

    def test_a_missing_superseded_plan_stops_a_reissue(self):
        original = config.SUPERSEDED_PLAN_PATHS
        try:
            config.SUPERSEDED_PLAN_PATHS = original + (
                config.PLAN_DIR / "premarket-perp-capture-planonly-never-existed.json",
            )
            with self.assertRaises(plan_builder.PlanBuildError):
                plan_builder.superseded_plans()
        finally:
            config.SUPERSEDED_PLAN_PATHS = original


class LineageVerificationTests(unittest.TestCase):
    """The gate must notice a lineage that has been trimmed or edited."""

    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp())
        self.backup = {}
        for path in config.SUPERSEDED_PLAN_PATHS:
            copy = self.scratch / path.name
            shutil.copy2(path, copy)
            self.backup[path] = copy

    def tearDown(self) -> None:
        for path, copy in self.backup.items():
            shutil.copy2(copy, path)

    def test_editing_a_superseded_plan_breaks_verification(self):
        victim = config.SUPERSEDED_PLAN_PATHS[0]
        payload = json.loads(victim.read_text(encoding="utf-8"))
        payload["status"] = "QUIETLY_CHANGED"
        victim.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(risk_gate.RiskGateError, "edited since it was superseded"):
            risk_gate.load_and_verify_plan()

    def test_deleting_a_superseded_plan_breaks_verification(self):
        victim = config.SUPERSEDED_PLAN_PATHS[-1]
        victim.unlink()
        with self.assertRaisesRegex(risk_gate.RiskGateError, "missing from the lineage"):
            risk_gate.load_and_verify_plan()


if __name__ == "__main__":
    unittest.main()
