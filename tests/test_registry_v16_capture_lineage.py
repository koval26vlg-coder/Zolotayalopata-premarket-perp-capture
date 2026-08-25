"""Historical registry-prefix verification for production capture evidence."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for search_path in (ROOT / "src", ROOT / "tests"):
    if str(search_path) not in sys.path:
        sys.path.insert(0, str(search_path))

import event_registry as registry  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import test_activation_hardening_v6_registry as v6  # noqa: E402


class HistoricalCaptureLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "listing-events-v3.jsonl"
        self.records = v6._records()
        v6._write_records(self.path, self.records)
        with mock.patch.object(
            registry, "active_registry_contract_hash", return_value="b" * 64
        ):
            v6._write_refresh_summary(self.path, self.records)
        receipts, problems = registry._load_mutation_receipt_chain(self.path)
        self.assertEqual(problems, [])
        receipt = receipts[-1]
        official = next(
            record
            for record in self.records
            if record["timestamp_kind"] == registry.TIMESTAMP_OFFICIAL_SPOT_T0
        )
        self.evidence = {
            "episode_id": official["episode_id"],
            "venue": official["venue"],
            "listing_venue": official["listing_venue"],
            "premarket_contract_id": official["premarket_contract_id"],
            "spot_symbol": official["spot_symbol"],
            "official_spot_t0": official["timestamp_ts"],
            "t0_source_class": official["source_class"],
            "t0_precision_sec": official["t0_precision_sec"],
            "official_record_hash": official["record_hash"],
            "registry_sha256": receipt["registry_sha256"],
            "registry_tail_record_hash": receipt["registry_head_record_hash"],
            "mutation_receipt_seq": receipt["mutation_seq"],
            "mutation_receipt_hash": receipt["receipt_hash"],
            "summary_content_sha256": receipt["summary_content_hash"],
            "registry_authority_state_hash": registry.registry_authority_state_hash(
                active_generations=receipt[
                    registry.ACTIVE_LIFECYCLE_GENERATIONS_FIELD
                ],
                lifecycle_high_water=receipt[
                    registry.LIFECYCLE_GENERATION_HIGH_WATER_FIELD
                ],
                metadata_refresh_received_at=receipt[
                    registry.LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD
                ],
                raw_universe_rows_by_surface=receipt[
                    registry.RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD
                ],
                relevant_identity_hashes_by_surface=receipt[
                    registry.RELEVANT_IDENTITY_HASHES_BY_SURFACE_FIELD
                ],
                explicit_terminal_ids_by_surface=receipt[
                    registry.EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD
                ],
            ),
            "plan_id": receipt["plan_id"],
            "plan_hash": receipt["plan_hash"],
            "asset_class": official["asset_class"],
            "issuer_namespace": official["issuer_namespace"],
            "issuer_id": official["issuer_id"],
            "asset_identity_hash": official["asset_identity_hash"],
            "official_source_url": official["source_url"],
            "official_source_identity": official["source_identity"],
        }

    def append_later_revision(self) -> None:
        observation = v6._metadata_observation()
        observation["timestamp_ts"] = int(observation["timestamp_ts"]) + 60
        observation["received_at_utc"] = "2027-01-08T04:01:00Z"
        appended = registry.build_stream_revisions(self.records, [observation])
        self.assertEqual(len(appended), 1)
        lock_path = self.path.with_suffix(".lock")
        with registry.registry_lock(
            lock_path,
            run_id="later-metadata-revision",
            plan_hash=trust_root.PLAN_HASH,
        ) as owner:
            registry.append_entries(appended, self.path, lock_owner=owner)
            summary_path = self.path.with_suffix(".summary.json")
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary.update(
                {
                    "mutation_type": "metadata_refresh",
                    "mutation_run_id": "later-metadata-revision",
                    "refresh_run_id": "later-metadata-revision",
                    "registry": registry.verify_registry(
                        self.path,
                        verify_summary=False,
                        bootstrap_lock_owner=owner,
                    ),
                }
            )
            registry._write_summary_with_mutation_receipt(
                self.path,
                summary,
                lock_owner=owner,
            )

    def test_exact_historical_prefix_and_receipt_verify(self) -> None:
        report = registry.verify_capture_lineage(self.evidence, path=self.path)
        self.assertEqual(report["status"], "CAPTURE_LINEAGE_OK")
        self.assertEqual(report["registry_entries_at_capture"], 2)

    def test_later_append_does_not_invalidate_the_historical_prefix(self) -> None:
        self.append_later_revision()
        report = registry.verify_capture_lineage(self.evidence, path=self.path)
        self.assertEqual(report["status"], "CAPTURE_LINEAGE_OK")
        self.assertEqual(report["registry_entries_at_capture"], 2)
        self.assertEqual(report["current_registry_entries"], 3)

    def test_a_wrong_prefix_hash_is_rejected(self) -> None:
        evidence = dict(self.evidence, registry_sha256="0" * 64)
        with self.assertRaisesRegex(registry.EventRegistryError, "registry_sha256"):
            registry.verify_capture_lineage(evidence, path=self.path)

    def test_the_exact_mutation_receipt_is_required(self) -> None:
        evidence = dict(self.evidence, mutation_receipt_hash="0" * 64)
        with self.assertRaisesRegex(registry.EventRegistryError, "mutation receipt"):
            registry.verify_capture_lineage(evidence, path=self.path)

    def test_non_crypto_or_relabelled_identity_is_rejected(self) -> None:
        evidence = dict(
            self.evidence,
            asset_class=registry.ASSET_CLASS_EQUITY_ISSUER,
        )
        with self.assertRaisesRegex(registry.EventRegistryError, "asset|crypto"):
            registry.verify_capture_lineage(evidence, path=self.path)

    def test_official_source_url_and_identity_are_bound_to_the_record(self) -> None:
        for field, replacement in (
            ("official_source_url", "https://announcements.bybit.com/en-US/article/other"),
            ("official_source_identity", "bybit:announcement:substituted"),
        ):
            with self.subTest(field=field):
                evidence = dict(self.evidence, **{field: replacement})
                with self.assertRaisesRegex(
                    registry.EventRegistryError,
                    "official capture asset/timestamp lineage",
                ):
                    registry.verify_capture_lineage(evidence, path=self.path)

    def test_listing_venue_is_bound_to_the_official_record(self) -> None:
        evidence = dict(self.evidence, listing_venue="binance")
        with self.assertRaisesRegex(
            registry.EventRegistryError,
            "official capture asset/timestamp lineage",
        ):
            registry.verify_capture_lineage(evidence, path=self.path)

    def test_official_precision_is_bound_to_the_exact_record(self) -> None:
        evidence = dict(self.evidence, t0_precision_sec=1)
        with self.assertRaisesRegex(
            registry.EventRegistryError,
            "official capture asset/timestamp lineage",
        ):
            registry.verify_capture_lineage(evidence, path=self.path)

    def test_rehashed_record_chain_cannot_substitute_asset_identity_authority(self) -> None:
        mutations = (
            ("asset_identity_hash", "0" * 64),
            ("asset_class", "NOT_A_REAL_ASSET_CLASS"),
            ("identity_evidence_class", "NOT_A_REAL_EVIDENCE_CLASS"),
        )
        for field, replacement in mutations:
            with self.subTest(field=field):
                official = v6._official_observation()
                official[field] = replacement
                records = registry.build_stream_revisions(
                    [], [v6._metadata_observation(), official]
                )
                problems = registry._verify_records(records)
                self.assertTrue(
                    any("asset identity" in problem for problem in problems),
                    problems,
                )


if __name__ == "__main__":
    unittest.main()
