"""RED contract for the second official-registry activation hardening pass.

Only temporary files and supplied public-metadata fixtures are used.  The tests pin
causal provenance, pre-append validation, the real OKX FUTURES identifier shape,
capture-window eligibility, exact preflight lineage, and lifecycle generations.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


TESTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(TESTS_ROOT))

import event_registry as registry  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import official_attestation as attestation  # noqa: E402
import project_config as config  # noqa: E402
import risk_gate  # noqa: E402
import test_activation_hardening_v6_registry as v6  # noqa: E402


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _write_registry(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


@contextmanager
def _production_registry(records: list[dict], *, refresh_at: str | None = None):
    root = Path(tempfile.mkdtemp())
    path = root / "listing-events-v2.jsonl"
    lock_path = root / "listing-events-v2.lock"
    _write_registry(path, records)
    v6._write_refresh_summary(
        path,
        records,
        last_complete_metadata_refresh_received_at=refresh_at,
    )
    with mock.patch.object(registry, "REGISTRY_PATH", path), mock.patch.object(
        registry, "REGISTRY_LOCK_PATH", lock_path
    ):
        yield path


def _valid_attestation_preflight(run_id: str) -> dict:
    return {
        "schema": risk_gate.PREFLIGHT_RESULT_SCHEMA,
        "ok": True,
        "verified": True,
        "decision": "ALLOW_OFFICIAL_ATTESTATION",
        "write_class": "official_attestation",
        "run_id": run_id,
        "action": risk_gate.OFFICIAL_ATTESTATION_ACTION,
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
        "resolved_paths_hash": "c" * 64,
    }


def _attest_kwargs(*, run_id: str, attested_by: str = "registry-v7-test") -> dict:
    return {
        "run_id": run_id,
        "venue": "bybit",
        "spot_symbol": v6.SPOT_SYMBOL,
        "premarket_contract_id": v6.CONTRACT_ID,
        "lifecycle_generation": v6.GENERATION,
        "announced_utc": v6.ANNOUNCED_UTC,
        "announcement_url": v6.SOURCE_URL,
        "quoted_sentence": v6.QUOTE,
        "quoted_time_text": v6.QUOTED_TIME,
        "quoted_symbol_text": v6.QUOTED_SYMBOL,
        "attested_by": attested_by,
    }


class CausalOfficialProvenanceTests(unittest.TestCase):
    def _report_for(self, official: dict) -> dict:
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        records = v6._records(official=official)
        _write_registry(path, records)
        return registry.verify_registry(path)

    def test_received_at_after_official_t0_is_not_acceptance_grade(self) -> None:
        official = v6._official_observation(
            received_at_utc=_iso(v6._t0_ts() + 3600)
        )

        report = self._report_for(official)

        self.assertEqual(report["status"], "REGISTRY_PROBLEMS")
        self.assertRegex("; ".join(report["problems"]), r"received|causal|lead")

    def test_attested_lead_must_equal_t0_minus_received_at(self) -> None:
        official = v6._official_observation()
        official["attestation"] = dict(
            official["attestation"], lead_sec_at_attestation=-1
        )

        report = self._report_for(official)

        self.assertEqual(report["status"], "REGISTRY_PROBLEMS")
        self.assertRegex("; ".join(report["problems"]), r"lead|received")

    def test_event_is_not_selectable_before_the_attestation_was_received(self) -> None:
        received_ts = v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC
        official = v6._official_observation(received_at_utc=_iso(received_ts))
        official["attestation"] = dict(
            official["attestation"],
            lead_sec_at_attestation=config.CAPTURE_WINDOW_BEFORE_SEC,
        )
        records = v6._records(official=official)

        with _production_registry(records, refresh_at=_iso(received_ts - 1)):
            selected = registry.events_for_capture(
                now_ts=received_ts - 1,
                source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            )

        self.assertEqual(selected, [])

    def test_public_attest_cannot_accept_a_caller_supplied_historical_clock(self) -> None:
        parameters = __import__("inspect").signature(attestation.attest).parameters

        self.assertNotIn(
            "now_ts",
            parameters,
            "the production write API must take receipt time from its own UTC clock",
        )


class ValidateBeforeAppendTests(unittest.TestCase):
    def test_whitespace_author_cannot_be_fsynced_before_semantic_validation(self) -> None:
        root = Path(tempfile.mkdtemp())
        path = root / "registry.jsonl"
        initial = registry.build_stream_revisions([], [v6._metadata_observation()])
        _write_registry(path, initial)
        before = path.read_bytes()
        real_append = registry.append_entries
        receipt = _valid_attestation_preflight("preappend-v7")

        with mock.patch.object(
            attestation.risk_gate, "preflight", return_value=receipt
        ), mock.patch.object(
            attestation.registry, "append_entries", wraps=real_append
        ) as append:
            with self.assertRaisesRegex(
                attestation.AttestationError, r"author|attested_by|semantic|invalid"
            ):
                attestation.attest(
                    path=path,
                    **_attest_kwargs(
                        run_id="preappend-v7", attested_by=" registry-v7-test "
                    ),
                )

        append.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(registry.verify_registry(path)["status"], "REGISTRY_OK")


class OkxDatedFuturesMappingTests(unittest.TestCase):
    def test_dated_okx_futures_contract_maps_to_its_spot_market(self) -> None:
        contract_id = "NEW-USDT-250101"
        spot_symbol = "NEW-USDT"
        episode_id = registry.make_episode_id("okx", contract_id, 0)
        metadata = registry.make_timestamp_observation(
            episode_id=episode_id,
            venue="okx",
            premarket_contract_id=contract_id,
            spot_symbol=None,
            timestamp_kind=registry.TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
            timestamp_ts=v6._t0_ts() - 8 * 24 * 3600,
            instrument_role="premarket_perp",
            source_class=registry.SOURCE_VENUE_INSTRUMENT_METADATA,
            source_identity="okx:instrument_metadata:listTime",
            source_url="https://www.okx.com/api/v5/public/instruments",
            received_at_utc=v6.RECEIVED_AT,
            lifecycle_generation=0,
        )
        official = registry.make_timestamp_observation(
            episode_id=episode_id,
            venue="okx",
            premarket_contract_id=contract_id,
            spot_symbol=spot_symbol,
            timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            timestamp_ts=v6._t0_ts(),
            instrument_role="spot",
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="human_attestation:registry-v7-test",
            source_url="https://www.okx.com/help/new-listing",
            received_at_utc=v6.RECEIVED_AT,
            precision_sec=60,
            caveats=("OFFICIAL_T0_READ_BY_A_PERSON_FROM_ANNOUNCEMENT_PROSE",),
            lifecycle_generation=0,
        )
        official["attestation"] = {
            "schema": attestation.ATTESTATION_SCHEMA,
            "attested_by": "registry-v7-test",
            "announced_utc": v6.ANNOUNCED_UTC,
            "quoted_sentence": (
                f"Spot trading for NEW/USDT will start on {v6.QUOTED_TIME}."
            ),
            "quoted_time_text": v6.QUOTED_TIME,
            "quoted_symbol_text": "NEW/USDT",
            "announcement_url": "https://www.okx.com/help/new-listing",
            "lead_sec_at_attestation": 7 * 24 * 3600,
        }
        records = registry.build_stream_revisions([], [metadata, official])
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        _write_registry(path, records)

        report = registry.verify_registry(path)

        self.assertEqual(report["status"], "REGISTRY_OK", report["problems"])


class NearLaunchEligibilityTests(unittest.TestCase):
    def test_verified_event_is_not_due_hours_before_its_capture_window(self) -> None:
        records = v6._records()

        with _production_registry(
            records, refresh_at=_iso(v6._t0_ts() - 6 * 3600)
        ):
            selected = registry.events_for_capture(
                now_ts=v6._t0_ts() - 6 * 3600,
                source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            )

        self.assertEqual(selected, [])

    def test_verified_event_becomes_due_at_the_capture_window_boundary(self) -> None:
        records = v6._records()

        with _production_registry(records):
            selected = registry.events_for_capture(
                now_ts=v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC,
                source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            )

        self.assertEqual(len(selected), 1)


class ExactAttestationPreflightTests(unittest.TestCase):
    def _assert_blocked_before_append(self, receipt: dict, *, name: str) -> None:
        root = Path(tempfile.mkdtemp())
        path = root / f"{name}.jsonl"
        initial = registry.build_stream_revisions([], [v6._metadata_observation()])
        _write_registry(path, initial)
        with mock.patch.object(
            attestation.risk_gate, "preflight", return_value=receipt
        ), mock.patch.object(
            attestation.registry, "append_entries", return_value=0
        ) as append:
            with self.assertRaisesRegex(attestation.AttestationError, "PREFLIGHT_BLOCKED"):
                attestation.attest(
                    path=path,
                    **_attest_kwargs(run_id=str(receipt["run_id"])),
                )
        append.assert_not_called()

    def test_schema_and_current_trust_root_are_exact_preconditions(self) -> None:
        run_id = "preflight-v7"
        valid = _valid_attestation_preflight(run_id)
        wrong_hash = ("0" if trust_root.PLAN_HASH[0] != "0" else "1") * 64
        cases = {
            "missing_schema": {key: value for key, value in valid.items() if key != "schema"},
            "wrong_schema": dict(valid, schema="forged_preflight"),
            "wrong_plan_id": dict(valid, plan_id=trust_root.PLAN_ID + "-forged"),
            "wrong_plan_hash": dict(valid, plan_hash=wrong_hash),
        }
        for name, receipt in cases.items():
            with self.subTest(name=name):
                self._assert_blocked_before_append(receipt, name=name)


class PersistentLifecycleGenerationTests(unittest.TestCase):
    @staticmethod
    def _payloads(*, present: bool) -> dict:
        bybit_row = {
            "symbol": "RELUSDT",
            "status": "PreLaunch" if present else "Trading",
            "isPreListing": bool(present),
            "contractType": "LinearPerpetual",
            "launchTime": str(v6._t0_ts() * 1000),
        }
        prelaunch_rows = [bybit_row] if present else []
        trading_rows = [] if present else [bybit_row]
        return {
            "bybit": {
                "prelaunch": {
                    "retCode": 0,
                    "result": {
                        "category": "linear",
                        "list": prelaunch_rows,
                        "nextPageCursor": "",
                    },
                },
                "trading": {
                    "retCode": 0,
                    "result": {
                        "category": "linear",
                        "list": trading_rows,
                        "nextPageCursor": "",
                    },
                },
            },
            "okx": {
                "code": "0",
                "data": [{
                    "instId": "FILL-USDT-250101",
                    "instType": "FUTURES",
                    "ruleType": "normal",
                    "state": "live",
                    "listTime": "1700000000000",
                }],
            },
            "gate": [{
                "name": "FILL_USDT",
                "status": "trading",
                "is_pre_market": False,
                "create_time": 1_700_000_000,
            }],
        }

    def test_explicit_terminal_then_reappear_allocates_a_new_episode_generation(self) -> None:
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"

        def preflight(*, write_class: str, run_id: str) -> dict:
            self.assertEqual(write_class, "metadata_registry")
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
                "resolved_paths_hash": "d" * 64,
            }

        ticks = (
            ("generation-present-0", True, "2027-01-01T00:00:00Z"),
            ("generation-absent", False, "2027-01-02T00:00:00Z"),
            ("generation-present-1", True, "2027-01-03T00:00:00Z"),
        )
        with mock.patch.object(registry.risk_gate, "preflight", side_effect=preflight):
            for run_id, present, observed_at in ticks:
                result = registry.refresh(
                    payloads=self._payloads(present=present),
                    path=path,
                    observed_at_utc=observed_at,
                    run_id=run_id,
                )
                self.assertEqual(result["status"], "REFRESH_COMPLETE")

        episodes = [
            episode
            for episode in registry.materialize_episodes(registry.load_registry(path))
            if episode["venue"] == "bybit"
            and episode["premarket_contract_id"] == "RELUSDT"
        ]
        self.assertEqual(len(episodes), 2)
        self.assertEqual(
            sorted(episode["lifecycle_generation"] for episode in episodes), [0, 1]
        )
        self.assertEqual(len({episode["episode_id"] for episode in episodes}), 2)


class SecondaryLineageRegressions(unittest.TestCase):
    def test_format_equivalent_existing_spot_mapping_is_not_a_conflict(self) -> None:
        root = Path(tempfile.mkdtemp())
        path = root / "registry.jsonl"
        metadata = v6._metadata_observation()
        metadata["spot_symbol"] = "KII-USDT"
        initial = registry.build_stream_revisions([], [metadata])
        _write_registry(path, initial)
        v6._write_refresh_summary(path, initial)
        receipt = _valid_attestation_preflight("normalised-mapping-v7")

        with mock.patch.object(
            attestation.risk_gate, "preflight", return_value=receipt
        ), mock.patch.object(
            attestation.time,
            "time",
            return_value=int(datetime.fromisoformat(v6.RECEIVED_AT.replace("Z", "+00:00")).timestamp()),
        ):
            result = attestation.attest(
                path=path,
                **_attest_kwargs(run_id="normalised-mapping-v7"),
            )

        self.assertEqual(result["status"], "ATTESTED")
        self.assertEqual(registry.verify_registry(path)["status"], "REGISTRY_OK")

    def test_already_recorded_receipt_returns_the_existing_official_hash(self) -> None:
        root = Path(tempfile.mkdtemp())
        path = root / "registry.jsonl"
        initial = registry.build_stream_revisions([], [v6._metadata_observation()])
        _write_registry(path, initial)
        v6._write_refresh_summary(path, initial)

        def preflight(*, write_class: str, run_id: str) -> dict:
            self.assertEqual(write_class, "official_attestation")
            return _valid_attestation_preflight(run_id)

        with mock.patch.object(
            attestation.risk_gate, "preflight", side_effect=preflight
        ), mock.patch.object(
            attestation.time,
            "time",
            return_value=int(datetime.fromisoformat(v6.RECEIVED_AT.replace("Z", "+00:00")).timestamp()),
        ):
            first = attestation.attest(
                path=path, **_attest_kwargs(run_id="duplicate-v7-first")
            )
            second = attestation.attest(
                path=path, **_attest_kwargs(run_id="duplicate-v7-second")
            )

        self.assertEqual(first["status"], "ATTESTED")
        self.assertEqual(second["status"], "ALREADY_RECORDED")
        self.assertEqual(second["official_record_hash"], first["official_record_hash"])


if __name__ == "__main__":
    unittest.main()
