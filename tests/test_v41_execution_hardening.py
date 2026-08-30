"""Contracts for v42 execution-replay evidence hardening.

The fixtures are synthetic and in-memory.  They grant no capture, network,
private-API, paper-exchange, order, leverage, margin, or acceptance authority.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import inspect
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
for path in (SRC, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from test_execution_replay_v39 import T0, complete_request, horizon


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def execution_runtime() -> ModuleType:
    return importlib.import_module("execution_replay")


def historical_runtime() -> ModuleType:
    return importlib.import_module("historical_causal_replay")


def unsigned_execution_request() -> dict[str, Any]:
    request = complete_request()
    request["event"].update(
        {
            "t0_precision_sec": 1,
            "official_source_url": "https://announcements.example.test/bybit/new",
            "official_record_hash": "d" * 64,
        }
    )
    request.update(
        {
            "sealed": True,
            "evidence_class": "SEALED_L2_CAPTURE",
            "capture_manifest_sha256": "c" * 64,
        }
    )
    return request


def seal(payload: dict[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(payload)
    sealed["evidence_envelope"] = {
        "schema": "premarket_perp_execution_evidence_envelope_v1",
        "sealed": True,
        "evidence_class": "SEALED_L2_CAPTURE",
        "capture_manifest_sha256": sealed.get("capture_manifest_sha256"),
        "payload_sha256": canonical_sha256(sealed),
    }
    return sealed


def sealed_execution_request() -> dict[str, Any]:
    return seal(unsigned_execution_request())


def replay_direct(request: dict[str, Any]) -> dict[str, Any]:
    try:
        report = execution_runtime().replay_fixed_long(copy.deepcopy(request))
    except Exception as exc:
        raise AssertionError(
            "execution replay must return a fail-closed JSON report, "
            f"not raise {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(report, dict):
        raise AssertionError("execution replay must return a JSON-object report")
    return report


def delegate_historical(request: dict[str, Any]) -> dict[str, Any]:
    try:
        report = historical_runtime().delegate_sealed_l2_execution(
            sealed_request=copy.deepcopy(request),
            expected_input_sha256=canonical_sha256(request),
        )
    except Exception as exc:
        raise AssertionError(
            "historical delegation must invoke execution_replay directly and return "
            f"a fail-closed JSON report, not raise {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(report, dict):
        raise AssertionError("historical delegation must return a JSON-object report")
    return report


def funding_row(
    observation_id: str,
    settlement_ts: float,
    received_ts: float,
    rate: float,
    settlement_mark_price: float = 100.0,
) -> dict[str, Any]:
    return {
        "schema": "premarket_perp_funding_settlement_v1",
        "observation_id": observation_id,
        "settlement_ts": settlement_ts,
        "received_ts": received_ts,
        "rate": rate,
        "settlement_mark_price": settlement_mark_price,
    }


def assert_fail_closed(case: unittest.TestCase, report: dict[str, Any]) -> None:
    status = str(report.get("status", ""))
    case.assertRegex(status, r"^NOT_RUN_", f"unexpected status: {status}")
    case.assertEqual(report.get("orders_created", 0), 0)
    case.assertEqual(report.get("virtual_positions_created", 0), 0)
    case.assertIsNone(report.get("net_pnl_usdt"))


class ExecutionEvidenceBoundaryTests(unittest.TestCase):
    def test_legacy_top_level_seal_claim_is_not_an_internal_evidence_seal(self) -> None:
        report = replay_direct(unsigned_execution_request())

        assert_fail_closed(self, report)
        self.assertIn("SEAL", report["status"])

    def test_payload_mutation_after_internal_seal_is_rejected(self) -> None:
        request = sealed_execution_request()
        request["depth_snapshots"][1]["asks"][0][1] = 2.0

        report = replay_direct(request)

        assert_fail_closed(self, report)
        self.assertIn("HASH", report["status"])

    def test_top_level_execution_request_schema_is_mandatory(self) -> None:
        request = unsigned_execution_request()
        request["schema"] = "premarket_perp_historical_event_v1"
        request["candles"] = [{"open_ts": int(T0), "open": "100"}]

        report = replay_direct(seal(request))

        assert_fail_closed(self, report)

    def test_caller_resealed_payload_is_not_a_trusted_capture(self) -> None:
        request = unsigned_execution_request()
        request["event"]["official_source_url"] = "https://attacker.invalid/fake"
        request["event"]["official_record_hash"] = "a" * 64

        report = replay_direct(seal(request))

        assert_fail_closed(self, report)
        self.assertEqual(report["status"], "NOT_RUN_TRUSTED_EVIDENCE_LOADER_REQUIRED")

        delegated = delegate_historical(seal(request))
        assert_fail_closed(self, delegated)
        self.assertEqual(delegated.get("delegate_status"), report["status"])


class EventIdentityTests(unittest.TestCase):
    def test_proxy_nonofficial_or_nonseconds_t0_is_rejected(self) -> None:
        cases = (
            ("source", "DETECTION_TIME_PROXY"),
            ("source", "VENUE_INSTRUMENT_METADATA"),
            ("precision", 60),
            ("precision", None),
        )
        for kind, value in cases:
            with self.subTest(kind=kind, value=value):
                request = unsigned_execution_request()
                if kind == "source":
                    request["event"]["t0_source_class"] = value
                elif value is None:
                    request["event"].pop("t0_precision_sec")
                else:
                    request["event"]["t0_precision_sec"] = value

                assert_fail_closed(self, replay_direct(seal(request)))

    def test_event_venue_and_contract_must_match_contract_spec(self) -> None:
        for field, value in (("venue", "okx"), ("contract_id", "OTHERUSDT")):
            with self.subTest(field=field):
                request = unsigned_execution_request()
                request["event"][field] = value

                assert_fail_closed(self, replay_direct(seal(request)))


class FundingEvidenceTests(unittest.TestCase):
    def test_funding_window_uses_actual_entry_and_exit_received_times(self) -> None:
        request = complete_request()
        request["funding_observations"] = [
            funding_row(
                "settled-before-actual-entry",
                T0 - 59.900,
                T0 - 59.800,
                0.02,
                300.0,
            ),
            funding_row(
                "settled-before-actual-exit",
                T0 + 0.100,
                T0 + 0.200,
                0.01,
                200.0,
            ),
        ]

        zero = horizon(replay_direct(request), 0)

        self.assertEqual(
            zero["funding"]["settlement_ids"],
            ["settled-before-actual-exit"],
        )

    def test_funding_uses_settlement_mark_value_not_entry_quote(self) -> None:
        request = complete_request()
        request["funding_observations"] = [
            funding_row("marked-settlement", T0 - 10.0, T0 - 9.9, 0.01, 200.0)
        ]

        zero = horizon(replay_direct(request), 0)

        # 0.25 base * 200 settlement mark * 1% paid by a LONG.
        self.assertAlmostEqual(zero["funding_pnl_usdt"], -0.5)

    def test_received_before_settlement_funding_is_rejected(self) -> None:
        request = complete_request()
        request["funding_observations"] = [
            funding_row("impossible-clock", T0 - 10.0, T0 - 10.1, 0.01)
        ]

        assert_fail_closed(self, replay_direct(request))

    def test_duplicate_funding_ids_or_settlement_instants_are_rejected(self) -> None:
        cases = (
            [
                funding_row("duplicate-id", T0 - 20.0, T0 - 19.9, 0.001),
                funding_row("duplicate-id", T0 - 10.0, T0 - 9.9, 0.002),
            ],
            [
                funding_row("settlement-a", T0 - 10.0, T0 - 9.9, 0.001),
                funding_row("settlement-b", T0 - 10.0, T0 - 9.8, 0.002),
            ],
        )
        for rows in cases:
            with self.subTest(ids=[row["observation_id"] for row in rows]):
                request = complete_request()
                request["funding_observations"] = rows

                assert_fail_closed(self, replay_direct(request))


class MarketEvidenceQualityTests(unittest.TestCase):
    def test_missing_mark_index_evidence_makes_liquidation_unknown(self) -> None:
        request = complete_request()
        request["mark_index_observations"] = []

        report = replay_direct(request)

        self.assertEqual(report["status"], "COMPLETE")
        for offset in (0, 5, 15, 60):
            for leverage in ("2x", "5x"):
                with self.subTest(offset=offset, leverage=leverage):
                    stress = horizon(report, offset)["liquidation_stress"][leverage]
                    self.assertIsNone(stress["liquidated"])
                    self.assertEqual(stress.get("evidence_coverage"), "MISSING")

    def test_nonempty_but_invalid_mark_index_rows_are_still_missing(self) -> None:
        request = complete_request()
        request["mark_index_observations"] = [
            {
                "observation_id": "invalid-mark",
                "received_ts": "not-a-clock",
                "mark_price": -1,
                "index_price": None,
            }
        ]

        report = replay_direct(request)

        self.assertEqual(report["status"], "COMPLETE")
        for item in report["horizons"]:
            self.assertTrue(item["liquidation_model_missing"])
            for stress in item["liquidation_stress"].values():
                self.assertEqual(stress["evidence_coverage"], "MISSING")
                self.assertIsNone(stress["liquidated"])

    def test_unsorted_or_crossed_depth_fails_closed(self) -> None:
        cases = (
            ("entry-causal", "asks", [[101.0, 1.0], [100.0, 1.0]]),
            ("exit-0-causal", "bids", [[109.0, 1.0], [110.0, 1.0]]),
            ("entry-causal", "crossed", None),
        )
        for snapshot_id, field, levels in cases:
            with self.subTest(snapshot_id=snapshot_id, defect=field):
                request = complete_request()
                snapshot = next(
                    row
                    for row in request["depth_snapshots"]
                    if row["snapshot_id"] == snapshot_id
                )
                if field == "crossed":
                    snapshot["bids"] = [[101.0, 1.0]]
                    snapshot["asks"] = [[100.0, 1.0]]
                else:
                    snapshot[field] = levels

                assert_fail_closed(self, replay_direct(request))


class HistoricalExecutionDelegationTests(unittest.TestCase):
    def test_historical_delegation_has_no_injected_execution_callback(self) -> None:
        signature = inspect.signature(
            historical_runtime().delegate_sealed_l2_execution
        )

        self.assertNotIn("execution_delegate", signature.parameters)

    def test_historical_delegation_rejects_self_attested_sealed_input(self) -> None:
        request = sealed_execution_request()
        expected = replay_direct(request)

        report = delegate_historical(request)

        self.assertEqual(expected["status"], "NOT_RUN_TRUSTED_EVIDENCE_LOADER_REQUIRED")
        assert_fail_closed(self, report)
        self.assertEqual(report["status"], "NOT_RUN_EXECUTION_DELEGATE_REJECTED")
        self.assertEqual(report["delegate_status"], expected["status"])

    def test_historical_delegation_rejects_ohlcv_shaped_spoof(self) -> None:
        spoof = {
            "schema": "premarket_perp_historical_event_v1",
            "sealed": True,
            "evidence_class": "SEALED_L2_CAPTURE",
            "capture_manifest_sha256": "c" * 64,
            "official_spot_t0": T0,
            "candles": [{"open_ts": int(T0), "open": "100", "closed": True}],
        }

        assert_fail_closed(self, delegate_historical(seal(spoof)))


if __name__ == "__main__":
    unittest.main()
