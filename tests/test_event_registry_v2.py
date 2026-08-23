from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import event_registry as registry
import frozen_plan_bindings as trust_root


class LifecycleTimestampTests(unittest.TestCase):
    @staticmethod
    def bybit_row(symbol: str = "NEWUSDT", launch: str = "1800000000000") -> dict:
        return {
            "symbol": symbol,
            "launchTime": launch,
            "status": "PreLaunch",
            "contractType": "LinearPerpetual",
            "isPreListing": True,
        }

    def test_bybit_metadata_populates_contract_launch_not_spot_t0(self) -> None:
        adapter = next(item for item in registry.ADAPTERS if item.venue == "bybit")
        event = registry.normalise_rows(
            adapter,
            [self.bybit_row()],
            observed_at_utc="2026-08-22T20:00:00Z",
        )[0]

        self.assertIn("premarket_contract_launch_ts", event)
        self.assertEqual(event["premarket_contract_launch_ts"], 1_800_000_000)
        self.assertIsNone(event["official_spot_t0"])

    def test_schedule_change_keeps_episode_and_source_stream_identity(self) -> None:
        adapter = next(item for item in registry.ADAPTERS if item.venue == "bybit")
        first = registry.normalise_rows(
            adapter,
            [self.bybit_row()],
            observed_at_utc="2026-08-22T20:00:00Z",
        )[0]
        moved = registry.normalise_rows(
            adapter,
            [self.bybit_row(launch="1800001800000")],
            observed_at_utc="2026-08-22T21:00:00Z",
        )[0]

        self.assertEqual(first["episode_id"], moved["episode_id"])
        self.assertEqual(first["stream_id"], moved["stream_id"])
        self.assertEqual(
            first["episode_id"], registry.make_episode_id("bybit", "NEWUSDT")
        )

    def test_normal_trading_contract_is_not_a_premarket_event(self) -> None:
        adapter = next(item for item in registry.ADAPTERS if item.venue == "bybit")
        row = self.bybit_row()
        row.update({"status": "Trading", "isPreListing": False})

        self.assertEqual(
            registry.normalise_rows(
                adapter, [row], observed_at_utc="2026-08-22T20:00:00Z"
            ),
            [],
        )

    def test_okx_transition_is_a_separate_lifecycle_stream(self) -> None:
        adapter = next(item for item in registry.ADAPTERS if item.venue == "okx")
        events = registry.normalise_rows(
            adapter,
            [{
                "instId": "NEW-USDT-250101",
                "instType": "FUTURES",
                "ruleType": "xperp",
                "listTime": "1799990000000",
                "preMktSwTime": "1800000000000",
            }],
            observed_at_utc="2026-08-22T20:00:00Z",
        )

        self.assertEqual(
            {item["timestamp_kind"] for item in events},
            {
                registry.TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
                registry.TIMESTAMP_TRANSITION,
            },
        )
        self.assertEqual(len({item["stream_id"] for item in events}), 2)
        transition = next(
            item for item in events if item["timestamp_kind"] == registry.TIMESTAMP_TRANSITION
        )
        self.assertEqual(transition["transition_ts"], 1_800_000_000)
        self.assertEqual(transition["evidence_use"], "DESCRIPTIVE_ONLY")


