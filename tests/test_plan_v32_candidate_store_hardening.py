"""Immutable v32 remediation for the announcement-candidate authority boundary."""

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

import announcement_candidate_store as candidate_store  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import plan_builder  # noqa: E402
import project_config as config  # noqa: E402


V31_RELATIVE_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v31.json"
V31_PATH = ROOT / V31_RELATIVE_PATH
V31_PLAN_ID = "premarket_perp_capture_20260822_v31"
V31_PLAN_HASH = "0359596666d918145af2fe3e172cd9907b9f286b0d25a986671be8113415bb98"
V31_FILE_SHA256 = "a92c2f8a105a6b63c8747d5a45e75ec22af445da286292a4666bc02d02db26b4"
V32_PATH = ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v32.json"
V32_PLAN_ID = "premarket_perp_capture_20260822_v32"
V32_SCHEMA = "premarket_perp_capture_planonly_v32"


class V32ImmutableLineageTests(unittest.TestCase):
    def test_v31_is_byte_identical_and_v32_supersedes_it(self) -> None:
        self.assertEqual(hashlib.sha256(V31_PATH.read_bytes()).hexdigest(), V31_FILE_SHA256)
        self.assertEqual(config.V31_PLAN_PATH, V31_PATH)
        self.assertEqual(config.V32_PLAN_PATH, V32_PATH)

        payload = json.loads(V32_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], V32_SCHEMA)
        self.assertEqual(payload["plan_id"], V32_PLAN_ID)
        self.assertEqual(payload["supersedes_plan_id"], V31_PLAN_ID)
        self.assertEqual(payload["supersedes_plan_hash"], V31_PLAN_HASH)
        self.assertEqual(payload["supersedes_plan_path"], V31_RELATIVE_PATH)

    def test_v32_is_retired_and_v31_remains_retired(self) -> None:
        payload = json.loads(V32_PATH.read_text(encoding="utf-8"))
        retired = {item["path"]: item for item in trust_root.RETIRED_PLANS}
        self.assertEqual(retired[V31_RELATIVE_PATH]["plan_hash"], V31_PLAN_HASH)
        self.assertEqual(
            retired[V31_RELATIVE_PATH]["plan_file_sha256"],
            V31_FILE_SHA256,
        )
        v32_relative = V32_PATH.relative_to(ROOT).as_posix()
        self.assertEqual(retired[v32_relative]["schema"], V32_SCHEMA)
        self.assertEqual(retired[v32_relative]["plan_id"], V32_PLAN_ID)
        self.assertEqual(retired[v32_relative]["plan_hash"], payload["plan_hash"])
        self.assertEqual(
            retired[v32_relative]["plan_file_sha256"],
            hashlib.sha256(V32_PATH.read_bytes()).hexdigest(),
        )


class V32CandidateStoreContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = json.loads(V32_PATH.read_text(encoding="utf-8"))
        self.store = self.plan["announcement_discovery"]["candidate_store"]

    def test_plan_exact_field_schema_matches_the_runtime(self) -> None:
        self.assertEqual(
            self.store["exact_fields"],
            sorted(candidate_store._CANDIDATE_FIELDS),
        )
        self.assertEqual(
            self.store["unknown_fields"],
            "REJECT_BEFORE_LOCK_OR_WRITE",
        )

    def test_plan_fixes_every_non_authority_boundary_value(self) -> None:
        self.assertEqual(
            self.store["fixed_boundary_fields"],
            {
                "article_body_fetched": False,
                "human_attestation_required": True,
                "identity_authority": candidate_store.IDENTITY_AUTHORITY,
                "registry_write": False,
            },
        )
        self.assertEqual(
            self.store["article_url_binding"],
            "EXACT_LISTING_VENUE_OFFICIAL_HOST_AND_ARTICLE_PATH",
        )


if __name__ == "__main__":
    unittest.main()
