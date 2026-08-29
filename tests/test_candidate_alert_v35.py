"""RED contract for the quiet, at-most-once v35 candidate alert path."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import announcement_candidate_store as candidate_store  # noqa: E402
import announcement_discovery as discovery  # noqa: E402
import announcement_watch_scheduler as scheduler  # noqa: E402
import event_registry as registry  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import project_config as config  # noqa: E402


def _load_alert_module():
    spec = importlib.util.find_spec("candidate_alert")
    if spec is None:
        raise AssertionError("src/candidate_alert.py is required by PlanOnly v35")
    return importlib.import_module("candidate_alert")


def target() -> dict[str, object]:
    return {
        "episode_id": "episode-abc",
        "perpetual_venue": "bybit",
        "premarket_contract_id": "ABCUSDT",
        "lifecycle_generation": 0,
        "asset_class": registry.ASSET_CLASS_CRYPTO_TOKEN,
        "issuer_namespace": "crypto_asset",
        "issuer_id": "ABC",
        "asset_identity_hash": "a" * 64,
        "registry_sha256": "b" * 64,
        "registry_tail_record_hash": "c" * 64,
        "mutation_receipt_seq": 4,
        "mutation_receipt_hash": "d" * 64,
        "summary_content_hash": "e" * 64,
        "registry_authority_state_hash": "f" * 64,
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
        "metadata_refresh_received_at": "2033-05-18T03:33:20Z",
    }


def candidate() -> dict[str, object]:
    return discovery.make_candidate(
        target=target(),
        listing_venue="kucoin",
        article={
            "article_id": "abc-listing",
            "title": "ABC (ABC) Gets Listed on KuCoin!",
            "url": "https://www.kucoin.com/announcement/en-abc-gets-listed",
            "published_at_ms": None,
            "source_page": 1,
            "source_payload_sha256": "2" * 64,
        },
        detected_at_utc="2033-05-18T03:33:20Z",
    )


class CandidateAlertStoreTests(unittest.TestCase):
    NOW = 2_000_000_000

    def setUp(self) -> None:
        self.alert = _load_alert_module()
        self.root = Path(tempfile.mkdtemp())
        self.candidates = self.root / "candidates.jsonl"
        self.alerts = self.root / "alerts.jsonl"

    @staticmethod
    def allow_preflight(**kwargs: object) -> dict[str, object]:
        return {
            "schema": "premarket_write_preflight_v2",
            "ok": True,
            "verified": True,
            "decision": "ALLOW_CANDIDATE_ALERT",
            "write_class": "candidate_alert",
            "run_id": kwargs["run_id"],
            "action": "submit one local candidate notification after alert preflight",
            "plan_id": trust_root.PLAN_ID,
            "plan_hash": trust_root.PLAN_HASH,
            "resolved_paths_hash": "9" * 64,
        }

    @staticmethod
    def targets(**_: object) -> dict[str, object]:
        return {
            "status": "TARGETS_READY",
            "targets": [target()],
            "capture_authorized": False,
        }

    def invoke(self, notifier) -> dict[str, object]:
        return self.alert.process_candidate_alerts(
            now_ts=self.NOW,
            run_id="watch-run-1",
            candidate_store_path=self.candidates,
            alert_ledger_path=self.alerts,
            target_selector=self.targets,
            preflight=self.allow_preflight,
            notifier=notifier,
        )

    def test_candidate_store_has_a_public_verified_read_surface(self) -> None:
        self.assertTrue(
            callable(getattr(candidate_store, "load_verified_candidate_records", None))
        )

    def test_review_status_exposes_copyable_operator_identity_without_writes(self) -> None:
        seeded = candidate()
        candidate_store.append_candidates(self.candidates, [seeded], run_id="seed")
        self.assertFalse(self.alerts.exists())

        result = self.alert.inspect_candidate_review_queue(
            now_ts=self.NOW,
            candidate_store_path=self.candidates,
            alert_ledger_path=self.alerts,
            target_selector=self.targets,
        )

        self.assertEqual(result["status"], "CANDIDATES_READY_FOR_HUMAN_REVIEW")
        self.assertEqual(result["count"], 1)
        review = result["candidates"][0]
        self.assertEqual(review["candidate_id"], seeded["candidate_id"])
        self.assertEqual(review["episode_id"], "episode-abc")
        self.assertEqual(review["lifecycle_generation"], 0)
        self.assertEqual(review["premarket_contract_id"], "ABCUSDT")
        self.assertEqual(review["listing_venue"], "kucoin")
        self.assertEqual(
            review["article_url"],
            "https://www.kucoin.com/announcement/en-abc-gets-listed",
        )
        self.assertIsNone(review["alert_phase"])
        self.assertIs(result["capture_authorized"], False)
        self.assertFalse(self.alerts.exists())

    def test_review_status_cli_is_read_only_and_json_copyable(self) -> None:
        seeded = candidate()
        candidate_store.append_candidates(self.candidates, [seeded], run_id="seed")
        stdout = io.StringIO()
        with mock.patch.object(
            self.alert.config, "ANNOUNCEMENT_CANDIDATE_PATH", self.candidates
        ), mock.patch.object(
            self.alert.config, "CANDIDATE_ALERT_LEDGER_PATH", self.alerts
        ), mock.patch(
            "event_registry.select_unattested_crypto_premarket_episodes",
            side_effect=self.targets,
        ), mock.patch("sys.stdout", stdout):
            code = self.alert.main(
                ["--review-status", "--now-ts", str(self.NOW), "--json"]
            )

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["candidates"][0]["episode_id"], "episode-abc")
        self.assertEqual(payload["candidates"][0]["lifecycle_generation"], 0)
        self.assertFalse(self.alerts.exists())

    def test_first_revision_submits_once_and_duplicate_revision_never_resubmits(self) -> None:
        first = candidate()
        candidate_store.append_candidates(self.candidates, [first], run_id="seed-1")
        seen: list[dict[str, object]] = []

        def notifier(payload: dict[str, object]) -> dict[str, object]:
            seen.append(dict(payload))
            return {
                "schema": "premarket_candidate_alert_toast_result_v1",
                "status": "WINDOWS_HISTORY_CONFIRMED",
                "notification_id": payload["notification_id"],
                "show_invoked": True,
                "tag": payload["tag"],
                "group": "ZLP-PREMARKET",
            }

        one = self.invoke(notifier)
        two = self.invoke(notifier)
        changed = dict(first, article_title="ABC listing article updated")
        candidate_store.append_candidates(self.candidates, [changed], run_id="seed-2")
        three = self.invoke(notifier)

        self.assertEqual(one["status"], "CANDIDATE_ALERTS_HISTORY_CONFIRMED")
        self.assertEqual(two["status"], "NO_NEW_CANDIDATE_ALERTS")
        self.assertEqual(three["status"], "NO_NEW_CANDIDATE_ALERTS")
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0]["capture_authorized"], False)
        self.assertNotIn("official_spot_t0", seen[0])
        self.assertEqual(seen[0]["article_url"], first["article_url"])

    def test_crash_after_submission_intent_is_never_automatically_resent(self) -> None:
        first = candidate()
        candidate_store.append_candidates(self.candidates, [first], run_id="seed-1")
        calls = 0

        def uncertain(_payload: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            raise TimeoutError("toast sidecar result was not observable")

        one = self.invoke(uncertain)
        two = self.invoke(uncertain)
        records = [json.loads(line) for line in self.alerts.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(one["status"], "DELIVERY_UNCERTAIN_NO_AUTO_RETRY")
        self.assertEqual(two["status"], "NO_NEW_CANDIDATE_ALERTS")
        self.assertEqual(calls, 1)
        self.assertEqual(records[-1]["phase"], "DELIVERY_UNCERTAIN_NO_AUTO_RETRY")
        self.assertIs(records[-1]["capture_authorized"], False)

    def test_blocked_preflight_writes_no_intent_and_retries_next_interval(self) -> None:
        candidate_store.append_candidates(self.candidates, [candidate()], run_id="seed")
        calls = 0

        def notifier(_payload: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {}

        result = self.alert.process_candidate_alerts(
            now_ts=self.NOW,
            run_id="watch-run-1",
            candidate_store_path=self.candidates,
            alert_ledger_path=self.alerts,
            target_selector=self.targets,
            preflight=lambda **_: {"ok": False, "decision": "BLOCK"},
            notifier=notifier,
        )

        self.assertEqual(result["status"], "CANDIDATE_ALERT_RETRY_NEXT_INTERVAL")
        self.assertEqual(calls, 0)
        self.assertFalse(self.alerts.exists())

    def test_terminal_or_stale_episode_is_not_alerted(self) -> None:
        candidate_store.append_candidates(self.candidates, [candidate()], run_id="seed")
        called = False

        def notifier(_payload: dict[str, object]) -> dict[str, object]:
            nonlocal called
            called = True
            return {}

        result = self.alert.process_candidate_alerts(
            now_ts=self.NOW,
            run_id="watch-run-1",
            candidate_store_path=self.candidates,
            alert_ledger_path=self.alerts,
            target_selector=lambda **_: {
                "status": "NO_ANNOUNCEMENT_TARGETS",
                "targets": [],
                "capture_authorized": False,
            },
            preflight=self.allow_preflight,
            notifier=notifier,
        )

        self.assertEqual(result["status"], "NO_NEW_CANDIDATE_ALERTS")
        self.assertFalse(called)
        self.assertFalse(self.alerts.exists())

    def test_dead_same_host_lock_is_archived_and_alert_processing_resumes(self) -> None:
        candidate_store.append_candidates(self.candidates, [candidate()], run_id="seed")
        lock_path = Path(str(self.alerts) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps({
                "schema": "premarket_candidate_alert_lock_v1",
                "owner_pid": 999_999_999,
                "owner_host": socket.gethostname(),
                "run_id": "dead-run",
                "nonce": "1" * 32,
            }) + "\n",
            encoding="utf-8",
        )
        calls = 0

        def notifier(payload: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {
                "schema": "premarket_candidate_alert_toast_result_v1",
                "status": "WINDOWS_HISTORY_CONFIRMED",
                "notification_id": payload["notification_id"],
                "show_invoked": True,
                "tag": payload["tag"],
                "group": "ZLP-PREMARKET",
            }

        with mock.patch.object(self.alert, "process_is_alive", return_value=False):
            result = self.invoke(notifier)

        self.assertEqual(result["status"], "CANDIDATE_ALERTS_HISTORY_CONFIRMED")
        self.assertEqual(calls, 1)
        self.assertFalse(lock_path.exists())
        self.assertEqual(len(list(Path(str(lock_path) + ".archive").glob("*.json"))), 1)

    def test_submitted_unconfirmed_is_nonretry_and_reports_scheduler_counts(self) -> None:
        candidate_store.append_candidates(self.candidates, [candidate()], run_id="seed")

        def notifier(payload: dict[str, object]) -> dict[str, object]:
            return {
                "schema": "premarket_candidate_alert_toast_result_v1",
                "status": "WINDOWS_SUBMITTED_UNCONFIRMED",
                "notification_id": payload["notification_id"],
                "show_invoked": True,
                "tag": payload["tag"],
                "group": "ZLP-PREMARKET",
            }

        result = self.invoke(notifier)

        self.assertEqual(result["status"], "CANDIDATE_ALERTS_SUBMITTED_UNCONFIRMED")
        self.assertEqual(result["submitted_alerts"], 1)
        self.assertEqual(result["history_confirmed_alerts"], 0)
        self.assertIn(result["status"], scheduler.ALERT_SUCCESS_STATUSES)

    def test_alert_rechecks_preflight_inside_lock_and_never_shows_on_drift(self) -> None:
        candidate_store.append_candidates(self.candidates, [candidate()], run_id="seed")
        preflight_calls = 0
        notifier_called = False

        def drifting_preflight(**kwargs: object) -> dict[str, object]:
            nonlocal preflight_calls
            preflight_calls += 1
            receipt = dict(self.allow_preflight(**kwargs))
            if preflight_calls == 2:
                receipt["resolved_paths_hash"] = "8" * 64
            return receipt

        def notifier(_payload: dict[str, object]) -> dict[str, object]:
            nonlocal notifier_called
            notifier_called = True
            return {}

        result = self.alert.process_candidate_alerts(
            now_ts=self.NOW,
            run_id="watch-run-1",
            candidate_store_path=self.candidates,
            alert_ledger_path=self.alerts,
            target_selector=self.targets,
            preflight=drifting_preflight,
            notifier=notifier,
        )

        self.assertEqual(preflight_calls, 2)
        self.assertEqual(result["status"], "CANDIDATE_ALERT_RETRY_NEXT_INTERVAL")
        self.assertFalse(notifier_called)
        self.assertFalse(self.alerts.exists())


class SchedulerAlertIntegrationTests(unittest.TestCase):
    def test_production_wrapper_supplies_every_alert_dependency(self) -> None:
        with mock.patch("candidate_alert.process_candidate_alerts") as process:
            process.return_value = {"status": "NO_NEW_CANDIDATE_ALERTS"}
            scheduler._candidate_alert(now_ts=2_000_000_000, run_id="watch-run")

        kwargs = process.call_args.kwargs
        self.assertEqual(kwargs["candidate_store_path"], config.ANNOUNCEMENT_CANDIDATE_PATH)
        self.assertEqual(kwargs["alert_ledger_path"], config.CANDIDATE_ALERT_LEDGER_PATH)
        self.assertTrue(callable(kwargs["target_selector"]))
        self.assertTrue(callable(kwargs["preflight"]))

    def test_scheduler_has_a_lazy_candidate_alert_dependency(self) -> None:
        parameters = inspect.signature(scheduler.run_scheduled_tick).parameters
        self.assertIn("candidate_alert", parameters)
        source = (SRC / "announcement_watch_scheduler.py").read_text(encoding="utf-8")
        self.assertIn("def _candidate_alert", source)
        self.assertNotIn("import candidate_alert\n", source.split("def _candidate_alert", 1)[0])

    def test_not_due_contract_still_precedes_the_alert_runtime_import(self) -> None:
        source = (SRC / "announcement_watch_scheduler.py").read_text(encoding="utf-8")
        probe = source.index("store.probe_due")
        self.assertIn("candidate_alert(", source[source.index("def run_scheduled_tick") :])
        alert_call = source.index("candidate_alert(", source.index("def run_scheduled_tick"))
        self.assertLess(probe, alert_call)


class WindowsToastSidecarTests(unittest.TestCase):
    def test_sidecar_is_bound_and_uses_system_windows_powershell(self) -> None:
        alert = _load_alert_module()
        script = ROOT / "tools/show_premarket_candidate_alert.ps1"
        self.assertTrue(script.is_file())
        self.assertEqual(
            Path(config.WINDOWS_POWERSHELL_EXECUTABLE),
            Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"),
        )
        self.assertEqual(
            dict(config.BOUND_RUNTIME_FILES)["candidate_alert"],
            "src/candidate_alert.py",
        )
        self.assertEqual(
            dict(config.BOUND_RUNTIME_FILES)["candidate_alert_sidecar"],
            "tools/show_premarket_candidate_alert.ps1",
        )
        self.assertEqual(alert.ALERT_GROUP, "ZLP-PREMARKET")


if __name__ == "__main__":
    unittest.main()
