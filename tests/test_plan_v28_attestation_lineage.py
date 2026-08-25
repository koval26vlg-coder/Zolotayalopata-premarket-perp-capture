"""Immutable v28 contract for cross-venue and PlanOnly-rollover lineage."""

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

import plan_builder  # noqa: E402
import project_config as config  # noqa: E402
import risk_gate  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402


V27_RELATIVE_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v27.json"
V27_PLAN_HASH = "859bd59a406dd97ae0fb1e8239f5f34541a50cb08cbb39fbda4d189c5d7b2446"
V27_FILE_SHA256 = "de5c2bd1998bebd7cedd7ed728aa992ef4515ccbab4248a1d6aa8a63d644bfac"
V28_STATUS = "OFFICIAL_ATTESTATION_LINEAGE_HARDENED_NO_CAPTURE"


class V28AttestationLineageTests(unittest.TestCase):
    def test_v27_bytes_are_preserved_and_v28_supersedes_exact_identity(self) -> None:
        v27_path = ROOT / V27_RELATIVE_PATH
        self.assertEqual(
            hashlib.sha256(v27_path.read_bytes()).hexdigest(), V27_FILE_SHA256
        )
        self.assertEqual(
            json.loads(v27_path.read_text(encoding="utf-8"))["plan_hash"],
            V27_PLAN_HASH,
        )
        self.assertEqual(config.V27_PLAN_PATH, v27_path)
        self.assertEqual(
            config.PLAN_PATH.name,
            "premarket-perp-capture-planonly-20260822-v28.json",
        )

        plan = plan_builder.build_plan("2026-08-26T00:00:00.000Z")
        self.assertEqual(plan["schema"], "premarket_perp_capture_planonly_v28")
        self.assertEqual(plan["plan_id"], "premarket_perp_capture_20260822_v28")
        self.assertEqual(plan["supersedes_plan_id"], "premarket_perp_capture_20260822_v27")
        self.assertEqual(plan["supersedes_plan_hash"], V27_PLAN_HASH)
        self.assertEqual(plan["supersedes_plan_path"], V27_RELATIVE_PATH)
        self.assertEqual(plan["status"], V28_STATUS)
        self.assertFalse(plan["activation_gate"]["capture_authorized"])
        self.assertFalse(plan["acceptance_policy"]["acceptance_capable"])

    def test_v28_preregisters_cross_venue_and_rollover_lineage(self) -> None:
        self.assertIn("listing_venue", config.CAPTURE_LINEAGE_FIELDS)
        self.assertEqual(
            config.CAPTURE_LINEAGE_FIELDS.index("listing_venue"),
            config.CAPTURE_LINEAGE_FIELDS.index("venue") + 1,
        )

        plan = plan_builder.build_plan("2026-08-26T00:00:00.000Z")
        registry = plan["event_registry"]
        attestation = registry["official_attestation"]
        self.assertEqual(
            attestation["venue_roles"],
            {
                "venue": "PERPETUAL_CONTRACT_VENUE",
                "listing_venue": "OFFICIAL_SPOT_ANNOUNCEMENT_VENUE",
            },
        )
        self.assertEqual(
            attestation["locked_rebuild_field_preservation"],
            ["venue", "listing_venue"],
        )
        self.assertEqual(
            registry["capture_lineage_verification"]
            ["latest_mutation_receipt_plan_identity"],
            "EXACT_ACTIVE_PLAN_ID_AND_HASH",
        )
        self.assertEqual(
            registry["capture_lineage_fields"], list(config.CAPTURE_LINEAGE_FIELDS)
        )

    def test_v28_write_authority_stays_no_capture(self) -> None:
        plan = plan_builder.build_plan("2026-08-26T00:00:00.000Z")
        authorization = risk_gate.PLAN_WRITE_AUTHORIZATION[plan["status"]]
        self.assertEqual(
            authorization["write_classes"],
            frozenset(
                {"metadata_registry", "official_attestation", "registry_quarantine"}
            ),
        )
        self.assertNotIn("market_data_capture", authorization["write_classes"])
        self.assertFalse(plan["activation_gate"]["capture_authorized"])

    def test_v28_is_the_external_trust_root_and_v27_is_retired(self) -> None:
        plan = risk_gate.load_and_verify_plan()
        self.assertEqual(plan["plan_id"], "premarket_perp_capture_20260822_v28")
        self.assertEqual(plan["plan_hash"], trust_root.PLAN_HASH)
        self.assertEqual(
            hashlib.sha256(config.PLAN_PATH.read_bytes()).hexdigest(),
            trust_root.PLAN_FILE_SHA256,
        )
        retired = {
            str(item["path"]).replace("\\", "/"): item
            for item in trust_root.RETIRED_PLANS
        }
        self.assertIn(V27_RELATIVE_PATH, retired)
        self.assertEqual(retired[V27_RELATIVE_PATH]["plan_hash"], V27_PLAN_HASH)
        self.assertEqual(
            retired[V27_RELATIVE_PATH]["plan_file_sha256"], V27_FILE_SHA256
        )


if __name__ == "__main__":
    unittest.main()
