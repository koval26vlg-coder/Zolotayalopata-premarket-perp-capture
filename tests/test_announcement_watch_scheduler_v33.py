"""Contract tests for the quiet no-model announcement watcher scheduler."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys
import json
import tempfile
from unittest import mock
import contextlib
import io
import subprocess
import hashlib
import ast
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import announcement_watch_scheduler as scheduler  # noqa: E402
import announcement_watch_state as watch_state  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import project_config as config  # noqa: E402
import risk_gate  # noqa: E402
import plan_builder  # noqa: E402
from canonical_hash import canonical_hash  # noqa: E402


class SchedulerSurfaceTests(unittest.TestCase):
    def test_scheduler_runtime_and_windows_task_tools_exist(self) -> None:
        expected = (
            ROOT / "src/announcement_watch_state.py",
            ROOT / "src/announcement_watch_scheduler.py",
            ROOT / "tools/start_premarket_announcement_watch_scheduler.ps1",
            ROOT / "tools/install_premarket_announcement_watch_scheduler.ps1",
        )

        self.assertEqual([path.name for path in expected if not path.is_file()], [])

    def test_scheduler_exposes_a_pure_cadence_classifier(self) -> None:
        self.assertTrue(callable(getattr(scheduler, "choose_cadence", None)))
        self.assertTrue(callable(getattr(scheduler, "run_scheduled_tick", None)))
        self.assertTrue(callable(getattr(scheduler, "main", None)))
        self.assertTrue(callable(getattr(scheduler, "default_store", None)))

    def test_state_runtime_exposes_the_durable_store_surface(self) -> None:
        self.assertTrue(callable(getattr(watch_state, "WatchPaths", None)))
        self.assertTrue(callable(getattr(watch_state, "WatchStateStore", None)))
        self.assertTrue(hasattr(watch_state.WatchStateStore, "acquire_claim"))
        self.assertTrue(hasattr(watch_state.WatchStateStore, "release_claim"))

    def test_every_scheduled_control_runtime_is_sha_bound(self) -> None:
        bound = dict(config.BOUND_RUNTIME_FILES)
        self.assertEqual(bound["announcement_watch_state"], "src/announcement_watch_state.py")
        self.assertEqual(
            bound["announcement_watch_scheduler"],
            "src/announcement_watch_scheduler.py",
        )
        self.assertEqual(
            bound["announcement_watch_launcher"],
            "tools/start_premarket_announcement_watch_scheduler.ps1",
        )
        self.assertEqual(
            bound["announcement_watch_installer"],
            "tools/install_premarket_announcement_watch_scheduler.ps1",
        )

    def test_research_dependencies_are_lazy_on_the_five_minute_wake_path(self) -> None:
        source = (ROOT / "src/announcement_watch_scheduler.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in tree.body
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertFalse(
            imported
            & {"announcement_discovery", "event_registry", "risk_gate"}
        )


class AdaptiveCadenceTests(unittest.TestCase):
    NOW = 1_800_000_000

    @staticmethod
    def report(*rows: dict) -> dict:
        return {"status": "NO_SECONDS_GRADE_CANDIDATE", "rejections": list(rows)}

    @staticmethod
    def official_row(*, t0: int, precision: int = 1, reasons: list[str] | None = None) -> dict:
        return {
            "official_spot_t0": t0,
            "t0_precision_sec": precision,
            "asset_class": "CRYPTO_TOKEN",
            "reasons": list(reasons or ["OUTSIDE_CAPTURE_DUE_WINDOW"]),
        }

    def decision(self, **overrides: object) -> dict:
        arguments = {
            "now_ts": self.NOW,
            "active_unattested_count": 0,
            "unverified_candidate_count": 0,
            "candidate_report": self.report(),
        }
        arguments.update(overrides)
        result = scheduler.choose_cadence(**arguments)
        self.assertIsInstance(result, dict)
        return result

    def test_no_active_crypto_event_uses_six_hour_search(self) -> None:
        self.assertEqual(
            self.decision(),
            {"cadence_stage": "SEARCH", "interval_sec": 21_600},
        )

    def test_active_or_unverified_event_is_capped_at_three_hours(self) -> None:
        active = self.decision(active_unattested_count=1)
        unverified = self.decision(unverified_candidate_count=1)
        self.assertEqual(active, {"cadence_stage": "ACTIVE_UNATTESTED", "interval_sec": 10_800})
        self.assertEqual(unverified, {"cadence_stage": "UNVERIFIED_CANDIDATE", "interval_sec": 10_800})

    def test_human_official_evidence_without_seconds_grade_t0_uses_one_hour(self) -> None:
        row = self.official_row(
            t0=self.NOW + 86_401,
            precision=60,
            reasons=["OFFICIAL_T0_PRECISION_GT_ONE_SECOND", "OUTSIDE_CANDIDATE_HORIZON"],
        )
        self.assertEqual(
            self.decision(candidate_report=self.report(row)),
            {"cadence_stage": "OFFICIAL_CONFIRMED", "interval_sec": 3_600},
        )

    def test_exact_official_t0_within_24_hours_uses_five_minutes(self) -> None:
        row = self.official_row(t0=self.NOW + 86_400)
        self.assertEqual(
            self.decision(candidate_report=self.report(row)),
            {"cadence_stage": "EXACT_T0_WITHIN_24H", "interval_sec": 300},
        )

    def test_exact_official_t0_beyond_24_hours_stays_hourly(self) -> None:
        row = self.official_row(
            t0=self.NOW + 86_401,
            reasons=["OUTSIDE_CAPTURE_DUE_WINDOW", "OUTSIDE_CANDIDATE_HORIZON"],
        )
        self.assertEqual(
            self.decision(candidate_report=self.report(row)),
            {"cadence_stage": "OFFICIAL_CONFIRMED", "interval_sec": 3_600},
        )

    def test_proxy_timestamp_never_escalates_beyond_three_hours(self) -> None:
        row = self.official_row(
            t0=self.NOW + 60,
            reasons=["SOURCE_CLASS_NOT_OFFICIAL_ANNOUNCEMENT"],
        )
        self.assertEqual(
            self.decision(active_unattested_count=1, candidate_report=self.report(row)),
            {"cadence_stage": "ACTIVE_UNATTESTED", "interval_sec": 10_800},
        )

    def test_terminal_official_episode_returns_to_search(self) -> None:
        row = self.official_row(
            t0=self.NOW + 60,
            reasons=["LIFECYCLE_GENERATION_NOT_CURRENT"],
        )
        self.assertEqual(
            self.decision(candidate_report=self.report(row)),
            {"cadence_stage": "SEARCH", "interval_sec": 21_600},
        )


class DurableStateTests(unittest.TestCase):
    NOW = 1_800_000_000

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = watch_state.WatchPaths(
            state=root / "state.json",
            ledger=root / "attempts.jsonl",
            claim=root / "watch.claim.json",
            claim_archive=root / "claim-archive",
        )
        self.store = watch_state.WatchStateStore(
            self.paths,
            plan_id=trust_root.PLAN_ID,
            plan_hash=trust_root.PLAN_HASH,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _terminal_without_state(
        self,
        *,
        status: str = "COMPLETE",
        stage: str = "SEARCH",
        interval_sec: int = 21_600,
        pending_retry: bool = False,
    ) -> dict:
        started = self.store.begin_attempt(
            now_ts=self.NOW,
            cadence_stage=stage,
            interval_sec=interval_sec,
        )
        return self.store.record_terminal(
            attempt_id=started["attempt_id"],
            now_ts=self.NOW + 10,
            terminal_status=status,
            cadence_stage=stage,
            interval_sec=interval_sec,
            pending_retry=pending_retry,
            metadata_status="REFRESH_COMPLETE",
            discovery_status="NO_ANNOUNCEMENT_TARGETS",
            candidate_status="NO_SECONDS_GRADE_CANDIDATE",
            announcement_requests=0,
            appended_candidates=0,
            reason=None,
        )

    def test_absent_state_is_due_without_creating_any_control_artifact(self) -> None:
        self.assertEqual(self.store.probe_due(now_ts=self.NOW)["status"], "DUE")
        self.assertEqual([path for path in (self.paths.state, self.paths.ledger, self.paths.claim) if path.exists()], [])

    def test_terminal_ledger_then_atomic_state_makes_not_due_read_only(self) -> None:
        terminal = self._terminal_without_state()
        state = self.store.commit_terminal_state(terminal)
        before = {
            self.paths.state: self.paths.state.read_bytes(),
            self.paths.ledger: self.paths.ledger.read_bytes(),
        }

        due = self.store.probe_due(now_ts=self.NOW + 11)

        self.assertEqual(due["status"], "NOT_DUE")
        self.assertEqual(due["next_interval_at_utc"], state["next_interval_at_utc"])
        self.assertEqual(
            before,
            {path: path.read_bytes() for path in before},
        )
        rows = [json.loads(line) for line in self.paths.ledger.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["phase"] for row in rows], ["STARTED", "TERMINAL"])
        self.assertEqual(rows[1]["previous_record_hash"], rows[0]["record_hash"])
        self.assertEqual(state["attempt_ledger_head_hash"], rows[1]["record_hash"])

    def test_terminal_ledger_without_state_is_reconciled_before_network(self) -> None:
        terminal = self._terminal_without_state(
            status="RETRY_NEXT_INTERVAL",
            stage="ACTIVE_UNATTESTED",
            interval_sec=10_800,
            pending_retry=True,
        )
        self.assertEqual(
            self.store.probe_due(now_ts=self.NOW + 11)["status"],
            "CONTROL_RECOVERY_REQUIRED",
        )

        recovered = self.store.reconcile_from_ledger()

        self.assertEqual(recovered["status"], "RETRY_NEXT_INTERVAL")
        self.assertTrue(recovered["pending_retry"])
        self.assertEqual(recovered["attempt_ledger_head_hash"], terminal["record_hash"])
        self.assertEqual(self.store.probe_due(now_ts=self.NOW + 11)["status"], "NOT_DUE")

    def test_ledger_tampering_fails_closed(self) -> None:
        terminal = self._terminal_without_state()
        self.store.commit_terminal_state(terminal)
        rows = self.paths.ledger.read_text(encoding="utf-8").splitlines()
        forged = json.loads(rows[-1])
        forged["terminal_status"] = "COMPLETE_FORGED"
        rows[-1] = json.dumps(forged, sort_keys=True)
        self.paths.ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")

        with self.assertRaises(watch_state.WatchStateError):
            self.store.probe_due(now_ts=self.NOW + 11)


class WatchClaimTests(DurableStateTests):
    def test_live_duplicate_is_skipped_without_mutating_the_claim(self) -> None:
        first = self.store.acquire_claim(now_ts=self.NOW)
        before = self.paths.claim.read_bytes()

        duplicate = self.store.acquire_claim(now_ts=self.NOW + 1)

        self.assertEqual(first["status"], "ACQUIRED")
        self.assertEqual(duplicate["status"], "ALREADY_RUNNING")
        self.assertEqual(self.paths.claim.read_bytes(), before)
        archived = self.store.release_claim(
            first["claim"], terminal_status="COMPLETE", now_ts=self.NOW + 2
        )
        self.assertFalse(self.paths.claim.exists())
        self.assertTrue(archived.is_file())

    def test_dead_claim_is_archived_before_a_recovery_owner_is_created(self) -> None:
        first = self.store.acquire_claim(now_ts=self.NOW)
        with mock.patch.object(watch_state, "process_is_alive", return_value=False):
            recovered = self.store.acquire_claim(now_ts=self.NOW + 300)

        self.assertEqual(recovered["status"], "ACQUIRED")
        self.assertTrue(recovered["stale_claim_recovered"])
        self.assertNotEqual(
            recovered["claim"]["claim_id"], first["claim"]["claim_id"]
        )
        self.assertEqual(len(list(self.paths.claim_archive.glob("*.json"))), 1)
        self.store.release_claim(
            recovered["claim"],
            terminal_status="RETRY_NEXT_INTERVAL",
            now_ts=self.NOW + 301,
        )


class ScheduledTickTests(DurableStateTests):
    def _seed_state(
        self,
        *,
        finished_ts: int,
        stage: str = "SEARCH",
        interval_sec: int = 21_600,
    ) -> None:
        started = self.store.begin_attempt(
            now_ts=finished_ts - 1,
            cadence_stage=stage,
            interval_sec=interval_sec,
        )
        terminal = self.store.record_terminal(
            attempt_id=started["attempt_id"],
            now_ts=finished_ts,
            terminal_status="COMPLETE",
            cadence_stage=stage,
            interval_sec=interval_sec,
            pending_retry=False,
            metadata_status="REFRESH_COMPLETE",
            discovery_status="NO_ANNOUNCEMENT_TARGETS",
            candidate_status="NO_SECONDS_GRADE_CANDIDATE",
            announcement_requests=0,
            appended_candidates=0,
            reason=None,
        )
        self.store.commit_terminal_state(terminal)

    @staticmethod
    def good_control_preflight(**kwargs: object) -> dict:
        resolved_paths = risk_gate.resolved_config()
        return {
            "schema": risk_gate.PREFLIGHT_RESULT_SCHEMA,
            "ok": True,
            "verified": True,
            "decision": "ALLOW_ANNOUNCEMENT_WATCH_CONTROL",
            "write_class": "announcement_watch_control",
            "run_id": kwargs["run_id"],
            "action": risk_gate.ANNOUNCEMENT_WATCH_CONTROL_ACTION,
            "plan_id": trust_root.PLAN_ID,
            "plan_hash": trust_root.PLAN_HASH,
            "resolved_paths": resolved_paths,
            "resolved_paths_hash": canonical_hash(resolved_paths),
        }

    def invoke(
        self,
        *,
        now_ts: int | None = None,
        control_preflight=None,
        metadata_refresh=None,
        announcement_discovery=None,
        candidate_inspection=None,
        candidate_alert=None,
        clock=None,
    ) -> dict:
        tick_now = self.NOW if now_ts is None else now_ts
        result = scheduler.run_scheduled_tick(
            now_ts=tick_now,
            store=self.store,
            control_preflight=control_preflight or self.good_control_preflight,
            metadata_refresh=metadata_refresh or (lambda **_: {"status": "REFRESH_COMPLETE"}),
            announcement_discovery=announcement_discovery or (
                lambda **_: {
                    "status": "NO_ANNOUNCEMENT_TARGETS",
                    "targets": [],
                    "matched_candidates": 0,
                    "announcement_requests": 0,
                    "appended_candidates": 0,
                }
            ),
            candidate_inspection=candidate_inspection or (
                lambda **_: {
                    "status": "NO_SECONDS_GRADE_CANDIDATE",
                    "candidates": [],
                    "rejections": [],
                }
            ),
            candidate_alert=candidate_alert,
            clock=clock or (lambda: tick_now),
        )
        self.assertIsInstance(result, dict)
        return result

    def test_not_due_returns_before_preflight_claim_network_or_write(self) -> None:
        self._seed_state(finished_ts=self.NOW - 10)
        before = {
            self.paths.state: self.paths.state.read_bytes(),
            self.paths.ledger: self.paths.ledger.read_bytes(),
        }
        control = mock.Mock()
        refresh = mock.Mock()
        discovery = mock.Mock()
        inspection = mock.Mock()

        result = self.invoke(
            control_preflight=control,
            metadata_refresh=refresh,
            announcement_discovery=discovery,
            candidate_inspection=inspection,
        )

        self.assertEqual(result["status"], "NOT_DUE")
        control.assert_not_called()
        refresh.assert_not_called()
        discovery.assert_not_called()
        inspection.assert_not_called()
        self.assertFalse(self.paths.claim.exists())
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_due_tick_refreshes_before_discovery_then_advances_to_three_hours(self) -> None:
        calls: list[str] = []

        def refresh(**_: object) -> dict:
            calls.append("metadata")
            return {"status": "REFRESH_COMPLETE"}

        def discovery(**_: object) -> dict:
            calls.append("discovery")
            return {
                "status": "NO_MATCHING_ANNOUNCEMENTS",
                "targets": 1,
                "matched_candidates": 0,
                "announcement_requests": 3,
                "appended_candidates": 0,
            }

        def inspection(**_: object) -> dict:
            calls.append("inspection")
            return {
                "status": "NO_SECONDS_GRADE_CANDIDATE",
                "candidates": [],
                "rejections": [],
            }

        result = self.invoke(
            metadata_refresh=refresh,
            announcement_discovery=discovery,
            candidate_inspection=inspection,
        )

        self.assertEqual(calls, ["metadata", "discovery", "inspection"])
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["cadence_stage"], "ACTIVE_UNATTESTED")
        self.assertEqual(result["interval_sec"], 10_800)
        self.assertFalse(result["capture_authorized"])
        self.assertFalse(self.paths.claim.exists())
        self.assertEqual(len(list(self.paths.claim_archive.glob("*.json"))), 1)

    def test_incomplete_metadata_blocks_discovery_and_retries_at_starting_cadence(self) -> None:
        self._seed_state(
            finished_ts=self.NOW - 301,
            stage="EXACT_T0_WITHIN_24H",
            interval_sec=300,
        )
        discovery = mock.Mock()
        inspection = mock.Mock()

        result = self.invoke(
            metadata_refresh=lambda **_: {
                "status": "INCOMPLETE_NO_REGISTRY_WRITE",
                "venue_errors": {"okx": "network"},
            },
            announcement_discovery=discovery,
            candidate_inspection=inspection,
        )

        self.assertEqual(result["status"], "RETRY_NEXT_INTERVAL")
        self.assertEqual(result["cadence_stage"], "EXACT_T0_WITHIN_24H")
        self.assertEqual(result["interval_sec"], 300)
        self.assertTrue(result["pending_retry"])
        discovery.assert_not_called()
        inspection.assert_not_called()

    def test_partial_discovery_retries_at_starting_cadence_without_tight_loop(self) -> None:
        self._seed_state(
            finished_ts=self.NOW - 10_801,
            stage="ACTIVE_UNATTESTED",
            interval_sec=10_800,
        )
        result = self.invoke(
            announcement_discovery=lambda **_: {
                "status": "PARTIAL_RETRY_NEXT_INTERVAL",
                "targets": 1,
                "matched_candidates": 1,
                "announcement_requests": 2,
                "appended_candidates": 1,
                "reason": "one venue failed",
            }
        )

        self.assertEqual(result["status"], "PARTIAL_RETRY_NEXT_INTERVAL")
        self.assertEqual(result["interval_sec"], 10_800)
        self.assertTrue(result["pending_retry"])
        self.assertEqual(result["next_interval_at_utc"], "2027-01-15T11:00:00Z")

    def test_control_preflight_failure_never_creates_claim_or_network_work(self) -> None:
        refresh = mock.Mock()
        discovery = mock.Mock()
        result = self.invoke(
            control_preflight=lambda **_: {"ok": False, "blockers": ["plan"]},
            metadata_refresh=refresh,
            announcement_discovery=discovery,
        )

        self.assertEqual(result["status"], "CONTROL_PREFLIGHT_BLOCKED")
        refresh.assert_not_called()
        discovery.assert_not_called()
        self.assertFalse(self.paths.claim.exists())
        self.assertFalse(self.paths.ledger.exists())

    def test_incomplete_success_control_receipt_fails_closed_before_any_write(self) -> None:
        refresh = mock.Mock()
        result = self.invoke(
            control_preflight=lambda **_: {
                "ok": True,
                "decision": "ALLOW_ANNOUNCEMENT_WATCH_CONTROL",
            },
            metadata_refresh=refresh,
        )

        self.assertEqual(result["status"], "CONTROL_PREFLIGHT_BLOCKED")
        refresh.assert_not_called()
        self.assertFalse(self.paths.claim.exists())
        self.assertFalse(self.paths.ledger.exists())

    def test_interrupted_terminal_is_reconciled_without_network(self) -> None:
        self._terminal_without_state()
        refresh = mock.Mock()
        discovery = mock.Mock()

        result = self.invoke(metadata_refresh=refresh, announcement_discovery=discovery)

        self.assertEqual(result["status"], "CONTROL_STATE_RECONCILED")
        refresh.assert_not_called()
        discovery.assert_not_called()
        self.assertTrue(self.paths.state.is_file())

    def test_interrupted_started_attempt_is_closed_and_deferred_without_network(self) -> None:
        started = self.store.begin_attempt(
            now_ts=self.NOW - 60,
            cadence_stage="ACTIVE_UNATTESTED",
            interval_sec=10_800,
        )
        refresh = mock.Mock()
        discovery = mock.Mock()

        result = self.invoke(metadata_refresh=refresh, announcement_discovery=discovery)

        self.assertEqual(result["status"], "RETRY_NEXT_INTERVAL")
        self.assertTrue(result["pending_retry"])
        self.assertEqual(result["cadence_stage"], "ACTIVE_UNATTESTED")
        self.assertEqual(result["interval_sec"], 10_800)
        refresh.assert_not_called()
        discovery.assert_not_called()
        rows = self.store.verify_ledger()
        self.assertEqual([row["phase"] for row in rows], ["STARTED", "TERMINAL"])
        self.assertEqual(rows[-1]["attempt_id"], started["attempt_id"])
        self.assertEqual(rows[-1]["terminal_status"], "RETRY_NEXT_INTERVAL")
        self.assertTrue(self.paths.state.is_file())

    def test_stale_claim_recovery_defers_without_network(self) -> None:
        self.store.acquire_claim(now_ts=self.NOW - 600)
        refresh = mock.Mock()
        discovery = mock.Mock()
        with mock.patch.object(watch_state, "process_is_alive", return_value=False):
            result = self.invoke(
                metadata_refresh=refresh,
                announcement_discovery=discovery,
            )

        self.assertEqual(result["status"], "RETRY_NEXT_INTERVAL")
        self.assertIn("stale watcher claim archived", result["reason"])
        refresh.assert_not_called()
        discovery.assert_not_called()
        self.assertEqual(len(list(self.paths.claim_archive.glob("*.json"))), 2)

    def test_shared_gate_failure_from_metadata_preflight_is_persisted(self) -> None:
        result = self.invoke(
            metadata_refresh=mock.Mock(
                side_effect=RuntimeError("shared active-run gate is closed")
            )
        )

        self.assertEqual(result["status"], "RETRY_NEXT_INTERVAL")
        self.assertTrue(result["pending_retry"])
        self.assertIn("shared active-run gate is closed", result["reason"])
        self.assertTrue(self.paths.state.is_file())
        rows = self.store.verify_ledger()
        self.assertEqual(rows[-1]["terminal_status"], "RETRY_NEXT_INTERVAL")

    def test_unknown_discovery_status_is_partial_retry_not_success(self) -> None:
        inspection = mock.Mock()
        result = self.invoke(
            announcement_discovery=lambda **_: {
                "status": "ERROR",
                "announcement_requests": 1,
                "appended_candidates": 0,
            },
            candidate_inspection=inspection,
        )

        self.assertEqual(result["status"], "PARTIAL_RETRY_NEXT_INTERVAL")
        self.assertEqual(result["failure_stage"], "announcement_discovery")
        inspection.assert_not_called()
        rows = self.store.verify_ledger()
        self.assertEqual(rows[-1]["failure_stage"], "announcement_discovery")

    def test_candidate_inspection_failure_after_refresh_is_partial_retry(self) -> None:
        result = self.invoke(
            candidate_inspection=lambda **_: {
                "status": "CANDIDATE_INSPECTION_FAILED",
                "reason": "registry read failed",
            }
        )

        self.assertEqual(result["status"], "PARTIAL_RETRY_NEXT_INTERVAL")
        self.assertEqual(result["failure_stage"], "candidate_inspection")

    def test_unconfirmed_alert_continues_to_inspection_and_completes(self) -> None:
        inspection = mock.Mock(
            return_value={
                "status": "NO_SECONDS_GRADE_CANDIDATE",
                "candidates": [],
                "rejections": [],
            }
        )
        result = self.invoke(
            candidate_alert=lambda **_: {
                "status": "CANDIDATE_ALERTS_SUBMITTED_UNCONFIRMED",
                "submitted_alerts": 1,
                "history_confirmed_alerts": 0,
                "alert_ledger_head_hash": "a" * 64,
            },
            candidate_inspection=inspection,
        )

        inspection.assert_called_once()
        self.assertEqual(result["status"], "COMPLETE")
        self.assertFalse(result["pending_retry"])
        self.assertEqual(result["submitted_alerts"], 1)
        self.assertEqual(result["history_confirmed_alerts"], 0)
        self.assertEqual(result["alert_status"], "CANDIDATE_ALERTS_SUBMITTED_UNCONFIRMED")

    def test_network_timestamps_and_next_due_use_fresh_clocks(self) -> None:
        discovery_times: list[int] = []
        inspection_times: list[int] = []
        fresh_times = iter(
            (self.NOW + 30, self.NOW + 31, self.NOW + 32, self.NOW + 40)
        )

        result = self.invoke(
            announcement_discovery=lambda **kwargs: (
                discovery_times.append(int(kwargs["now_ts"]))
                or {
                    "status": "NO_ANNOUNCEMENT_TARGETS",
                    "targets": [],
                    "matched_candidates": 0,
                    "announcement_requests": 0,
                    "appended_candidates": 0,
                }
            ),
            candidate_inspection=lambda **kwargs: (
                inspection_times.append(int(kwargs["now_ts"]))
                or {
                    "status": "NO_SECONDS_GRADE_CANDIDATE",
                    "candidates": [],
                    "rejections": [],
                }
            ),
            clock=lambda: next(fresh_times),
        )

        self.assertEqual(discovery_times, [self.NOW + 30])
        self.assertEqual(inspection_times, [self.NOW + 32])
        expected_next = datetime.fromtimestamp(
            self.NOW + 40 + 21_600, timezone.utc
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        self.assertEqual(result["next_interval_at_utc"], expected_next)


class CliAndWindowsTaskTests(DurableStateTests):
    def test_default_store_uses_active_plan_and_dedicated_control_paths(self) -> None:
        store = scheduler.default_store()
        self.assertIsInstance(store, watch_state.WatchStateStore)
        self.assertEqual(store.plan_id, trust_root.PLAN_ID)
        self.assertEqual(store.plan_hash, trust_root.PLAN_HASH)
        self.assertEqual(store.paths.state, config.ANNOUNCEMENT_STATE_PATH)
        self.assertEqual(store.paths.ledger, config.ANNOUNCEMENT_ATTEMPTS_PATH)
        self.assertEqual(store.paths.claim, config.ANNOUNCEMENT_WATCH_CLAIM_PATH)
        self.assertEqual(store.paths.claim_archive, config.ANNOUNCEMENT_WATCH_CLAIM_ARCHIVE)

    def test_scheduled_cli_is_silent_without_json_and_json_is_opt_in(self) -> None:
        result = {
            "status": "NOT_DUE",
            "next_interval_at_utc": "2027-01-15T14:00:00Z",
            "capture_authorized": False,
        }
        with (
            mock.patch.object(scheduler, "default_store", return_value=self.store),
            mock.patch.object(scheduler, "run_scheduled_tick", return_value=result),
        ):
            quiet = io.StringIO()
            with contextlib.redirect_stdout(quiet):
                quiet_code = scheduler.main(["--scheduled-tick"])
            visible = io.StringIO()
            with contextlib.redirect_stdout(visible):
                json_code = scheduler.main(["--scheduled-tick", "--json"])

        self.assertEqual(quiet_code, 0)
        self.assertEqual(quiet.getvalue(), "")
        self.assertEqual(json_code, 0)
        self.assertEqual(json.loads(visible.getvalue()), result)

    def test_retry_result_is_stderr_visible_and_nonzero_without_disabling_task(self) -> None:
        result = {
            "status": "PARTIAL_RETRY_NEXT_INTERVAL",
            "pending_retry": True,
            "capture_authorized": False,
        }
        with (
            mock.patch.object(scheduler, "default_store", return_value=self.store),
            mock.patch.object(scheduler, "run_scheduled_tick", return_value=result),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = scheduler.main(["--scheduled-tick"])

        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue()), result)

    def test_status_cli_is_read_only(self) -> None:
        with mock.patch.object(scheduler, "default_store", return_value=self.store):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = scheduler.main(["--status", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "DUE")
        self.assertFalse(self.paths.state.exists())
        self.assertFalse(self.paths.ledger.exists())
        self.assertFalse(self.paths.claim.exists())

    def test_launcher_invokes_only_the_new_no_model_scheduler(self) -> None:
        launcher = (
            ROOT / "tools/start_premarket_announcement_watch_scheduler.ps1"
        ).read_text(encoding="utf-8")
        lowered = launcher.lower()
        self.assertIn("announcement_watch_scheduler.py", lowered)
        self.assertIn("--scheduled-tick", lowered)
        self.assertIn("--status", lowered)
        self.assertNotIn("start_premarket_perp_listing_automation_visible", lowered)
        self.assertNotIn("start_premarket_perp_paper_only_visible", lowered)
        self.assertNotIn("start-process", lowered)

    def test_launcher_pins_bundled_python_without_environment_or_path_fallback(self) -> None:
        launcher = (
            ROOT / "tools/start_premarket_announcement_watch_scheduler.ps1"
        ).read_text(encoding="utf-8").lower()
        self.assertIn(
            r"c:\users\koval\.cache\codex-runtimes\codex-primary-runtime"
            r"\dependencies\python\python.exe",
            launcher,
        )
        self.assertNotIn("premarket_announcement_python", launcher)
        self.assertNotIn("get-command python", launcher)

    def test_installer_registers_one_hidden_five_minute_no_model_task(self) -> None:
        installer = (
            ROOT / "tools/install_premarket_announcement_watch_scheduler.ps1"
        ).read_text(encoding="utf-8")
        lowered = installer.lower()
        self.assertIn("premarketannouncementwatch", lowered)
        self.assertIn("\\zolotyaylopata\\", lowered)
        self.assertIn("register-scheduledtask", lowered)
        self.assertIn("-repetitioninterval ([timespan]::fromminutes(5))", lowered)
        self.assertIn("-multipleinstances ignorenew", lowered)
        self.assertIn("-windowstyle hidden", lowered)
        self.assertIn("-noninteractive", lowered)
        self.assertIn("start_premarket_announcement_watch_scheduler.ps1", lowered)
        self.assertNotIn("unregister-scheduledtask", lowered)
        self.assertNotIn("codex exec", lowered)
        self.assertIn("schedule.service", lowered)
        self.assertIn("createfolder('zolotyaylopata')", lowered)
        self.assertIn("0x80070002", lowered)
        self.assertIn("ensure-scheduledtaskfolder -path $taskpath", lowered)
        self.assertLess(
            lowered.index("ensure-scheduledtaskfolder -path $taskpath"),
            lowered.index("register-scheduledtask"),
        )
        self.assertIn(
            r"$pwsh = 'c:\program files\powershell\7\pwsh.exe'",
            lowered,
        )
        self.assertNotIn("get-command pwsh", lowered)

    @unittest.skipUnless(
        Path("C:/Program Files/PowerShell/7/pwsh.exe").is_file(),
        "PowerShell 7 is unavailable",
    )
    def test_windows_status_entrypoints_execute_and_return_json(self) -> None:
        pwsh = "C:/Program Files/PowerShell/7/pwsh.exe"
        commands = (
            [
                pwsh,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "tools/start_premarket_announcement_watch_scheduler.ps1"),
                "-Status",
                "-Json",
            ],
            [
                pwsh,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "tools/install_premarket_announcement_watch_scheduler.ps1"),
                "-Status",
                "-Json",
            ],
        )
        for command in commands:
            with self.subTest(script=command[6]):
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIsInstance(json.loads(completed.stdout), dict)


class ControlPlaneAuthorizationTests(unittest.TestCase):
    def test_watch_control_is_local_only_and_does_not_require_shared_gate(self) -> None:
        policy = config.WRITE_CLASSES["announcement_watch_control"]
        self.assertIs(policy["exclusive_writer_claim"], False)
        self.assertIs(policy["capture_token"], False)
        self.assertIs(policy["endpoint_allow_list"], False)
        self.assertIs(policy["shared_gate_required"], False)
        self.assertIs(policy["plan_and_capability_scan"], True)

    def test_new_status_authorizes_control_but_never_market_capture(self) -> None:
        status = risk_gate.ANNOUNCEMENT_WATCH_PLAN_STATUS
        authorization = risk_gate.PLAN_WRITE_AUTHORIZATION[status]
        self.assertIn("announcement_watch_control", authorization["write_classes"])
        self.assertNotIn("market_data_capture", authorization["write_classes"])
        self.assertIn(
            risk_gate.ANNOUNCEMENT_WATCH_CONTROL_ACTION,
            authorization["authorized_actions"],
        )

    def test_control_preflight_does_not_consult_shared_gate(self) -> None:
        status = risk_gate.ANNOUNCEMENT_WATCH_PLAN_STATUS
        authorization = risk_gate.PLAN_WRITE_AUTHORIZATION[status]
        plan = {
            "status": status,
            "plan_id": "premarket_perp_capture_20260822_v34",
            "plan_hash": "a" * 64,
            "authorized_after_gate_green": sorted(authorization["authorized_actions"]),
            "resolved_path_bindings": risk_gate.resolved_path_bindings(),
        }
        with (
            mock.patch.object(risk_gate, "load_and_verify_plan", return_value=plan),
            mock.patch.object(
                risk_gate,
                "run_capability_scan",
                return_value={"status": "CAPABILITY_SCAN_CLEAN"},
            ),
            mock.patch.object(risk_gate, "read_shared_gate") as shared_gate,
        ):
            result = risk_gate.preflight(
                write_class="announcement_watch_control",
                run_id="watch-control-test",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"], "ALLOW_ANNOUNCEMENT_WATCH_CONTROL")
        shared_gate.assert_not_called()

    def test_control_receipt_validator_rejects_incomplete_or_wrong_authority(self) -> None:
        with self.assertRaises(risk_gate.RiskGateError):
            risk_gate.validate_control_preflight_receipt(
                {"ok": True}, run_id="watch-control-test"
            )

        receipt = ScheduledTickTests.good_control_preflight(
            run_id="watch-control-test"
        )
        receipt["resolved_paths_hash"] = "0" * 64
        with self.assertRaises(risk_gate.RiskGateError):
            risk_gate.validate_control_preflight_receipt(
                receipt, run_id="watch-control-test"
            )

    def test_control_artifact_paths_are_versioned_for_future_plan_rollover(self) -> None:
        for path in (
            config.ANNOUNCEMENT_STATE_PATH,
            config.ANNOUNCEMENT_ATTEMPTS_PATH,
            config.ANNOUNCEMENT_WATCH_CLAIM_PATH,
            config.ANNOUNCEMENT_WATCH_CLAIM_ARCHIVE,
        ):
            self.assertIn("v36", path.name)


class V33ImmutablePlanTests(unittest.TestCase):
    V32_RELATIVE = "docs/plans/premarket-perp-capture-planonly-20260822-v32.json"
    V32_PATH = ROOT / V32_RELATIVE
    V32_ID = "premarket_perp_capture_20260822_v32"
    V32_HASH = "15b84b04cf004834909950846837df9ccef29bb8209e56d7ca58a2b1419e784d"
    V32_FILE_SHA = "d0c4a3625ff9a526166694db67d329e3d1e650fdfcbf4c60e36553983175018d"
    V33_RELATIVE = "docs/plans/premarket-perp-capture-planonly-20260822-v33.json"
    V33_PATH = ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v33.json"
    V33_ID = "premarket_perp_capture_20260822_v33"
    V33_HASH = "9db73dc2e15ec266472d0cf0693f5db935f26e6b6dd3633885214e8cc965980e"
    V33_FILE_SHA = "55bab73391016340ef07d705503e13ab5cd94f5677555c5dda3b6b85766ea89d"

    def test_v32_is_preserved_byte_identical(self) -> None:
        self.assertEqual(
            hashlib.sha256(self.V32_PATH.read_bytes()).hexdigest(),
            self.V32_FILE_SHA,
        )
        payload = json.loads(self.V32_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload["plan_id"], self.V32_ID)
        self.assertEqual(payload["plan_hash"], self.V32_HASH)

    def test_v33_has_a_new_identity_and_exact_v32_lineage(self) -> None:
        self.assertTrue(self.V33_PATH.is_file())
        self.assertEqual(config.V32_PLAN_PATH, self.V32_PATH)
        self.assertEqual(config.V33_PLAN_PATH, self.V33_PATH)
        self.assertEqual(
            hashlib.sha256(self.V33_PATH.read_bytes()).hexdigest(),
            self.V33_FILE_SHA,
        )

        payload = json.loads(self.V33_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], "premarket_perp_capture_planonly_v33")
        self.assertEqual(payload["plan_id"], self.V33_ID)
        self.assertEqual(payload["plan_hash"], self.V33_HASH)
        self.assertEqual(payload["supersedes_plan_hash"], self.V32_HASH)
        self.assertEqual(payload["status"], risk_gate.ANNOUNCEMENT_WATCH_PLAN_STATUS)

    def test_v33_preregisters_the_quiet_adaptive_scheduler_exactly(self) -> None:
        payload = json.loads(self.V33_PATH.read_text(encoding="utf-8"))
        contract = payload["announcement_watch_scheduler"]
        self.assertEqual(contract["scheduler"], "WINDOWS_TASK_SCHEDULER_NO_MODEL")
        self.assertEqual(contract["wake_interval_sec"], 300)
        self.assertEqual(
            contract["cadence_interval_sec"],
            {
                "SEARCH": 21_600,
                "ACTIVE_UNATTESTED": 10_800,
                "UNVERIFIED_CANDIDATE": 10_800,
                "OFFICIAL_CONFIRMED": 3_600,
                "EXACT_T0_WITHIN_24H": 300,
            },
        )
        self.assertEqual(
            contract["not_due"],
            {
                "network": False,
                "writes": False,
                "preflight": False,
                "claim": False,
                "stdout": "NONE_UNLESS_JSON_REQUESTED",
            },
        )
        self.assertEqual(
            contract["due_order"],
            [
                "announcement_watch_control_preflight",
                "dedicated_watch_claim",
                "attempt_started_ledger",
                "metadata_registry_refresh",
                "announcement_discovery",
                "candidate_inspection",
                "attempt_terminal_ledger",
                "atomic_state",
                "claim_archive_release",
            ],
        )
        self.assertEqual(contract["proxy_or_unverified_min_interval_sec"], 10_800)
        self.assertEqual(
            contract["control_path_rollover"],
            "NEW_PLAN_VERSION_REQUIRES_NEW_VERSIONED_CONTROL_PATHS",
        )
        self.assertEqual(
            contract["control_preflight_failure"],
            "FAIL_CLOSED_NO_WRITE_RECHECK_ON_NEXT_FIVE_MINUTE_WAKE",
        )
        self.assertIs(contract["capture_authorized"], False)
        self.assertIs(contract["global_market_data_claim_used"], False)

    def test_v33_and_v32_are_retired_byte_identical(self) -> None:
        retired = {item["path"]: item for item in trust_root.RETIRED_PLANS}
        self.assertEqual(retired[self.V32_RELATIVE]["plan_hash"], self.V32_HASH)
        self.assertEqual(
            retired[self.V32_RELATIVE]["plan_file_sha256"], self.V32_FILE_SHA
        )
        self.assertEqual(retired[self.V33_RELATIVE]["plan_hash"], self.V33_HASH)
        self.assertEqual(
            retired[self.V33_RELATIVE]["plan_file_sha256"], self.V33_FILE_SHA
        )


class V34ImmutablePlanTests(unittest.TestCase):
    V33_RELATIVE = "docs/plans/premarket-perp-capture-planonly-20260822-v33.json"
    V33_PATH = ROOT / V33_RELATIVE
    V33_ID = "premarket_perp_capture_20260822_v33"
    V33_HASH = "9db73dc2e15ec266472d0cf0693f5db935f26e6b6dd3633885214e8cc965980e"
    V33_FILE_SHA = "55bab73391016340ef07d705503e13ab5cd94f5677555c5dda3b6b85766ea89d"
    V34_PATH = ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v34.json"

    def test_v34_supersedes_exact_immutable_v33(self) -> None:
        self.assertEqual(config.V33_PLAN_PATH, self.V33_PATH)
        self.assertEqual(config.V34_PLAN_PATH, self.V34_PATH)
        self.assertEqual(
            hashlib.sha256(self.V33_PATH.read_bytes()).hexdigest(),
            self.V33_FILE_SHA,
        )
        payload = json.loads(self.V34_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], "premarket_perp_capture_planonly_v34")
        self.assertEqual(payload["plan_id"], "premarket_perp_capture_20260822_v34")
        self.assertEqual(payload["supersedes_plan_id"], self.V33_ID)
        self.assertEqual(payload["supersedes_plan_hash"], self.V33_HASH)
        self.assertEqual(payload["supersedes_plan_path"], self.V33_RELATIVE)
        self.assertEqual(payload["status"], risk_gate.ANNOUNCEMENT_WATCH_PLAN_STATUS)

    def test_v34_preregisters_versioned_control_paths_and_fresh_host_install(self) -> None:
        payload = json.loads(self.V34_PATH.read_text(encoding="utf-8"))
        contract = payload["announcement_watch_scheduler"]
        self.assertIn("v34", Path(contract["state_path"]).name)
        self.assertIn("v34", Path(contract["attempt_ledger_path"]).name)
        self.assertIn("v34", Path(contract["claim_path"]).name)
        self.assertIn("v34", Path(contract["claim_archive"]).name)
        installer = (
            ROOT / "tools/install_premarket_announcement_watch_scheduler.ps1"
        ).read_text(encoding="utf-8").lower()
        self.assertIn("0x80070002", installer)
        bound = {
            item["role"]: item for item in payload["implementation"]["files"]
        }
        self.assertEqual(
            bound["announcement_watch_installer"]["sha256"],
            hashlib.sha256(
                (ROOT / "tools/install_premarket_announcement_watch_scheduler.ps1").read_bytes()
            ).hexdigest(),
        )

    def test_v34_and_v33_are_retired_byte_identical(self) -> None:
        payload = json.loads(self.V34_PATH.read_text(encoding="utf-8"))
        retired = {item["path"]: item for item in trust_root.RETIRED_PLANS}
        self.assertEqual(retired[self.V33_RELATIVE]["plan_hash"], self.V33_HASH)
        self.assertEqual(
            retired[self.V33_RELATIVE]["plan_file_sha256"], self.V33_FILE_SHA
        )
        v34_relative = "docs/plans/premarket-perp-capture-planonly-20260822-v34.json"
        self.assertEqual(retired[v34_relative]["plan_hash"], payload["plan_hash"])
        self.assertEqual(
            retired[v34_relative]["plan_file_sha256"],
            hashlib.sha256(self.V34_PATH.read_bytes()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
