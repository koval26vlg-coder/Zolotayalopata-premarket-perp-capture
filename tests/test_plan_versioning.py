from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import plan_builder
import project_config as config


V1_PLAN_SHA256 = "cac4d34cbc6228fd0a7fc7922afb8ce3b1110388a1df860dba5bbd9f40ae2934"
V1_PLAN_HASH = "aa174438bf457e3a57d94e8f3839ae9a61dbb42504d03f5876825f59a9b2d6c1"
V2_PLAN_SHA256 = "1d990fbfd84cf5d9d06fd927074b50d200e1d49c0f9bc4200020ec43cb4aac57"
V2_PLAN_HASH = "b7c0543a81b9afa6781f1ca89871d0632405551b3ff51e18e10348da405910d7"
V3_PLAN_SHA256 = "d6b67c4f52f05bd6902855bc58b416eaab2ef9e3bd430e948ff77d9c6bdb9f94"
V3_PLAN_HASH = "ee5f555f88691e18207ec22231217a73ec2a82f25069402b14e8d85646350627"


class ImmutablePlanVersioningTests(unittest.TestCase):
    def test_original_v1_plan_is_restored_byte_for_byte(self) -> None:
        path = config.PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822.json"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), V1_PLAN_SHA256)

    def test_v2_plan_is_preserved_byte_for_byte(self) -> None:
        path = config.PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v2.json"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), V2_PLAN_SHA256)

    def test_active_plan_is_a_new_v3_identity_that_supersedes_v2(self) -> None:
        self.assertEqual(config.PLAN_PATH.name, "premarket-perp-capture-planonly-20260822-v3.json")
        plan = plan_builder.build_plan("2026-08-22T20:00:00.000Z")
        self.assertEqual(plan["schema"], "premarket_perp_capture_planonly_v3")
        self.assertEqual(plan["plan_id"], "premarket_perp_capture_20260822_v3")
        self.assertEqual(plan["supersedes_plan_hash"], V2_PLAN_HASH)
        self.assertEqual(
            plan["supersedes_plan_path"],
            "docs/plans/premarket-perp-capture-planonly-20260822-v2.json",
        )

    def test_checked_in_v3_artifact_has_the_frozen_identity(self) -> None:
        self.assertEqual(
            hashlib.sha256(config.PLAN_PATH.read_bytes()).hexdigest(),
            V3_PLAN_SHA256,
        )
        plan = json.loads(config.PLAN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(plan["plan_hash"], V3_PLAN_HASH)

    def test_builder_never_replaces_an_existing_plan_identity(self) -> None:
        path = Path(tempfile.mkdtemp()) / "existing-v3.json"
        original = b"already immutable\n"
        path.write_bytes(original)

        with mock.patch.object(config, "PLAN_PATH", path):
            with self.assertRaisesRegex(
                plan_builder.PlanBuildError, "new versioned PlanOnly path"
            ):
                plan_builder.write_plan("2026-08-22T20:00:00.000Z")

        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
