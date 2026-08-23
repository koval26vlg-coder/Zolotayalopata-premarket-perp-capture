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
# v2 as it was actually published at 9b71be2, not regenerated into a tidier shape:
# it shares v1's plan_id because that WAS the defect, and rewriting history to hide
# it would be the same mistake in a different direction.
V2_PLAN_SHA256 = "22a31cd3e283e492f062e66d0f6353e9c08d336fa1ceddb2a33d0888440e8836"
V2_PLAN_HASH = "6b4093be300c456794413486879a9302af12e86c3bf0994bfa075f7c7270592a"
V3_PLAN_SHA256 = "60e2c64048091ea191ba40a60e69ba4916af2a13f22dcb0c089fca614d114192"
V3_PLAN_HASH = "ef17f97b00faf1de53eecb16b3bd4355bfabd70fd887e1df0efd787149cdef92"
V4_PLAN_SHA256 = "48e8e33171425cff1642b1b9088dd24593edd5649211e3552d958933a42a4f27"
V4_PLAN_HASH = "fae208baf126163e2041fccffe4c1b656848a80647b1a15b0bc0af5901dd3314"


class ImmutablePlanVersioningTests(unittest.TestCase):
    def test_original_v1_plan_is_restored_byte_for_byte(self) -> None:
        path = config.PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822.json"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), V1_PLAN_SHA256)

    def test_v2_plan_is_preserved_byte_for_byte(self) -> None:
        path = config.PROJECT_ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v2.json"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), V2_PLAN_SHA256)

    def test_v3_plan_is_preserved_byte_for_byte(self) -> None:
        path = config.V3_PLAN_PATH
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), V3_PLAN_SHA256)

    def test_active_plan_is_a_new_v4_identity_that_supersedes_v3(self) -> None:
        self.assertEqual(config.PLAN_PATH.name, "premarket-perp-capture-planonly-20260822-v4.json")
        plan = plan_builder.build_plan("2026-08-22T20:00:00.000Z")
        self.assertEqual(plan["schema"], "premarket_perp_capture_planonly_v4")
        self.assertEqual(plan["plan_id"], "premarket_perp_capture_20260822_v4")
        self.assertEqual(plan["supersedes_plan_hash"], V3_PLAN_HASH)
        self.assertEqual(
            plan["supersedes_plan_path"],
            "docs/plans/premarket-perp-capture-planonly-20260822-v3.json",
        )

    def test_checked_in_v4_artifact_has_the_frozen_identity(self) -> None:
        self.assertEqual(
            hashlib.sha256(config.PLAN_PATH.read_bytes()).hexdigest(),
            V4_PLAN_SHA256,
        )
        plan = json.loads(config.PLAN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(plan["plan_hash"], V4_PLAN_HASH)

    def test_every_published_plan_is_still_on_disk(self) -> None:
        """A lineage that can silently lose a version is not a lineage."""
        import frozen_plan_bindings as trust_root
        for retired in trust_root.RETIRED_PLANS:
            with self.subTest(plan=retired["plan_id"]):
                path = config.PROJECT_ROOT / retired["path"]
                self.assertTrue(path.is_file())
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    retired["plan_file_sha256"],
                )

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
