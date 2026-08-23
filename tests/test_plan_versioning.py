"""A plan is immutable, so a reissue is a new file and every old one stays on disk.

This was prose in AGENTS.md and the plan at one path was regenerated in place three
times under a single plan_id. The assertions here are deliberately about the invariant
rather than about one release's hashes: a test that has to be hand-edited on every
reissue is a test that will eventually be edited to agree with whatever happened.

The one hardcoded anchor is v1, restored byte for byte from cf4b702. It is the fixed
point the rest of the lineage is checked against.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import frozen_plan_bindings as trust_root
import plan_builder
import project_config as config


V1_PLAN_SHA256 = "cac4d34cbc6228fd0a7fc7922afb8ce3b1110388a1df860dba5bbd9f40ae2934"
V1_PLAN_HASH = "aa174438bf457e3a57d94e8f3839ae9a61dbb42504d03f5876825f59a9b2d6c1"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ImmutablePlanVersioningTests(unittest.TestCase):
    def test_original_v1_plan_is_restored_byte_for_byte(self) -> None:
        path = config.PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822.json"
        self.assertEqual(sha256_of(path), V1_PLAN_SHA256)
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["plan_hash"], V1_PLAN_HASH
        )

    def test_the_lineage_starts_at_v1(self) -> None:
        self.assertEqual(trust_root.RETIRED_PLANS[0]["plan_hash"], V1_PLAN_HASH)

    def test_every_retired_plan_is_still_on_disk_unchanged(self) -> None:
        """A lineage that can silently lose a version is not a lineage."""
        for retired in trust_root.RETIRED_PLANS:
            with self.subTest(plan=retired["path"]):
                path = config.PROJECT_ROOT / retired["path"]
                self.assertTrue(path.is_file(), f"lineage lost {retired['path']}")
                self.assertEqual(sha256_of(path), retired["plan_file_sha256"])
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(payload["plan_hash"], retired["plan_hash"])
                self.assertEqual(payload["plan_id"], retired["plan_id"])

    def test_the_active_plan_is_not_one_of_the_retired_ones(self) -> None:
        active = config.PLAN_PATH.name
        self.assertNotIn(active, {Path(r["path"]).name for r in trust_root.RETIRED_PLANS})

    def test_the_active_plan_supersedes_the_most_recent_retired_one(self) -> None:
        previous = trust_root.RETIRED_PLANS[-1]
        plan = plan_builder.build_plan("2026-08-23T00:00:00.000Z")
        self.assertEqual(plan["plan_id"], trust_root.ACTIVE_PLAN["plan_id"])
        self.assertEqual(plan["schema"], trust_root.ACTIVE_PLAN["schema"])
        self.assertEqual(plan["supersedes_plan_id"], previous["plan_id"])
        self.assertEqual(plan["supersedes_plan_hash"], previous["plan_hash"])
        self.assertEqual(plan["supersedes_plan_path"], previous["path"])

    def test_the_checked_in_artifact_matches_the_frozen_identity(self) -> None:
        self.assertEqual(sha256_of(config.PLAN_PATH), trust_root.PLAN_FILE_SHA256)
        payload = json.loads(config.PLAN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload["plan_hash"], trust_root.PLAN_HASH)
        self.assertEqual(payload["plan_id"], trust_root.PLAN_ID)

    def test_the_version_is_visible_in_both_the_filename_and_the_identity(self) -> None:
        suffix = trust_root.PLAN_ID.rsplit("_", 1)[-1]
        self.assertRegex(suffix, r"^v\d+$")
        self.assertIn(f"-{suffix}.json", config.PLAN_PATH.name)

    def test_no_two_plans_in_the_lineage_share_a_hash(self) -> None:
        hashes = [r["plan_hash"] for r in trust_root.RETIRED_PLANS]
        hashes.append(trust_root.PLAN_HASH)
        self.assertEqual(len(hashes), len(set(hashes)))

    def test_builder_never_replaces_an_existing_plan_identity(self) -> None:
        path = Path(tempfile.mkdtemp()) / "existing-v9.json"
        original = b"already immutable\n"
        path.write_bytes(original)

        with mock.patch.object(config, "PLAN_PATH", path):
            with self.assertRaisesRegex(
                plan_builder.PlanBuildError, "new versioned PlanOnly path"
            ):
                plan_builder.write_plan("2026-08-23T00:00:00.000Z")

        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