class CaptureSelectionContractTests(unittest.TestCase):
    def _write_production_registry(
        self,
        records: list[dict],
        *,
        summary_overrides: dict | None = None,
    ) -> Path:
        path = Path(tempfile.mkdtemp()) / "listing-events-v2.jsonl"
        path.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in records),
            encoding="utf-8",
        )
        report = registry.verify_registry(path, verify_summary=False)
        summary = {
            "schema": registry.REGISTRY_SCHEMA,
            "status": "REFRESH_COMPLETE",
            "complete": True,
            "refresh_run_id": "capture-selection-fixture",
            "plan_hash": trust_root.PLAN_HASH,
            "resolved_paths_hash": "b" * 64,
            "refreshed_at_utc": "2026-08-22T20:00:00Z",
            "registry": report,
        }
        summary.update(summary_overrides or {})
        path.with_suffix(".summary.json").write_text(
            json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path

    def test_source_class_is_a_required_keyword(self) -> None:
        parameter = inspect.signature(registry.events_for_capture).parameters["source_class"]
        self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_metadata_proxy_is_rejected_by_capture_selector(self) -> None:
        adapter = next(item for item in registry.ADAPTERS if item.venue == "bybit")
        event = registry.normalise_rows(
            adapter,
            [LifecycleTimestampTests.bybit_row()],
            observed_at_utc="2026-08-22T20:00:00Z",
        )[0]

        with self.assertRaises(registry.EventRegistryError):
            registry.events_for_capture(
                [event],
                now_ts=1_799_999_000,
                source_class=registry.SOURCE_VENUE_INSTRUMENT_METADATA,
            )

    def test_direct_official_sequence_is_not_capture_eligible_without_registry_receipt(self) -> None:
        observation = registry.make_timestamp_observation(
            episode_id="ep_bybit_new_0",
            venue="bybit",
            premarket_contract_id="NEWUSDT",
            spot_symbol="NEWUSDT",
            timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            timestamp_ts=1_800_000_000,
            instrument_role="spot",
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="bybit:announcement:123",
            source_url="https://announcements.bybit.com/example",
            received_at_utc="2026-08-22T20:00:00Z",
        )
        records = registry.build_stream_revisions([], [observation])

        with self.assertRaisesRegex(
            registry.EventRegistryError, "VERIFIED_PRODUCTION_REGISTRY_REQUIRED"
        ):
            registry.events_for_capture(
                records,
                now_ts=1_799_999_000,
                source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            )

    def test_registry_exposes_a_validated_timestamp_observation_factory(self) -> None:
        self.assertIsNotNone(getattr(registry, "make_timestamp_observation", None))

    def test_official_spot_timestamp_is_the_only_acceptance_anchor(self) -> None:
        try:
            event = registry.make_timestamp_observation(
                episode_id="ep_bybit_new_0",
                venue="bybit",
                premarket_contract_id="NEWUSDT",
                spot_symbol="NEWUSDT",
                timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
                timestamp_ts=1_800_000_000,
                instrument_role="spot",
                source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
                source_identity="bybit:announcement:123",
                source_url="https://announcements.bybit.com/example",
                received_at_utc="2026-08-22T20:00:00Z",
                precision_sec=1,
            )
        except registry.EventRegistryError as exc:
            self.fail(str(exc))

        self.assertEqual(event["official_spot_t0"], 1_800_000_000)
        self.assertEqual(event["evidence_use"], "ACCEPTANCE_ANCHOR")
        self.assertIs(event["capture_eligible"], True)

    def test_registry_exposes_episode_materialization(self) -> None:
        self.assertIsNotNone(getattr(registry, "materialize_episodes", None))

    def test_proxy_and_official_timestamps_coexist_in_one_episode(self) -> None:
        common = {
            "episode_id": "ep_bybit_new_0",
            "venue": "bybit",
            "premarket_contract_id": "NEWUSDT",
            "spot_symbol": "NEWUSDT",
            "received_at_utc": "2026-08-22T20:00:00Z",
            "precision_sec": 1,
        }
        contract_launch = registry.make_timestamp_observation(
            **common,
            timestamp_kind=registry.TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
            timestamp_ts=1_799_990_000,
            instrument_role="premarket_perp",
            source_class=registry.SOURCE_VENUE_INSTRUMENT_METADATA,
            source_identity="bybit:instrument_metadata:launchTime",
            source_url="https://api.bybit.com/v5/market/instruments-info",
        )
        official_spot = registry.make_timestamp_observation(
            **common,
            timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            timestamp_ts=1_800_000_000,
            instrument_role="spot",
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="bybit:announcement:123",
            source_url="https://announcements.bybit.com/example",
        )

        materialized = registry.materialize_episodes([contract_launch, official_spot])

        self.assertEqual(len(materialized), 1)
        self.assertEqual(materialized[0]["premarket_contract_launch_ts"], 1_799_990_000)
        self.assertEqual(materialized[0]["official_spot_t0"], 1_800_000_000)
        self.assertEqual(len(materialized[0]["timestamp_observations"]), 2)
        self.assertIs(materialized[0]["capture_eligible"], True)

    def test_capture_selector_materializes_all_streams_before_selecting(self) -> None:
        common = {
            "episode_id": "ep_bybit_new_0",
            "venue": "bybit",
            "premarket_contract_id": "NEWUSDT",
            "spot_symbol": "NEWUSDT",
            "received_at_utc": "2026-08-22T20:00:00Z",
        }
        proxy = registry.make_timestamp_observation(
            **common,
            timestamp_kind=registry.TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
            timestamp_ts=1_799_990_000,
            instrument_role="premarket_perp",
            source_class=registry.SOURCE_VENUE_INSTRUMENT_METADATA,
            source_identity="bybit:instrument_metadata:launchTime",
            source_url="https://api.bybit.com/v5/market/instruments-info",
        )
        official = registry.make_timestamp_observation(
            **common,
            timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            timestamp_ts=1_800_000_000,
            instrument_role="spot",
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="bybit:announcement:123",
            source_url="https://announcements.bybit.com/example",
        )
        records = registry.build_stream_revisions([], [proxy, official])

        path = self._write_production_registry(records)
        with mock.patch.object(registry, "REGISTRY_PATH", path):
            selected = registry.events_for_capture(
                now_ts=1_799_999_000,
                source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            )

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["official_spot_t0"], 1_800_000_000)
        self.assertEqual(selected[0]["premarket_contract_launch_ts"], 1_799_990_000)

    def test_conflicting_official_timestamps_fail_closed(self) -> None:
        common = {
            "episode_id": "ep_bybit_new_0",
            "venue": "bybit",
            "premarket_contract_id": "NEWUSDT",
            "spot_symbol": "NEWUSDT",
            "timestamp_kind": registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            "instrument_role": "spot",
            "source_class": registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            "received_at_utc": "2026-08-22T20:00:00Z",
        }
        first = registry.make_timestamp_observation(
            **common,
            timestamp_ts=1_800_000_000,
            source_identity="bybit:announcement:123",
            source_url="https://announcements.bybit.com/123",
        )
        conflicting = registry.make_timestamp_observation(
            **common,
            timestamp_ts=1_800_000_060,
            source_identity="bybit:announcement:456",
            source_url="https://announcements.bybit.com/456",
        )
        records = registry.build_stream_revisions([], [first, conflicting])

        path = self._write_production_registry(records)
        with mock.patch.object(registry, "REGISTRY_PATH", path):
            with self.assertRaisesRegex(registry.EventRegistryError, "OFFICIAL_CONFLICT"):
                registry.events_for_capture(
                    now_ts=1_799_999_000,
                    source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
                )

    def test_capture_selector_rejects_incomplete_summary_even_when_head_matches(self) -> None:
        observation = registry.make_timestamp_observation(
            episode_id="ep_bybit_new_0",
            venue="bybit",
            premarket_contract_id="NEWUSDT",
            spot_symbol="NEWUSDT",
            timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            timestamp_ts=1_800_000_000,
            instrument_role="spot",
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="bybit:announcement:123",
            source_url="https://announcements.bybit.com/example",
            received_at_utc="2026-08-22T20:00:00Z",
        )
        path = self._write_production_registry(
            registry.build_stream_revisions([], [observation]),
            summary_overrides={"complete": False},
        )

        with mock.patch.object(registry, "REGISTRY_PATH", path):
            with self.assertRaisesRegex(
                registry.EventRegistryError, "summary complete flag"
            ):
                registry.events_for_capture(
                    now_ts=1_799_999_000,
                    source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
                )

    def test_capture_selector_materializes_the_same_snapshot_it_verified(self) -> None:
        observation = registry.make_timestamp_observation(
            episode_id="ep_bybit_new_0",
            venue="bybit",
            premarket_contract_id="NEWUSDT",
            spot_symbol="NEWUSDT",
            timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            timestamp_ts=1_800_000_000,
            instrument_role="spot",
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="bybit:announcement:123",
            source_url="https://announcements.bybit.com/example",
            received_at_utc="2026-08-22T20:00:00Z",
        )
        path = self._write_production_registry(
            registry.build_stream_revisions([], [observation])
        )

        with mock.patch.object(registry, "REGISTRY_PATH", path), mock.patch.object(
            registry, "load_registry", wraps=registry.load_registry
        ) as load_registry:
            selected = registry.events_for_capture(
                now_ts=1_799_999_000,
                source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            )

        self.assertEqual(len(selected), 1)
        load_registry.assert_called_once_with(path)

    def test_metadata_timestamp_is_descriptive_only(self) -> None:
        adapter = next(item for item in registry.ADAPTERS if item.venue == "bybit")
        event = registry.normalise_rows(
            adapter,
            [LifecycleTimestampTests.bybit_row()],
            observed_at_utc="2026-08-22T20:00:00Z",
        )[0]

        self.assertEqual(event.get("evidence_use"), "DESCRIPTIVE_ONLY")
        self.assertIs(event.get("capture_eligible"), False)

    def test_gate_create_time_is_not_promoted_to_contract_launch(self) -> None:
        adapter = next(item for item in registry.ADAPTERS if item.venue == "gate")
        event = registry.normalise_rows(
            adapter,
            [{"name": "NEW_USDT", "create_time": "1800000000", "status": "prelaunch"}],
            observed_at_utc="2026-08-22T20:00:00Z",
        )[0]

        self.assertIsNone(event["premarket_contract_launch_ts"])
        self.assertEqual(event["contract_created_ts"], 1_800_000_000)
        self.assertIsNone(event["official_spot_t0"])


