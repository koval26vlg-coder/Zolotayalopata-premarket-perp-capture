"""RED contract for the pure historical causal-replay boundary.

Post-hoc OHLCV is useful for a fixed descriptive price markout, but it cannot
reconstruct BBO, fills, fees, slippage, funding, or net execution economics.  A
separately sealed L2 request may be delegated to the execution engine only after an
exact canonical input-hash check; even that wrapper remains non-acceptance evidence.

All fixtures are synthetic in-memory objects.  These tests grant no network, file,
capture, private-API, or order authority.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import importlib.util
import inspect
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

T0 = 1_800_000_000
CANDLE_GRANULARITY_SEC = 60
ENTRY_OFFSET_SEC = -60
EXIT_OFFSETS_SEC = (0, 5, 15, 60)


def runtime() -> ModuleType:
    spec = importlib.util.find_spec("historical_causal_replay")
    if spec is None:
        raise AssertionError(
            "RED: src/historical_causal_replay.py is missing; implement the pure "
            "historical replay in the GREEN phase"
        )
    return importlib.import_module("historical_causal_replay")


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def candle(open_ts: int, open_price: str, close_price: str) -> dict[str, object]:
    open_value = float(open_price)
    close_value = float(close_price)
    return {
        "open_ts": open_ts,
        "open": open_price,
        "high": str(max(open_value, close_value) + 1.0),
        "low": str(min(open_value, close_value) - 1.0),
        "close": close_price,
        "base_volume": "10",
        "quote_volume": "1000",
        "closed": True,
    }


def historical_event() -> dict[str, object]:
    event: dict[str, object] = {
        "schema": "premarket_perp_historical_event_v1",
        "event_id": "historical-bybit-new-1800000000",
        "venue": "bybit",
        "listing_venue": "bybit",
        "premarket_contract_id": "NEWUSDT",
        "spot_symbol": "NEWUSDT",
        "asset_class": "CRYPTO_TOKEN",
        "premarket_contract_launch_ts": T0 - 7_200,
        "official_spot_t0": T0,
        "first_trade_ts": None,
        "transition_ts": None,
        "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
        "t0_precision_sec": 1,
        "official_source_url": "https://announcements.example.test/bybit/new",
        "official_record_hash": "a" * 64,
        "history_source_class": "VENUE_PUBLIC_REST_POSTHOC_OHLCV",
        "history_source_url": "https://api.bybit.com/v5/market/kline",
        "history_request_params": {
            "symbol": "NEWUSDT",
            "interval": "1m",
            "start_ts": T0 - 60,
            "end_ts": T0 + 60,
        },
        "retrieved_at_ts": T0 + 86_400,
        "posthoc_retrieval": True,
        "evidence_use": "DESCRIPTIVE_ONLY",
        "price_evidence_class": "POSTHOC_OHLCV_NOT_EXECUTION_EVIDENCE",
        "acceptance_capable": False,
        "capture_eligible": False,
        "execution_evidence": False,
        "orders_allowed": False,
        "candle_count": 3,
        "candles": [
            candle(T0 - 60, "90", "100"),
            candle(T0, "100", "110"),
            candle(T0 + 60, "110", "120"),
        ],
        "raw_payload_sha256": "b" * 64,
    }
    event["manifest_sha256"] = canonical_sha256(event)
    return event


def replay_ohlcv(
    event: dict[str, object],
    *,
    granularity_sec: int = CANDLE_GRANULARITY_SEC,
) -> dict[str, Any]:
    module = runtime()
    try:
        result = module.replay_historical_ohlcv(
            copy.deepcopy(event),
            candle_granularity_sec=granularity_sec,
        )
    except Exception as exc:
        raise AssertionError(
            "historical replay must return a deterministic fail-closed JSON report, "
            f"not raise {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(result, dict):
        raise AssertionError("historical replay must return a JSON-object report")
    return result


def markout(report: dict[str, Any], offset_sec: int) -> dict[str, Any]:
    matches = [
        item
        for item in report.get("markouts", [])
        if item.get("target_offset_sec") == offset_sec
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one fixed candle markout at offset {offset_sec}, got {len(matches)}"
        )
    return matches[0]


def all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(map(str, value)) | set().union(*(all_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(all_keys(item) for item in value))
    return set()


class HistoricalOhlcvMarkoutTests(unittest.TestCase):
    def test_public_api_keeps_fixed_offsets_out_of_caller_control(self) -> None:
        module = runtime()
        signature = inspect.signature(module.replay_historical_ohlcv)

        self.assertEqual(
            tuple(signature.parameters),
            ("historical_event", "candle_granularity_sec"),
        )
        self.assertNotIn("entry_offset", str(signature))
        self.assertNotIn("exit_offsets", str(signature))

    def test_ohlcv_is_always_descriptive_only_with_explicit_candle_granularity(self) -> None:
        report = replay_ohlcv(historical_event())

        self.assertEqual(report["schema"], "premarket_perp_historical_markout_v1")
        self.assertEqual(report["status"], "DESCRIPTIVE_ONLY_COMPLETE")
        self.assertEqual(report["evidence_use"], "DESCRIPTIVE_ONLY")
        self.assertEqual(
            report["price_evidence_class"],
            "POSTHOC_OHLCV_FIXED_CANDLE_MARKOUT_NOT_EXECUTION_EVIDENCE",
        )
        self.assertFalse(report["acceptance_capable"])
        self.assertFalse(report["execution_evidence"])
        self.assertEqual(report["candle_granularity_sec"], 60)
        self.assertEqual(report["sampling_rule"], "TARGET_BUCKET_OPEN")
        self.assertEqual(report["entry"]["target_offset_sec"], -60)
        self.assertEqual(report["entry"]["observed_candle_open_ts"], T0 - 60)
        self.assertEqual(report["entry"]["observed_price"], 90.0)
        self.assertEqual(
            [item["target_offset_sec"] for item in report["markouts"]],
            [0, 5, 15, 60],
        )

    def test_fixed_markouts_use_bucket_open_and_expose_quantisation(self) -> None:
        report = replay_ohlcv(historical_event())

        expected = {
            0: (T0, 100.0, 0, True, 100.0 / 90.0 - 1.0),
            5: (T0, 100.0, 5, False, 100.0 / 90.0 - 1.0),
            15: (T0, 100.0, 15, False, 100.0 / 90.0 - 1.0),
            60: (T0 + 60, 110.0, 0, True, 110.0 / 90.0 - 1.0),
        }
        for offset, (
            candle_open_ts,
            price,
            quantisation_sec,
            exact,
            expected_return,
        ) in expected.items():
            with self.subTest(offset=offset):
                item = markout(report, offset)
                self.assertEqual(item["status"], "OBSERVED_CANDLE_BUCKET_OPEN")
                self.assertEqual(item["target_ts"], T0 + offset)
                self.assertEqual(item["observed_candle_open_ts"], candle_open_ts)
                self.assertEqual(item["observed_price"], price)
                self.assertEqual(item["quantisation_sec"], quantisation_sec)
                self.assertIs(item["exact_target_boundary"], exact)
                self.assertTrue(
                    abs(item["candle_markout_return"] - expected_return) < 1e-12
                )

    def test_missing_target_bucket_is_not_interpolated_from_future_or_previous_candle(self) -> None:
        event = historical_event()
        event["candles"] = [
            row for row in event["candles"] if row["open_ts"] != T0
        ]
        event["candle_count"] = len(event["candles"])
        unhashed = dict(event)
        unhashed.pop("manifest_sha256")
        event["manifest_sha256"] = canonical_sha256(unhashed)

        report = replay_ohlcv(event)

        for offset in (0, 5, 15):
            with self.subTest(offset=offset):
                item = markout(report, offset)
                self.assertEqual(item["status"], "TARGET_CANDLE_BUCKET_MISSING")
                self.assertIsNone(item["observed_candle_open_ts"])
                self.assertIsNone(item["observed_price"])
                self.assertIsNone(item["candle_markout_return"])
        self.assertEqual(markout(report, 60)["status"], "OBSERVED_CANDLE_BUCKET_OPEN")

    def test_mismatched_granularity_fails_closed_without_markouts(self) -> None:
        report = replay_ohlcv(historical_event(), granularity_sec=5)

        self.assertEqual(report["status"], "NOT_RUN_CANDLE_GRANULARITY_MISMATCH")
        self.assertEqual(report["requested_candle_granularity_sec"], 5)
        self.assertEqual(report["observed_candle_granularity_sec"], 60)
        self.assertEqual(report["markouts"], [])
        self.assertFalse(report["acceptance_capable"])

    def test_ohlcv_output_never_manufactures_execution_economics(self) -> None:
        report = replay_ohlcv(historical_event())
        forbidden_fragments = (
            "bbo",
            "bid",
            "ask",
            "fill",
            "fee",
            "slippage",
            "funding",
            "queue",
            "net_pnl",
            "gross_pnl",
            "liquidation",
            "mark_price",
            "index_price",
        )

        keys = {key.lower() for key in all_keys(report)}
        for key in keys:
            for fragment in forbidden_fragments:
                with self.subTest(key=key, fragment=fragment):
                    self.assertNotIn(fragment, key)
        self.assertFalse(report["acceptance_capable"])
        self.assertFalse(report["execution_evidence"])


class SealedL2DelegationTests(unittest.TestCase):
    def sealed_request(self) -> dict[str, object]:
        return {
            "schema": "premarket_perp_execution_replay_request_v1",
            "sealed": True,
            "evidence_class": "SEALED_L2_CAPTURE",
            "capture_manifest_sha256": "c" * 64,
            "event": {
                "event_id": "event_" + "d" * 64,
                "official_spot_t0": T0,
                "venue": "bybit",
            },
            "model": {
                "side": "LONG",
                "paper_notional_usdt": 25.0,
                "leverage_equivalent": 1.0,
                "entry_offset_sec": -60,
                "exit_offsets_sec": [0, 5, 15, 60],
            },
            "depth_snapshots": [{"snapshot_id": "sealed-l2-1"}],
        }

    def delegate(
        self,
        sealed_request: dict[str, object],
        expected_input_sha256: str,
    ) -> dict[str, Any]:
        module = runtime()
        try:
            result = module.delegate_sealed_l2_execution(
                sealed_request=copy.deepcopy(sealed_request),
                expected_input_sha256=expected_input_sha256,
            )
        except Exception as exc:
            raise AssertionError(
                "sealed L2 delegation must return a fail-closed JSON report, "
                f"not raise {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(result, dict):
            raise AssertionError("sealed L2 delegation must return a JSON-object report")
        return result

    def test_exact_input_hash_is_verified_before_bound_engine_rejects_incomplete_input(self) -> None:
        request = self.sealed_request()
        request_before = copy.deepcopy(request)
        expected_hash = canonical_sha256(request)

        report = self.delegate(request, expected_hash)

        self.assertEqual(request, request_before)
        self.assertEqual(report["status"], "NOT_RUN_EXECUTION_DELEGATE_REJECTED")
        self.assertEqual(report["execution_input_sha256"], expected_hash)
        self.assertEqual(report["evidence_use"], "EXECUTION_MODEL_SENSITIVITY_ONLY")
        self.assertFalse(report["acceptance_capable"])
        self.assertIsNone(report["net_pnl_usdt"])
        self.assertEqual(report["orders_created"], 0)

    def test_hash_mismatch_or_unsealed_input_fails_closed(self) -> None:
        mismatch = self.delegate(
            self.sealed_request(),
            "0" * 64,
        )
        unsealed_request = self.sealed_request()
        unsealed_request["sealed"] = False
        unsealed = self.delegate(
            unsealed_request,
            canonical_sha256(unsealed_request),
        )

        self.assertEqual(mismatch["status"], "NOT_RUN_EXECUTION_INPUT_HASH_MISMATCH")
        self.assertEqual(unsealed["status"], "NOT_RUN_EXECUTION_DELEGATE_REJECTED")
        for report in (mismatch, unsealed):
            self.assertFalse(report["acceptance_capable"])
            self.assertIsNone(report["net_pnl_usdt"])
            self.assertEqual(report["orders_created"], 0)


class PurityAndDeterminismTests(unittest.TestCase):
    def test_descriptive_result_hash_is_deterministic_and_self_verifying(self) -> None:
        event = historical_event()
        first = replay_ohlcv(event)
        second = replay_ohlcv(copy.deepcopy(event))

        self.assertEqual(first, second)
        claimed = first["result_hash"]
        material = dict(first)
        del material["result_hash"]
        self.assertEqual(claimed, canonical_sha256(material))
        json.dumps(first, sort_keys=True, allow_nan=False)

    def test_module_has_no_network_files_capture_token_or_order_capability(self) -> None:
        module = runtime()
        source_path = Path(module.__file__).resolve()
        self.assertEqual(source_path, (SRC / "historical_causal_replay.py").resolve())
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))

        forbidden_import_roots = {
            "aiohttp",
            "capture",
            "global_market_writer_claim",
            "http",
            "httpx",
            "os",
            "pathlib",
            "public_http",
            "requests",
            "risk_gate",
            "socket",
            "subprocess",
            "tempfile",
            "urllib",
            "websocket",
        }
        imported_roots: set[str] = set()
        called_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called_names.add(node.func.attr)

        self.assertFalse(imported_roots & forbidden_import_roots)
        self.assertFalse(
            called_names
            & {
                "capture_event",
                "claim_global_market_writer",
                "consume_capture_token",
                "create_order",
                "get_json",
                "mkdir",
                "open",
                "place_order",
                "post_json",
                "rename",
                "replace",
                "request",
                "rmdir",
                "touch",
                "unlink",
                "urlopen",
                "write_bytes",
                "write_text",
            }
        )
        lowered = source.lower()
        for marker in (
            "api_key",
            "api_secret",
            "/v5/order",
            "/api/v5/trade/",
            "/futures/usdt/orders",
            "place-order",
            "cancel-order",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, lowered)


if __name__ == "__main__":
    unittest.main()
