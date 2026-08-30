"""RED contract for the first post-hoc historical-event package.

This package is deliberately narrower than the forward capture path.  It accepts
already-fetched public REST fixtures, normalises venue OHLCV, and emits immutable
DESCRIPTIVE_ONLY evidence.  A candle may describe price history; it may never be
promoted into BBO, fill, queue, slippage, or acceptance evidence.

The test is expected to remain RED until ``src/historical_event_builder.py`` is
implemented.  Nothing in this contract authorises network access, production-path
writes, capture tokens, paper/live orders, or a PlanOnly rebind.
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


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

MODULE_SPEC = importlib.util.find_spec("historical_event_builder")

T0 = 1_800_000_000
RETRIEVED_AT = T0 + 86_400

VENUE_CASES = {
    "bybit": {
        "contract": "NEWUSDT",
        "source_url": "https://api.bybit.com/v5/market/kline",
        # Bybit returns newest first.  The parser must validate that ordering,
        # then expose canonical candles oldest first.
        "payload": {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "category": "linear",
                "symbol": "NEWUSDT",
                "list": [
                    [str((T0 + 60) * 1000), "110", "121", "109", "120", "8", "920"],
                    [str(T0 * 1000), "100", "111", "99", "110", "10", "1050"],
                    [str((T0 - 60) * 1000), "90", "101", "89", "100", "12", "1140"],
                ],
            },
        },
    },
    "okx": {
        "contract": "NEW-USDT-SWAP",
        "source_url": "https://www.okx.com/api/v5/market/candles",
        # OKX also returns newest first; confirm=1 marks completed candles.
        "payload": {
            "code": "0",
            "msg": "",
            "data": [
                [str((T0 + 60) * 1000), "110", "121", "109", "120", "8", "8", "920", "1"],
                [str(T0 * 1000), "100", "111", "99", "110", "10", "10", "1050", "1"],
                [str((T0 - 60) * 1000), "90", "101", "89", "100", "12", "12", "1140", "1"],
            ],
        },
    },
    "gate": {
        "contract": "NEW_USDT",
        "source_url": "https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
        # Gate's fixture is oldest first.  Its public schema is
        # [timestamp, quote_volume, close, high, low, open, base_volume].
        "payload": [
            [str(T0 - 60), "1140", "100", "101", "89", "90", "12"],
            [str(T0), "1050", "110", "111", "99", "100", "10"],
            [str(T0 + 60), "920", "120", "121", "109", "110", "8"],
        ],
    },
}


def _seed(venue: str) -> dict[str, object]:
    case = VENUE_CASES[venue]
    official_urls = {
        "bybit": "https://announcements.bybit.com/en/article/new",
        "okx": "https://www.okx.com/en-us/help/new",
        "gate": "https://www.gate.com/announcements/article/new",
    }
    seed = {
        "schema": "premarket_perp_historical_seed_v1",
        "event_id": f"historical-{venue}-new-{T0}",
        "venue": venue,
        "listing_venue": venue,
        "premarket_contract_id": case["contract"],
        "spot_symbol": "NEWUSDT",
        "asset_class": "CRYPTO_TOKEN",
        "premarket_contract_launch_ts": T0 - 7_200,
        "official_spot_t0": T0,
        "first_trade_ts": None,
        "transition_ts": None,
        "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
        "t0_precision_sec": 1,
        "official_source_url": official_urls[venue],
        "history_source_class": "VENUE_PUBLIC_REST_POSTHOC_OHLCV",
        "history_source_url": case["source_url"],
        "history_request_params": {
            "symbol": case["contract"],
            "interval": "1m",
            "start_ts": T0 - 60,
            "end_ts": T0 + 60,
        },
    }
    seed["official_record_hash"] = _canonical_sha256({
        field: seed[field]
        for field in (
            "venue",
            "premarket_contract_id",
            "spot_symbol",
            "official_spot_t0",
            "t0_source_class",
            "official_source_url",
        )
    })
    return seed


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class HistoricalBuilderModuleContract(unittest.TestCase):
    def test_v39_historical_builder_module_exists(self) -> None:
        self.assertIsNotNone(
            MODULE_SPEC,
            "RED: src/historical_event_builder.py has not been implemented",
        )


@unittest.skipUnless(
    MODULE_SPEC is not None,
    "historical_event_builder is intentionally absent in the RED phase",
)
class HistoricalBuilderV39Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.builder = importlib.import_module("historical_event_builder")

    def _build(self, venue: str, *, payload: object | None = None) -> dict[str, object]:
        if payload is None:
            payload = copy.deepcopy(VENUE_CASES[venue]["payload"])
        return self.builder.build_historical_event(
            seed=_seed(venue),
            venue_payload=payload,
            retrieved_at_ts=RETRIEVED_AT,
        )

    def test_public_api_is_fixture_injected_and_has_no_path_or_fetch_parameter(self) -> None:
        signature = inspect.signature(self.builder.build_historical_event)
        self.assertEqual(
            tuple(signature.parameters),
            ("seed", "venue_payload", "retrieved_at_ts"),
        )
        for forbidden in (
            "path",
            "output",
            "registry",
            "fetch",
            "session",
            "token",
            "order",
        ):
            self.assertNotIn(forbidden, " ".join(signature.parameters).lower())

    def test_bybit_okx_and_gate_are_normalised_to_strict_ascending_ohlcv(self) -> None:
        for venue in VENUE_CASES:
            with self.subTest(venue=venue):
                parsed = self.builder.parse_venue_candles(
                    venue,
                    copy.deepcopy(VENUE_CASES[venue]["payload"]),
                )
                self.assertEqual(
                    [row["open_ts"] for row in parsed],
                    [T0 - 60, T0, T0 + 60],
                )
                self.assertEqual(
                    [
                        (
                            row["open"],
                            row["high"],
                            row["low"],
                            row["close"],
                        )
                        for row in parsed
                    ],
                    [
                        ("90", "101", "89", "100"),
                        ("100", "111", "99", "110"),
                        ("110", "121", "109", "120"),
                    ],
                )
                self.assertTrue(all(row["closed"] is True for row in parsed))

    def test_gate_monthly_archive_is_explicit_and_never_mislabeled_as_rest(self) -> None:
        seed = _seed("gate")
        seed["history_source_class"] = "VENUE_PUBLIC_ARCHIVE_POSTHOC_OHLCV"
        seed["history_source_url"] = (
            "https://download.gatedata.org/futures_usdt/candlesticks_1m/"
            "202602/AZTEC_USDT-202602.csv.gz"
        )
        payload = {
            "archive_schema": "gate_futures_candlesticks_1m_v1",
            "rows": [[str(T0), "10", "110", "111", "99", "100"]],
        }
        result = self.builder.build_historical_event(seed, payload, RETRIEVED_AT)
        self.assertEqual(
            result["history_source_class"],
            "VENUE_PUBLIC_ARCHIVE_POSTHOC_OHLCV",
        )
        self.assertEqual(result["candles"][0]["base_volume"], "10")
        self.assertIsNone(result["candles"][0]["quote_volume"])

    def test_every_posthoc_event_is_descriptive_only_even_with_official_t0(self) -> None:
        for venue in VENUE_CASES:
            with self.subTest(venue=venue):
                result = self._build(venue)
                self.assertEqual(result["schema"], "premarket_perp_historical_event_v1")
                self.assertEqual(result["event_id"], _seed(venue)["event_id"])
                self.assertEqual(result["venue"], venue)
                self.assertEqual(result["official_spot_t0"], T0)
                self.assertEqual(result["t0_source_class"], "OFFICIAL_ANNOUNCEMENT")
                self.assertEqual(result["evidence_use"], "DESCRIPTIVE_ONLY")
                self.assertEqual(
                    result["price_evidence_class"],
                    "POSTHOC_OHLCV_NOT_EXECUTION_EVIDENCE",
                )
                self.assertIs(result["posthoc_retrieval"], True)
                self.assertIs(result["acceptance_capable"], False)
                self.assertIs(result["capture_eligible"], False)
                self.assertIs(result["execution_evidence"], False)
                self.assertIs(result["orders_allowed"], False)
                self.assertEqual(result["retrieved_at_ts"], RETRIEVED_AT)
                self.assertEqual(result["candle_count"], 3)

    def test_candles_never_manufacture_bbo_fills_or_pnl(self) -> None:
        forbidden_keys = {
            "bid",
            "ask",
            "bid_price",
            "ask_price",
            "spread",
            "entry_fill",
            "exit_fill",
            "partial_fill",
            "queue_ahead",
            "slippage",
            "funding_paid",
            "gross_pnl",
            "net_pnl",
        }

        def all_keys(value: object) -> set[str]:
            if isinstance(value, dict):
                return set(value) | set().union(*(all_keys(v) for v in value.values()))
            if isinstance(value, list):
                return set().union(*(all_keys(item) for item in value))
            return set()

        for venue in VENUE_CASES:
            with self.subTest(venue=venue):
                result = self._build(venue)
                self.assertFalse(forbidden_keys & all_keys(result))

    def test_manifest_and_raw_payload_hashes_are_canonical_and_deterministic(self) -> None:
        for venue in VENUE_CASES:
            with self.subTest(venue=venue):
                payload = copy.deepcopy(VENUE_CASES[venue]["payload"])
                first = self._build(venue, payload=copy.deepcopy(payload))
                second = self._build(venue, payload=copy.deepcopy(payload))
                self.assertEqual(first, second)
                self.assertRegex(first["raw_payload_sha256"], r"^[0-9a-f]{64}$")
                self.assertEqual(first["raw_payload_sha256"], _canonical_sha256(payload))
                self.assertRegex(first["manifest_sha256"], r"^[0-9a-f]{64}$")
                unhashed = dict(first)
                del unhashed["manifest_sha256"]
                self.assertEqual(first["manifest_sha256"], _canonical_sha256(unhashed))

    def test_seed_and_fixture_are_not_mutated(self) -> None:
        seed = _seed("bybit")
        payload = copy.deepcopy(VENUE_CASES["bybit"]["payload"])
        seed_before = copy.deepcopy(seed)
        payload_before = copy.deepcopy(payload)

        self.builder.build_historical_event(
            seed=seed,
            venue_payload=payload,
            retrieved_at_ts=RETRIEVED_AT,
        )

        self.assertEqual(seed, seed_before)
        self.assertEqual(payload, payload_before)

    def test_malformed_error_envelopes_rows_and_unclosed_candles_are_rejected(self) -> None:
        malformed = [
            ("bybit", {"retCode": 10001, "retMsg": "bad request", "result": {}}),
            ("bybit", {"retCode": 0, "result": {"list": [["1", "2"]]}}),
            ("okx", {"code": "51000", "msg": "bad request", "data": []}),
            (
                "okx",
                {
                    "code": "0",
                    "data": [[str(T0 * 1000), "1", "2", "0.5", "1.5", "1", "1", "1", "0"]],
                },
            ),
            ("gate", {"label": "INVALID_PARAM_VALUE"}),
            ("gate", [[str(T0), "1", "1", "0.5", "2", "1", "1"]]),
        ]
        for venue, payload in malformed:
            with self.subTest(venue=venue, payload=payload):
                with self.assertRaises(self.builder.HistoricalEventValidationError):
                    self.builder.parse_venue_candles(venue, payload)

    def test_duplicate_or_mixed_out_of_order_timestamps_are_rejected(self) -> None:
        bybit = copy.deepcopy(VENUE_CASES["bybit"]["payload"])
        rows = bybit["result"]["list"]
        rows[:] = [rows[0], rows[2], rows[1]]  # neither newest-first nor oldest-first
        duplicate = copy.deepcopy(VENUE_CASES["gate"]["payload"])
        duplicate[1][0] = duplicate[0][0]

        for venue, payload in (("bybit", bybit), ("gate", duplicate)):
            with self.subTest(venue=venue):
                with self.assertRaises(self.builder.HistoricalEventValidationError):
                    self.builder.parse_venue_candles(venue, payload)

    def test_builder_rejects_non_posthoc_time_and_unsupported_venue(self) -> None:
        with self.assertRaises(self.builder.HistoricalEventValidationError):
            self.builder.build_historical_event(
                seed=_seed("bybit"),
                venue_payload=copy.deepcopy(VENUE_CASES["bybit"]["payload"]),
                retrieved_at_ts=T0 - 1,
            )

        unsupported = _seed("bybit")
        unsupported["venue"] = "binance"
        with self.assertRaises(self.builder.HistoricalEventValidationError):
            self.builder.build_historical_event(
                seed=unsupported,
                venue_payload=copy.deepcopy(VENUE_CASES["bybit"]["payload"]),
                retrieved_at_ts=RETRIEVED_AT,
            )

    def test_module_has_no_network_writer_token_or_order_capability(self) -> None:
        source_path = Path(self.builder.__file__).resolve()
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        forbidden_import_roots = {
            "aiohttp",
            "capture",
            "global_market_writer_claim",
            "httpx",
            "importlib",
            "official_attestation",
            "os",
            "public_http",
            "requests",
            "risk_gate",
            "socket",
            "subprocess",
            "urllib",
            "websocket",
        }
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
        self.assertFalse(imported_roots & forbidden_import_roots)

        forbidden_calls = {
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
            "touch",
            "unlink",
            "urlopen",
            "write_bytes",
            "write_text",
        }
        called: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
        self.assertFalse(called & forbidden_calls)


if __name__ == "__main__":
    unittest.main()