class RegistryLineageTests(unittest.TestCase):
    def _observation(
        self,
        *,
        timestamp_ts: int = 1_800_000_000,
        source_class: str = registry.SOURCE_VENUE_INSTRUMENT_METADATA,
        source_identity: str = "bybit:instrument_metadata:launchTime",
    ) -> dict:
        return registry.make_timestamp_observation(
            episode_id="ep_bybit_new_0",
            venue="bybit",
            premarket_contract_id="NEWUSDT",
            spot_symbol="NEWUSDT",
            timestamp_kind=registry.TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
            timestamp_ts=timestamp_ts,
            instrument_role="premarket_perp",
            source_class=source_class,
            source_identity=source_identity,
            source_url="https://api.bybit.com/v5/market/instruments-info",
            received_at_utc="2026-08-22T20:00:00Z",
        )

    def test_first_record_starts_both_hash_chains(self) -> None:
        records = registry.build_stream_revisions([], [self._observation()])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record_seq"], 0)
        self.assertIsNone(records[0]["previous_record_hash"])
        self.assertEqual(records[0]["stream_revision"], 0)
        self.assertIsNone(records[0]["supersedes_record_hash"])
        self.assertEqual(records[0]["record_hash"], registry._record_hash(records[0]))

    def test_changed_value_supersedes_exact_stream_head(self) -> None:
        first = registry.build_stream_revisions([], [self._observation()])
        second = registry.build_stream_revisions(
            first, [self._observation(timestamp_ts=1_800_001_800)]
        )

        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["record_seq"], 1)
        self.assertEqual(second[0]["previous_record_hash"], first[0]["record_hash"])
        self.assertEqual(second[0]["stream_revision"], 1)
        self.assertEqual(second[0]["supersedes_record_hash"], first[0]["record_hash"])

    def test_source_classes_have_separate_revision_streams(self) -> None:
        metadata = registry.build_stream_revisions([], [self._observation()])
        official_contract_notice = self._observation(
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="bybit:announcement:contract-launch:123",
        )
        appended = registry.build_stream_revisions(metadata, [official_contract_notice])

        self.assertEqual(appended[0]["stream_revision"], 0)
        self.assertIsNone(appended[0]["supersedes_record_hash"])

    def test_verifier_rejects_a_wrong_global_predecessor(self) -> None:
        records = registry.build_stream_revisions([], [self._observation()])
        records += registry.build_stream_revisions(
            records, [self._observation(timestamp_ts=1_800_001_800)]
        )
        records[1]["previous_record_hash"] = "0" * 64
        records[1]["record_hash"] = registry._record_hash(records[1])
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        path.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in records),
            encoding="utf-8",
        )

        report = registry.verify_registry(path)

        self.assertEqual(report["status"], "REGISTRY_PROBLEMS")
        self.assertIn("previous_record_hash", " ".join(report["problems"]))

    def test_verifier_rejects_orphan_stream_revision(self) -> None:
        record = registry.build_stream_revisions([], [self._observation()])[0]
        record["stream_revision"] = 7
        record["record_hash"] = registry._record_hash(record)
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

        report = registry.verify_registry(path)

        self.assertIn("stream_revision", " ".join(report["problems"]))

    def test_verifier_rejects_stream_identity_relabeling(self) -> None:
        record = registry.build_stream_revisions([], [self._observation()])[0]
        record["source_class"] = registry.SOURCE_OBSERVED_LIFECYCLE
        record["t0_source_class"] = registry.SOURCE_OBSERVED_LIFECYCLE
        record["record_hash"] = registry._record_hash(record)
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

        report = registry.verify_registry(path)

        self.assertIn("stream_id", " ".join(report["problems"]))

    def test_nonempty_production_registry_requires_summary_receipt(self) -> None:
        records = registry.build_stream_revisions([], [self._observation()])
        path = Path(tempfile.mkdtemp()) / "listing-events-v2.jsonl"
        path.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in records),
            encoding="utf-8",
        )

        with mock.patch.object(registry, "REGISTRY_PATH", path):
            report = registry.verify_registry(path)

        self.assertEqual(report["status"], "REGISTRY_PROBLEMS")
        self.assertIn("required summary receipt", " ".join(report["problems"]))
        self.assertEqual(
            report["recovery_action"],
            "RESTORE_MATCHING_SUMMARY_OR_QUARANTINE_AND_BOOTSTRAP_NEW_GENERATION",
        )

    def test_empty_production_registry_is_an_explicit_bootstrap_state(self) -> None:
        path = Path(tempfile.mkdtemp()) / "listing-events-v2.jsonl"

        with mock.patch.object(registry, "REGISTRY_PATH", path):
            report = registry.verify_registry(path)

        self.assertEqual(report["status"], "REGISTRY_OK")
        self.assertEqual(report["bootstrap_state"], "EMPTY_REGISTRY_BOOTSTRAP")
        self.assertIsNone(report["recovery_action"])

    def test_summary_verification_cannot_be_disabled_for_nonempty_production(self) -> None:
        records = registry.build_stream_revisions([], [self._observation()])
        path = Path(tempfile.mkdtemp()) / "listing-events-v2.jsonl"
        path.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in records),
            encoding="utf-8",
        )

        with mock.patch.object(registry, "REGISTRY_PATH", path):
            report = registry.verify_registry(path, verify_summary=False)

        self.assertEqual(report["status"], "REGISTRY_PROBLEMS")
        self.assertIn("cannot be disabled", " ".join(report["problems"]))

    def test_malformed_summary_root_is_reported_fail_closed(self) -> None:
        records = registry.build_stream_revisions([], [self._observation()])
        path = Path(tempfile.mkdtemp()) / "listing-events-v2.jsonl"
        path.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in records),
            encoding="utf-8",
        )
        path.with_suffix(".summary.json").write_text("[]\n", encoding="utf-8")

        with mock.patch.object(registry, "REGISTRY_PATH", path):
            report = registry.verify_registry(path)

        self.assertEqual(report["status"], "REGISTRY_PROBLEMS")
        self.assertIn("summary receipt is unreadable", " ".join(report["problems"]))

    def test_locked_writer_can_verify_transient_summary_pending_state(self) -> None:
        records = registry.build_stream_revisions([], [self._observation()])
        path = Path(tempfile.mkdtemp()) / "listing-events-v2.jsonl"
        path.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in records),
            encoding="utf-8",
        )
        lock_path = path.with_suffix(".lock")

        with mock.patch.object(registry, "REGISTRY_PATH", path), mock.patch.object(
            registry, "REGISTRY_LOCK_PATH", lock_path
        ):
            owner = registry.acquire_registry_lock(
                lock_path, run_id="summary-bootstrap", plan_hash=trust_root.PLAN_HASH
            )
            try:
                report = registry.verify_registry(
                    path,
                    verify_summary=False,
                    bootstrap_lock_owner=owner,
                )
            finally:
                registry.release_registry_lock(owner)

        self.assertEqual(report["status"], "REGISTRY_OK")
        self.assertEqual(report["bootstrap_state"], "SUMMARY_PENDING_UNDER_LOCK")


class RegistryLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock_path = Path(tempfile.mkdtemp()) / "listing-events-v2.lock"

    def test_competing_writer_cannot_take_registry_lock(self) -> None:
        owner = registry.acquire_registry_lock(
            self.lock_path, run_id="run-one", plan_hash="a" * 64
        )
        try:
            with self.assertRaisesRegex(registry.EventRegistryError, "REGISTRY_LOCKED"):
                registry.acquire_registry_lock(
                    self.lock_path, run_id="run-two", plan_hash="a" * 64
                )
        finally:
            registry.release_registry_lock(owner)

    def test_release_requires_the_exact_owner_nonce(self) -> None:
        owner = registry.acquire_registry_lock(
            self.lock_path, run_id="run-one", plan_hash="a" * 64
        )
        forged = registry.RegistryLockOwner(
            path=owner.path,
            owner_pid=owner.owner_pid,
            owner_host=owner.owner_host,
            run_id=owner.run_id,
            nonce="wrong",
            plan_hash=owner.plan_hash,
            acquired_at_utc=owner.acquired_at_utc,
        )
        with self.assertRaisesRegex(registry.EventRegistryError, "owner mismatch"):
            registry.release_registry_lock(forged)
        self.assertTrue(self.lock_path.exists())
        registry.release_registry_lock(owner)
        self.assertFalse(self.lock_path.exists())

    def test_registry_append_requires_an_explicit_lock_owner(self) -> None:
        parameter = inspect.signature(registry.append_entries).parameters["lock_owner"]
        self.assertIs(parameter.default, inspect.Parameter.empty)


class MetadataRefreshContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "listing-events-v2.jsonl"
        self.preflight = {
            "schema": "premarket_write_preflight_v2",
            "ok": True,
            "verified": True,
            "decision": "ALLOW_METADATA_REGISTRY",
            "write_class": "metadata_registry",
            "run_id": "metadata-test",
            "plan_id": "premarket_perp_capture_20260822_v2",
            "plan_hash": "a" * 64,
            "resolved_paths_hash": "b" * 64,
        }
        self.payloads = {
            "bybit": {
                "retCode": 0,
                "result": {
                    "category": "linear",
                    "list": [LifecycleTimestampTests.bybit_row()],
                    "nextPageCursor": "",
                },
            },
            "okx": {
                "code": "0",
                "data": [
                    {
                        "instId": "NEW-USDT-250101",
                        "instType": "FUTURES",
                        "ruleType": "pre_market",
                        "listTime": "1800000000000",
                    }
                ],
            },
            "gate": [
                {
                    "name": "NEW_USDT",
                    "status": "prelaunch",
                    "create_time": "1800000000",
                }
            ],
        }

    def _refresh(self, payloads: dict) -> dict:
        with mock.patch.object(
            registry.risk_gate, "preflight", return_value=self.preflight
        ) as preflight:
            result = registry.refresh(
                payloads=payloads,
                path=self.path,
                observed_at_utc="2026-08-22T20:00:00Z",
                run_id="metadata-test",
            )
        preflight.assert_called_once_with(
            write_class="metadata_registry", run_id="metadata-test"
        )
        return result

    def test_all_venues_stage_before_one_lineage_bound_write(self) -> None:
        result = self._refresh(self.payloads)

        self.assertEqual(result["status"], "REFRESH_COMPLETE")
        self.assertEqual(result["appended_entries"], 3)
        self.assertEqual(registry.verify_registry(self.path)["status"], "REGISTRY_OK")

    def test_summary_head_detects_tail_truncation(self) -> None:
        self._refresh(self.payloads)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

        report = registry.verify_registry(self.path)

        self.assertEqual(report["status"], "REGISTRY_PROBLEMS")
        self.assertIn("summary", " ".join(report["problems"]))

    def test_missing_venue_is_incomplete_and_writes_no_registry(self) -> None:
        payloads = dict(self.payloads)
        payloads.pop("gate")

        result = self._refresh(payloads)

        self.assertEqual(result["status"], "INCOMPLETE_NO_REGISTRY_WRITE")
        self.assertFalse(self.path.exists())

    def test_malformed_payload_is_incomplete_and_writes_no_registry(self) -> None:
        payloads = dict(self.payloads)
        payloads["okx"] = {"code": "0", "data": {}}

        result = self._refresh(payloads)

        self.assertEqual(result["status"], "INCOMPLETE_NO_REGISTRY_WRITE")
        self.assertFalse(self.path.exists())

    def test_preflight_failure_happens_before_network_or_write(self) -> None:
        blocked = dict(self.preflight, ok=False, verified=False)
        with mock.patch.object(
            registry.risk_gate, "preflight", return_value=blocked
        ), mock.patch.object(
            registry.public_http, "get_json", side_effect=AssertionError("network reached")
        ):
            with self.assertRaisesRegex(registry.EventRegistryError, "PREFLIGHT_BLOCKED"):
                registry.refresh(
                    path=self.path,
                    observed_at_utc="2026-08-22T20:00:00Z",
                    run_id="metadata-test",
                )
        self.assertFalse(self.path.exists())

    def test_malformed_preflight_receipt_cannot_authorize_refresh(self) -> None:
        malformed = dict(self.preflight, schema="forged")
        with mock.patch.object(
            registry.risk_gate, "preflight", return_value=malformed
        ), mock.patch.object(
            registry.public_http, "get_json", side_effect=AssertionError("network reached")
        ):
            with self.assertRaisesRegex(registry.EventRegistryError, "PREFLIGHT_BLOCKED"):
                registry.refresh(
                    path=self.path,
                    observed_at_utc="2026-08-22T20:00:00Z",
                    run_id="metadata-test",
                )
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
