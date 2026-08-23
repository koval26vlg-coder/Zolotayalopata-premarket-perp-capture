"""Offline regressions for lifecycle state, refresh CAS, and fixture isolation.

No test in this module opens a socket or writes to the project registry.  Supplied
venue payloads are permitted only at explicit temporary paths.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
for search_path in (SRC, TESTS):
    if str(search_path) not in sys.path:
        sys.path.insert(0, str(search_path))

import event_registry as registry  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import official_attestation as attestation  # noqa: E402
import project_config as config  # noqa: E402
import risk_gate  # noqa: E402
import test_activation_hardening_v6_registry as v6  # noqa: E402
import test_activation_hardening_v7_registry as v7  # noqa: E402


def _preflight(run_id: str) -> dict:
    return {
        "schema": risk_gate.PREFLIGHT_RESULT_SCHEMA,
        "ok": True,
        "verified": True,
        "decision": "ALLOW_METADATA_REGISTRY",
        "write_class": "metadata_registry",
        "run_id": run_id,
        "action": risk_gate.METADATA_REGISTRY_ACTION,
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
        "resolved_paths_hash": "9" * 64,
    }


def _payloads(
    *,
    present: bool,
    with_t0: bool = True,
    bybit_status: str = "PreLaunch",
    bybit_is_prelisting: bool = True,
) -> dict:
    bybit_rows: list[dict] = []
    if present:
        row = {
            "symbol": v6.CONTRACT_ID,
            "symbolType": "innovation",
            "status": (
                "PreLaunch"
                if bybit_status == "PreLaunch" and bybit_is_prelisting
                else "Trading"
            ),
            "isPreListing": bool(
                bybit_status == "PreLaunch" and bybit_is_prelisting
            ),
            "contractType": "LinearPerpetual",
        }
        if with_t0:
            row["launchTime"] = str((v6._t0_ts() - 8 * 24 * 3600) * 1000)
        bybit_rows.append(row)
    return {
        "bybit": {
            "prelaunch": {
                "retCode": 0,
                "result": {
                    "category": "linear",
                    "list": [
                        row for row in bybit_rows if row["status"] == "PreLaunch"
                    ],
                    "nextPageCursor": "",
                },
            },
            "trading": {
                "retCode": 0,
                "result": {
                    "category": "linear",
                    "list": [
                        row for row in bybit_rows if row["status"] == "Trading"
                    ],
                    "nextPageCursor": "",
                },
            },
        },
        "okx": {
            "code": "0",
            "data": [
                {
                    "instId": "FILL-USDT-250101",
                    "instType": "FUTURES",
                    "ruleType": "normal",
                    "state": "live",
                    "listTime": "1700000000000",
                }
            ],
        },
        "gate": [
            {
                "name": "FILL_USDT",
                "status": "trading",
                "is_pre_market": False,
                "create_time": 1_700_000_000,
            }
        ],
    }


def _refresh(
    path: Path,
    *,
    run_id: str,
    present: bool,
    with_t0: bool = True,
    bybit_status: str = "PreLaunch",
    bybit_is_prelisting: bool = True,
) -> dict:
    return registry.refresh(
        payloads=_payloads(
            present=present,
            with_t0=with_t0,
            bybit_status=bybit_status,
            bybit_is_prelisting=bybit_is_prelisting,
        ),
        path=path,
        observed_at_utc="2027-01-08T04:00:00Z",
        run_id=run_id,
    )


class ExactLifecycleStateTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.path = root / "registry.jsonl"

    def test_active_generation_and_high_water_are_sealed_in_summary_and_receipt(self) -> None:
        with mock.patch.object(
            registry.risk_gate,
            "preflight",
            side_effect=lambda *, write_class, run_id: _preflight(run_id),
        ):
            first = _refresh(self.path, run_id="present-generation-0", present=True)
            terminal = _refresh(
                self.path,
                run_id="terminal-generation-0",
                present=True,
                bybit_status="Closed",
                bybit_is_prelisting=False,
            )
            reappeared = _refresh(
                self.path,
                run_id="reappeared-generation-1-no-t0",
                present=True,
                with_t0=False,
            )

        active_field = registry.ACTIVE_LIFECYCLE_GENERATIONS_FIELD
        high_water_field = registry.LIFECYCLE_GENERATION_HIGH_WATER_FIELD
        self.assertEqual(first[active_field]["bybit"], {v6.CONTRACT_ID: 0})
        self.assertEqual(first[high_water_field]["bybit"], {v6.CONTRACT_ID: 0})
        self.assertEqual(terminal[active_field]["bybit"], {})
        self.assertEqual(terminal[high_water_field]["bybit"], {v6.CONTRACT_ID: 0})
        self.assertEqual(reappeared[active_field]["bybit"], {v6.CONTRACT_ID: 1})
        self.assertEqual(reappeared[high_water_field]["bybit"], {v6.CONTRACT_ID: 1})
        self.assertEqual(reappeared[registry.ACTIVE_CONTRACTS_FIELD]["bybit"], [v6.CONTRACT_ID])
        # The relisted row has no usable launch timestamp, but its lifecycle still
        # advances durably. The second row is the append-only terminal observation;
        # no timestamp is fabricated for the relisted generation.
        records = registry.load_registry(self.path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1]["timestamp_kind"], registry.TIMESTAMP_TRANSITION)
        self.assertEqual(
            records[-1]["lifecycle_phase"],
            registry.LIFECYCLE_TRANSITIONED_STANDARD,
        )

        receipts, problems = registry._load_mutation_receipt_chain(self.path)
        self.assertEqual(problems, [])
        self.assertEqual(len(receipts), 3)
        self.assertEqual(receipts[-1][active_field], reappeared[active_field])
        self.assertEqual(receipts[-1][high_water_field], reappeared[high_water_field])
        self.assertEqual(registry.verify_registry(self.path)["status"], "REGISTRY_OK")

    def test_old_generation_attestation_is_ineligible_after_relist_without_t0(self) -> None:
        with mock.patch.object(
            registry.risk_gate,
            "preflight",
            side_effect=lambda *, write_class, run_id: _preflight(run_id),
        ):
            _refresh(self.path, run_id="old-present", present=True)
            _refresh(
                self.path,
                run_id="old-terminal",
                present=True,
                bybit_status="Closed",
                bybit_is_prelisting=False,
            )
            _refresh(
                self.path,
                run_id="new-present-no-t0",
                present=True,
                with_t0=False,
            )

        attestation_preflight = v7._valid_attestation_preflight("old-generation-attest")
        with mock.patch.object(
            attestation.risk_gate, "preflight", return_value=attestation_preflight
        ), mock.patch.object(
            attestation.time,
            "time",
            return_value=v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC,
        ):
            with self.assertRaisesRegex(
                attestation.AttestationError, r"current active lifecycle generation"
            ):
                attestation.attest(
                    path=self.path,
                    run_id="old-generation-attest",
                    venue="bybit",
                    spot_symbol=v6.SPOT_SYMBOL,
                    premarket_contract_id=v6.CONTRACT_ID,
                    lifecycle_generation=0,
                    announced_utc=v6.ANNOUNCED_UTC,
                    announcement_url=v6.SOURCE_URL,
                    quoted_sentence=v6.QUOTE,
                    quoted_time_text=v6.QUOTED_TIME,
                    quoted_symbol_text=v6.QUOTED_SYMBOL,
                    attested_by="registry-v9-test",
                )

    def test_old_official_episode_is_not_selected_after_disappearance(self) -> None:
        records = v6._records()
        v6._write_records(self.path, records)
        v6._write_refresh_summary(self.path, records)
        summary_path = self.path.with_suffix(".summary.json")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary[registry.ACTIVE_CONTRACTS_FIELD]["bybit"] = []
        summary[registry.ACTIVE_LIFECYCLE_GENERATIONS_FIELD]["bybit"] = {}
        summary[registry.LIFECYCLE_GENERATION_HIGH_WATER_FIELD]["bybit"] = {
            v6.CONTRACT_ID: 0
        }
        # Commit the disappearance as a real immutable registry mutation.
        registry._write_summary_with_mutation_receipt(
            self.path,
            dict(
                summary,
                mutation_type="metadata_refresh",
                mutation_run_id="disappeared-v9",
                refresh_run_id="disappeared-v9",
            ),
            lock_owner=None,
        )

        with mock.patch.object(registry, "REGISTRY_PATH", self.path):
            selected = registry.events_for_capture(
                now_ts=v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC,
                source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            )
        self.assertEqual(selected, [])

    def test_capture_candidate_binds_latest_mutation_receipt_authority(self) -> None:
        records = v6._records()
        v6._write_records(self.path, records)
        v6._write_refresh_summary(self.path, records)
        summary = json.loads(
            self.path.with_suffix(".summary.json").read_text(encoding="utf-8")
        )
        registry._write_summary_with_mutation_receipt(
            self.path,
            dict(
                summary,
                mutation_type="metadata_refresh",
                mutation_run_id="no-append-refresh-v9",
                refresh_run_id="no-append-refresh-v9",
            ),
            lock_owner=None,
        )
        receipts, problems = registry._load_mutation_receipt_chain(self.path)
        self.assertEqual(problems, [])
        latest = receipts[-1]

        with mock.patch.object(registry, "REGISTRY_PATH", self.path):
            selected = registry.events_for_capture(
                now_ts=v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC,
                source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            )
        self.assertEqual(len(selected), 1)
        candidate = selected[0]
        self.assertEqual(candidate["mutation_receipt_seq"], latest["mutation_seq"])
        self.assertEqual(candidate["mutation_receipt_hash"], latest["receipt_hash"])
        self.assertEqual(
            candidate["summary_content_sha256"], latest["summary_content_hash"]
        )
        self.assertRegex(candidate["registry_authority_state_hash"], r"^[0-9a-f]{64}$")


class FreshnessAndWriterClockTests(unittest.TestCase):
    def test_due_selection_rejects_a_stale_complete_metadata_refresh(self) -> None:
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        records = v6._records()
        v6._write_records(path, records)
        v6._write_refresh_summary(path, records)
        summary = json.loads(path.with_suffix(".summary.json").read_text(encoding="utf-8"))
        summary[registry.LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD] = (
            "2027-01-14T00:00:00Z"
        )
        registry._write_summary_with_mutation_receipt(
            path,
            dict(
                summary,
                mutation_type="metadata_refresh",
                mutation_run_id="stale-metadata-v9",
                refresh_run_id="stale-metadata-v9",
            ),
            lock_owner=None,
        )

        with mock.patch.object(registry, "REGISTRY_PATH", path):
            with self.assertRaisesRegex(
                registry.EventRegistryError, r"STALE_METADATA_REFRESH"
            ):
                registry.events_for_capture(
                    now_ts=v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC,
                    source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
                )

    def test_attestation_rechecks_writer_clock_after_lock_acquisition(self) -> None:
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        initial = registry.build_stream_revisions([], [v6._metadata_observation()])
        v6._write_records(path, initial)
        v6._write_refresh_summary(path, initial)
        threshold = v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC
        summary = json.loads(path.with_suffix(".summary.json").read_text(encoding="utf-8"))
        summary[registry.LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD] = (
            "2027-01-15T03:30:00Z"
        )
        registry._write_summary_with_mutation_receipt(
            path,
            dict(
                summary,
                mutation_type="metadata_refresh",
                mutation_run_id="clock-jump-refresh-v9",
                refresh_run_id="clock-jump-refresh-v9",
            ),
            lock_owner=None,
        )
        clocks = iter([threshold, threshold + 1])

        with mock.patch.object(
            attestation.risk_gate,
            "preflight",
            return_value=v7._valid_attestation_preflight("clock-jump-v9"),
        ), mock.patch.object(
            attestation.time, "time", side_effect=lambda: next(clocks)
        ), mock.patch.object(attestation.registry, "append_entries") as append:
            with self.assertRaisesRegex(
                attestation.AttestationError, r"could not cover|away"
            ):
                attestation.attest(
                    path=path,
                    run_id="clock-jump-v9",
                    venue="bybit",
                    spot_symbol=v6.SPOT_SYMBOL,
                    premarket_contract_id=v6.CONTRACT_ID,
                    lifecycle_generation=0,
                    announced_utc=v6.ANNOUNCED_UTC,
                    announcement_url=v6.SOURCE_URL,
                    quoted_sentence=v6.QUOTE,
                    quoted_time_text=v6.QUOTED_TIME,
                    quoted_symbol_text=v6.QUOTED_SYMBOL,
                    attested_by="registry-v9-test",
                )
        append.assert_not_called()
        self.assertEqual(len(registry.load_registry(path)), 1)

    def test_new_attestation_rejects_a_day_old_metadata_refresh(self) -> None:
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        initial = registry.build_stream_revisions([], [v6._metadata_observation()])
        v6._write_records(path, initial)
        v6._write_refresh_summary(path, initial)
        received = registry._parse_explicit_utc(v6.RECEIVED_AT)
        assert received is not None
        writer_now = int(received.timestamp()) + 24 * 3600

        with mock.patch.object(
            attestation.risk_gate,
            "preflight",
            return_value=v7._valid_attestation_preflight("stale-attest-v9"),
        ), mock.patch.object(
            attestation.time, "time", return_value=writer_now
        ), mock.patch.object(attestation.registry, "append_entries") as append:
            with self.assertRaisesRegex(
                attestation.AttestationError, r"STALE_METADATA_REFRESH"
            ):
                attestation.attest(
                    path=path,
                    run_id="stale-attest-v9",
                    venue="bybit",
                    spot_symbol=v6.SPOT_SYMBOL,
                    premarket_contract_id=v6.CONTRACT_ID,
                    lifecycle_generation=0,
                    announced_utc=v6.ANNOUNCED_UTC,
                    announcement_url=v6.SOURCE_URL,
                    quoted_sentence=v6.QUOTE,
                    quoted_time_text=v6.QUOTED_TIME,
                    quoted_symbol_text=v6.QUOTED_SYMBOL,
                    attested_by="registry-v9-test",
                )
        append.assert_not_called()

    def test_attestation_rechecks_gate_under_lock_before_append(self) -> None:
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        initial = registry.build_stream_revisions([], [v6._metadata_observation()])
        v6._write_records(path, initial)
        v6._write_refresh_summary(path, initial)
        receipts = iter(
            [
                v7._valid_attestation_preflight("attest-gate-change-v9"),
                RuntimeError("shared gate closed"),
            ]
        )

        def changing_preflight(**_kwargs):
            result = next(receipts)
            if isinstance(result, BaseException):
                raise result
            return result

        with mock.patch.object(
            attestation.risk_gate, "preflight", side_effect=changing_preflight
        ), mock.patch.object(
            attestation.time, "time", return_value=registry._parse_explicit_utc(
                v6.RECEIVED_AT
            ).timestamp(),
        ), mock.patch.object(attestation.registry, "append_entries") as append:
            with self.assertRaisesRegex(
                attestation.AttestationError, r"PREFLIGHT_BLOCKED_AT_COMMIT"
            ):
                attestation.attest(
                    path=path,
                    run_id="attest-gate-change-v9",
                    venue="bybit",
                    spot_symbol=v6.SPOT_SYMBOL,
                    premarket_contract_id=v6.CONTRACT_ID,
                    lifecycle_generation=0,
                    announced_utc=v6.ANNOUNCED_UTC,
                    announcement_url=v6.SOURCE_URL,
                    quoted_sentence=v6.QUOTE,
                    quoted_time_text=v6.QUOTED_TIME,
                    quoted_symbol_text=v6.QUOTED_SYMBOL,
                    attested_by="registry-v9-test",
                )
        append.assert_not_called()


class OkxTerminalLifecycleTests(unittest.TestCase):
    CONTRACT = "KII-USDT-250115"
    SPOT = "KII-USDT"

    @classmethod
    def _payloads(cls, *, rule_type: str, state: str) -> dict:
        return {
            "bybit": {
                "retCode": 0,
                "result": {
                    "category": "linear",
                    "list": [],
                    "nextPageCursor": "",
                },
            },
            "okx": {
                "code": "0",
                "data": [
                    {
                        "instId": cls.CONTRACT,
                        # The phases are different instrument types on OKX: a
                        # pre-market perpetual is a SWAP, and the dated contract it
                        # transitions into is a FUTURES.
                        "instType": "SWAP" if rule_type == "pre_market" else "FUTURES",
                        "ruleType": rule_type,
                        "state": state,
                        "instCategory": "1",
                        "listTime": str((v6._t0_ts() - 8 * 24 * 3600) * 1000),
                        "preMktSwTime": str(v6._t0_ts() * 1000),
                    }
                ],
            },
            "gate": [
                {
                    "name": "FILL_USDT",
                    "status": "trading",
                    "is_pre_market": False,
                    "create_time": 1_700_000_000,
                }
            ],
        }

    def test_first_seen_xperp_cannot_allocate_a_local_generation(self) -> None:
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        with mock.patch.object(
            registry.risk_gate,
            "preflight",
            side_effect=lambda *, write_class, run_id: _preflight(run_id),
        ):
            result = registry.refresh(
                payloads=self._payloads(rule_type="xperp", state="live"),
                path=path,
                observed_at_utc="2027-01-15T03:30:00Z",
                run_id="okx-untracked-xperp-v17",
            )

        self.assertEqual(
            result[registry.ACTIVE_LIFECYCLE_GENERATIONS_FIELD]["okx"], {}
        )
        self.assertEqual(
            result[registry.LIFECYCLE_GENERATION_HIGH_WATER_FIELD]["okx"], {}
        )
        self.assertEqual(result["appended_entries"], 0)
        self.assertFalse(
            any(
                item["premarket_contract_id"] == self.CONTRACT
                for item in registry.materialize_episodes(
                    registry.load_registry(path)
                )
            )
        )

    def test_xperp_transition_is_terminal_and_cannot_remain_capture_active(self) -> None:
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        with mock.patch.object(
            registry.risk_gate,
            "preflight",
            side_effect=lambda *, write_class, run_id: _preflight(run_id),
        ):
            first = registry.refresh(
                payloads=self._payloads(rule_type="pre_market", state="live"),
                path=path,
                observed_at_utc="2027-01-08T04:00:00Z",
                run_id="okx-active-v9",
            )
        self.assertEqual(
            first[registry.ACTIVE_LIFECYCLE_GENERATIONS_FIELD]["okx"],
            {self.CONTRACT: 0},
        )

        official_preflight = v7._valid_attestation_preflight("okx-attest-v9")
        official_preflight = dict(official_preflight, run_id="okx-attest-v9")
        with mock.patch.object(
            attestation.risk_gate, "preflight", return_value=official_preflight
        ), mock.patch.object(
            attestation.time,
            "time",
            return_value=v6._t0_ts() - 7 * 24 * 3600,
        ):
            attestation.attest(
                path=path,
                run_id="okx-attest-v9",
                venue="okx",
                spot_symbol=self.SPOT,
                premarket_contract_id=self.CONTRACT,
                lifecycle_generation=0,
                announced_utc=v6.ANNOUNCED_UTC,
                announcement_url="https://www.okx.com/help/kii-listing",
                quoted_sentence=v6.QUOTE,
                quoted_time_text=v6.QUOTED_TIME,
                quoted_symbol_text=v6.QUOTED_SYMBOL,
                attested_by="registry-v9-test",
            )

        with mock.patch.object(
            registry.risk_gate,
            "preflight",
            side_effect=lambda *, write_class, run_id: _preflight(run_id),
        ):
            terminal = registry.refresh(
                payloads=self._payloads(rule_type="xperp", state="live"),
                path=path,
                observed_at_utc="2027-01-15T03:30:00Z",
                run_id="okx-terminal-v9",
            )

        self.assertEqual(
            terminal[registry.ACTIVE_LIFECYCLE_GENERATIONS_FIELD]["okx"], {}
        )
        self.assertEqual(
            terminal[registry.LIFECYCLE_GENERATION_HIGH_WATER_FIELD]["okx"],
            {self.CONTRACT: 0},
        )
        episode = next(
            item
            for item in registry.materialize_episodes(registry.load_registry(path))
            if item["premarket_contract_id"] == self.CONTRACT
        )
        self.assertEqual(episode["transition_ts"], v6._t0_ts())

        with mock.patch.object(registry, "REGISTRY_PATH", path):
            self.assertEqual(
                registry.events_for_capture(
                    now_ts=v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC,
                    source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
                ),
                [],
            )

        retry_preflight = dict(official_preflight, run_id="okx-terminal-retry-v9")
        with mock.patch.object(
            attestation.risk_gate, "preflight", return_value=retry_preflight
        ), mock.patch.object(
            attestation.time,
            "time",
            return_value=v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC,
        ):
            with self.assertRaisesRegex(
                attestation.AttestationError, r"current active lifecycle generation"
            ):
                attestation.attest(
                    path=path,
                    run_id="okx-terminal-retry-v9",
                    venue="okx",
                    spot_symbol=self.SPOT,
                    premarket_contract_id=self.CONTRACT,
                    lifecycle_generation=0,
                    announced_utc=v6.ANNOUNCED_UTC,
                    announcement_url="https://www.okx.com/help/kii-listing",
                    quoted_sentence=v6.QUOTE,
                    quoted_time_text=v6.QUOTED_TIME,
                    quoted_symbol_text=v6.QUOTED_SYMBOL,
                    attested_by="registry-v9-test",
                )


class RefreshCompareAndSwapTests(unittest.TestCase):
    def test_stale_staged_refresh_is_rejected_after_newer_commit(self) -> None:
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        staged_a = threading.Event()
        release_a = threading.Event()
        errors: list[BaseException] = []

        class BlockingPayloads(dict):
            def __getitem__(self, key):  # noqa: ANN001
                value = super().__getitem__(key)
                if key == "gate":
                    staged_a.set()
                    if not release_a.wait(timeout=5):
                        raise AssertionError("timed out waiting for the newer commit")
                return value

        def run_a() -> None:
            try:
                registry.refresh(
                    payloads=BlockingPayloads(_payloads(present=True)),
                    path=path,
                    observed_at_utc="2027-01-08T04:00:00Z",
                    run_id="stale-stage-a",
                )
            except BaseException as exc:  # expected assertion target
                errors.append(exc)

        with mock.patch.object(
            registry.risk_gate,
            "preflight",
            side_effect=lambda *, write_class, run_id: _preflight(run_id),
        ):
            worker = threading.Thread(target=run_a, daemon=True)
            worker.start()
            self.assertTrue(staged_a.wait(timeout=5))
            committed_b = registry.refresh(
                payloads=_payloads(present=False),
                path=path,
                observed_at_utc="2027-01-08T04:01:00Z",
                run_id="newer-stage-b",
            )
            release_a.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(committed_b["refresh_run_id"], "newer-stage-b")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], registry.EventRegistryError)
        self.assertRegex(str(errors[0]), r"STALE_REFRESH_STAGE|compare-and-swap")
        summary = json.loads(path.with_suffix(".summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["refresh_run_id"], "newer-stage-b")
        self.assertEqual(len(registry._load_mutation_receipt_chain(path)[0]), 1)

    def test_refresh_rechecks_gate_under_lock_before_append(self) -> None:
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        calls = 0

        def changing_preflight(*, write_class: str, run_id: str) -> dict:
            nonlocal calls
            calls += 1
            if calls == 1:
                return _preflight(run_id)
            raise RuntimeError("shared gate closed")

        with mock.patch.object(
            registry.risk_gate, "preflight", side_effect=changing_preflight
        ), mock.patch.object(registry, "append_entries") as append:
            with self.assertRaisesRegex(
                registry.EventRegistryError, r"PREFLIGHT_BLOCKED_AT_COMMIT"
            ):
                registry.refresh(
                    payloads=_payloads(present=True),
                    path=path,
                    observed_at_utc="2027-01-08T04:00:00Z",
                    run_id="refresh-gate-change-v9",
                )
        append.assert_not_called()
        self.assertFalse(path.exists())


class SyntheticRefreshIsolationTests(unittest.TestCase):
    def test_injected_payloads_cannot_target_implicit_or_explicit_production_path(self) -> None:
        root = Path(tempfile.mkdtemp())
        production = root / "listing-events-v2.jsonl"
        lock = root / "listing-events-v2.lock"
        fake = _payloads(present=True)
        with mock.patch.object(registry, "REGISTRY_PATH", production), mock.patch.object(
            registry, "REGISTRY_LOCK_PATH", lock
        ), mock.patch.object(
            registry.risk_gate,
            "preflight",
            side_effect=lambda *, write_class, run_id: _preflight(run_id),
        ), mock.patch.object(registry, "append_entries") as append:
            for name, path in (("implicit", None), ("explicit", production)):
                with self.subTest(name=name):
                    with self.assertRaisesRegex(
                        registry.EventRegistryError,
                        r"INJECTED_PAYLOADS_FORBIDDEN_FOR_PRODUCTION",
                    ):
                        registry.refresh(
                            payloads=fake,
                            path=path,
                            observed_at_utc="2027-01-08T04:00:00Z",
                            run_id=f"fixture-{name}",
                        )
        append.assert_not_called()
        self.assertFalse(production.exists())
        self.assertFalse(production.with_suffix(".summary.json").exists())


class ProductionUniverseCompletenessTests(unittest.TestCase):
    @staticmethod
    def _venue_payload(
        url: str, *, count: int, request_params: dict[str, str] | None = None
    ) -> object:
        if "bybit.com" in url:
            status = str((request_params or {}).get("status") or "")
            rows = []
            if status == "Trading":
                rows = [
                    {
                        "symbol": f"FILL{i}USDT",
                        "baseCoin": f"FILL{i}",
                        "contractType": "LinearPerpetual",
                        "status": "Trading",
                        "isPreListing": False,
                        "launchTime": "1700000000000",
                    }
                    for i in range(count)
                ]
            return {
                "retCode": 0,
                "result": {
                    "category": "linear",
                    "list": rows,
                    "nextPageCursor": "",
                },
            }
        if "okx.com" in url:
            inst_type = str((request_params or {}).get("instType") or "FUTURES")
            return {
                "code": "0",
                "data": [
                    {
                        "instId": f"FILL{i}-USDT-250101",
                        "instType": inst_type,
                        "ruleType": "normal",
                        "state": "live",
                        "listTime": "1700000000000",
                    }
                    for i in range(count)
                ],
            }
        return [
            {
                "name": f"FILL{i}_USDT",
                "status": "trading",
                "is_pre_market": False,
                "create_time": 1_700_000_000,
            }
            for i in range(count)
        ]

    def test_empty_okx_and_gate_universes_are_incomplete_without_mutation(self) -> None:
        root = Path(tempfile.mkdtemp())
        production = root / "listing-events-v2.jsonl"
        lock = root / "listing-events-v2.lock"
        with mock.patch.object(registry, "REGISTRY_PATH", production), mock.patch.object(
            registry, "REGISTRY_LOCK_PATH", lock
        ), mock.patch.object(
            registry.risk_gate,
            "preflight",
            return_value=_preflight("empty-universe-v9"),
        ), mock.patch.object(
            registry.public_http,
            "get_json",
            side_effect=lambda url, **kwargs: self._venue_payload(
                url, count=0, request_params=kwargs.get("params")
            ),
        ), mock.patch.object(
            registry, "_writer_refresh_completed_at_utc", return_value="2027-01-08T04:00:00Z"
        ):
            result = registry.refresh(run_id="empty-universe-v9")

        self.assertEqual(result["status"], "INCOMPLETE_NO_REGISTRY_WRITE")
        self.assertIn("okx", result["venue_errors"])
        self.assertIn("gate", result["venue_errors"])
        self.assertFalse(production.exists())

    def test_abrupt_full_universe_drop_cannot_terminalize_active_state(self) -> None:
        root = Path(tempfile.mkdtemp())
        production = root / "listing-events-v2.jsonl"
        lock = root / "listing-events-v2.lock"
        phase = {"count": 10}

        def fetch(url: str, **kwargs):
            return self._venue_payload(
                url,
                count=phase["count"],
                request_params=kwargs.get("params"),
            )

        with mock.patch.object(registry, "REGISTRY_PATH", production), mock.patch.object(
            registry, "REGISTRY_LOCK_PATH", lock
        ), mock.patch.object(
            registry.risk_gate,
            "preflight",
            side_effect=lambda *, write_class, run_id: _preflight(run_id),
        ), mock.patch.object(
            registry.public_http, "get_json", side_effect=fetch
        ), mock.patch.object(
            registry,
            "_writer_refresh_completed_at_utc",
            side_effect=["2027-01-08T04:00:00Z", "2027-01-08T04:01:00Z"],
        ):
            first = registry.refresh(run_id="full-universe-v9")
            registry_before = production.read_bytes() if production.is_file() else None
            summary_before = production.with_suffix(".summary.json").read_bytes()
            receipts_before = [
                item.read_bytes()
                for item in sorted(registry._mutation_receipt_dir(production).glob("*.json"))
            ]
            phase["count"] = 1
            with self.assertRaisesRegex(
                registry.EventRegistryError, r"INCOMPLETE_UNIVERSE_DROP"
            ):
                registry.refresh(run_id="partial-universe-v9")

        self.assertEqual(first[registry.RAW_UNIVERSE_ROWS_FIELD]["okx"], 20)
        self.assertEqual(
            production.read_bytes() if production.is_file() else None,
            registry_before,
        )
        self.assertEqual(
            production.with_suffix(".summary.json").read_bytes(), summary_before
        )
        self.assertEqual(
            [
                item.read_bytes()
                for item in sorted(registry._mutation_receipt_dir(production).glob("*.json"))
            ],
            receipts_before,
        )

    def test_bybit_trading_surface_drop_cannot_commit(self) -> None:
        root = Path(tempfile.mkdtemp())
        production = root / "listing-events-v3.jsonl"
        lock = root / "listing-events-v3.lock"
        phase = {"bybit_trading_count": 10}

        def fetch(url: str, **kwargs):
            count = (
                phase["bybit_trading_count"] if "bybit.com" in url else 10
            )
            return self._venue_payload(
                url,
                count=count,
                request_params=kwargs.get("params"),
            )

        with mock.patch.object(registry, "REGISTRY_PATH", production), mock.patch.object(
            registry, "REGISTRY_LOCK_PATH", lock
        ), mock.patch.object(
            registry.risk_gate,
            "preflight",
            side_effect=lambda *, write_class, run_id: _preflight(run_id),
        ), mock.patch.object(
            registry.public_http, "get_json", side_effect=fetch
        ), mock.patch.object(
            registry,
            "_writer_refresh_completed_at_utc",
            side_effect=["2027-01-08T04:00:00Z", "2027-01-08T04:01:00Z"],
        ):
            first = registry.refresh(run_id="bybit-full-surface-v17")
            summary_before = production.with_suffix(".summary.json").read_bytes()
            receipts_before = [
                item.read_bytes()
                for item in sorted(
                    registry._mutation_receipt_dir(production).glob("*.json")
                )
            ]
            phase["bybit_trading_count"] = 1
            with self.assertRaisesRegex(
                registry.EventRegistryError, r"bybit_linear_trading"
            ):
                registry.refresh(run_id="bybit-partial-surface-v17")

        self.assertEqual(first[registry.RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD][
            "bybit_linear_trading"
        ], 10)
        self.assertEqual(
            production.with_suffix(".summary.json").read_bytes(), summary_before
        )
        self.assertEqual(
            [
                item.read_bytes()
                for item in sorted(
                    registry._mutation_receipt_dir(production).glob("*.json")
                )
            ],
            receipts_before,
        )

    def test_offline_fixture_is_explicitly_non_production_evidence(self) -> None:
        path = Path(tempfile.mkdtemp()) / "fixture-registry.jsonl"
        with mock.patch.object(
            registry.risk_gate,
            "preflight",
            side_effect=lambda *, write_class, run_id: _preflight(run_id),
        ):
            summary = registry.refresh(
                payloads=_payloads(present=True),
                path=path,
                observed_at_utc="2027-01-08T04:00:00Z",
                run_id="synthetic-offline-v9",
            )
        self.assertEqual(
            summary["refresh_evidence_class"], "SYNTHETIC_OFFLINE_FIXTURE_ONLY"
        )
        self.assertFalse(summary["production_eligible"])

    def test_live_fetch_cannot_redirect_to_a_non_production_path(self) -> None:
        path = Path(tempfile.mkdtemp()) / "redirected-live.jsonl"
        with mock.patch.object(
            registry.risk_gate,
            "preflight",
            return_value=_preflight("redirected-live-v9"),
        ), mock.patch.object(
            registry.public_http,
            "get_json",
            side_effect=AssertionError("network must not be reached"),
        ) as network:
            with self.assertRaisesRegex(
                registry.EventRegistryError, r"LIVE_REFRESH_REQUIRES_CANONICAL_PRODUCTION_PATH"
            ):
                registry.refresh(
                    path=path,
                    observed_at_utc="2027-01-08T04:00:00Z",
                    run_id="redirected-live-v9",
                )
        network.assert_not_called()
        self.assertFalse(path.exists())

    def test_live_refresh_rejects_caller_owned_observed_at_before_fetch(self) -> None:
        root = Path(tempfile.mkdtemp())
        production = root / "listing-events-v2.jsonl"
        lock = root / "listing-events-v2.lock"
        with mock.patch.object(registry, "REGISTRY_PATH", production), mock.patch.object(
            registry, "REGISTRY_LOCK_PATH", lock
        ), mock.patch.object(
            registry.risk_gate,
            "preflight",
            return_value=_preflight("caller-clock-v9"),
        ), mock.patch.object(
            registry.public_http,
            "get_json",
            side_effect=AssertionError("network must not be reached"),
        ) as network:
            with self.assertRaisesRegex(
                registry.EventRegistryError,
                r"LIVE_REFRESH_OBSERVED_AT_OVERRIDE_FORBIDDEN",
            ):
                registry.refresh(
                    observed_at_utc="2099-01-01T00:00:00Z",
                    run_id="caller-clock-v9",
                )
        network.assert_not_called()
        self.assertFalse(production.exists())


if __name__ == "__main__":
    unittest.main()
