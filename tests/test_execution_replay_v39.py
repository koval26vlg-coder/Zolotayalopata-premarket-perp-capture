"""RED contract for a pure offline pre-market perpetual execution replay.

The tests deliberately use synthetic, in-memory evidence.  They specify a fixed
paper LONG of 25 USDT at 1x-equivalent, entered before official spot ``t0`` and
independently exited at ``t0`` / ``+5`` / ``+15`` / ``+60`` seconds.  Nothing in
this module authorizes capture, authenticated APIs, exchange orders, or acceptance
claims.

Production implementation belongs to the GREEN phase.  Until then every test fails
with the explicit ``src/execution_replay.py is missing`` assertion.
"""

from __future__ import annotations

import ast
import copy
import importlib
import importlib.util
import json
import math
import sys
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

T0 = 1_900_000_000.0
ENTRY_OFFSET_SEC = -60
EXIT_OFFSETS_SEC = (0, 5, 15, 60)
LATENCY_MS = 200
ORDER_TTL_MS = 500
TAKER_FEE_BPS = 10.0


def execution_runtime() -> ModuleType:
    """Load the wished-for runtime without turning a missing module into collection error."""

    spec = importlib.util.find_spec("execution_replay")
    if spec is None:
        raise AssertionError(
            "RED: src/execution_replay.py is missing; implement the pure offline "
            "execution replay in the GREEN phase"
        )
    return importlib.import_module("execution_replay")


def contract_spec(
    venue: str = "bybit",
    *,
    size_unit: str = "BASE",
    base_per_size_unit: float = 1.0,
    quantity_step_size_units: float = 0.00000001,
) -> dict[str, Any]:
    return {
        "schema": "premarket_perp_contract_spec_v1",
        "venue": venue,
        "contract_id": {
            "bybit": "NEWUSDT",
            "okx": "NEW-USDT-SWAP",
            "gate": "NEW_USDT",
        }[venue],
        "contract_kind": "LINEAR",
        "quote_currency": "USDT",
        "settle_currency": "USDT",
        "size_unit": size_unit,
        "base_per_size_unit": base_per_size_unit,
        "price_tick": 0.01,
        "quantity_step_size_units": quantity_step_size_units,
        "min_quantity_size_units": quantity_step_size_units,
        "min_notional_usdt": 1.0,
        "maintenance_margin_rate": 0.005,
        "price_limit_low": 1.0,
        "price_limit_high": 1_000.0,
        "received_ts": T0 - 3_600,
        "raw_sha256": "a" * 64,
    }


def depth(
    snapshot_id: str,
    received_ts: float,
    *,
    bids: list[list[float]],
    asks: list[list[float]],
    exchange_ts: float | None = None,
) -> dict[str, Any]:
    return {
        "schema": "premarket_perp_depth_snapshot_v1",
        "snapshot_id": snapshot_id,
        "request_ts": received_ts - 0.050,
        "received_ts": received_ts,
        "exchange_ts": received_ts if exchange_ts is None else exchange_ts,
        "bids": bids,
        "asks": asks,
    }


def mark_index(
    observation_id: str,
    received_ts: float,
    mark_price: float,
    index_price: float,
) -> dict[str, Any]:
    return {
        "schema": "premarket_perp_mark_index_observation_v1",
        "observation_id": observation_id,
        "received_ts": received_ts,
        "exchange_ts": received_ts,
        "mark_price": mark_price,
        "index_price": index_price,
    }


def funding(
    observation_id: str,
    settlement_ts: float,
    rate: float,
    *,
    received_ts: float | None = None,
) -> dict[str, Any]:
    return {
        "schema": "premarket_perp_funding_settlement_v1",
        "observation_id": observation_id,
        "settlement_ts": settlement_ts,
        "received_ts": settlement_ts if received_ts is None else received_ts,
        "rate": rate,
    }


def fixed_model() -> dict[str, Any]:
    return {
        "side": "LONG",
        "paper_notional_usdt": 25.0,
        "leverage_equivalent": 1.0,
        "entry_offset_sec": ENTRY_OFFSET_SEC,
        "exit_offsets_sec": list(EXIT_OFFSETS_SEC),
        "latency_ms": LATENCY_MS,
        "order_ttl_ms": ORDER_TTL_MS,
        "taker_fee_bps": TAKER_FEE_BPS,
        "stress_leverages": [2, 5],
        "entry_liquidity": "TAKER_ASKS",
        "exit_liquidity": "TAKER_BIDS",
    }


def complete_request(
    *,
    spec: dict[str, Any] | None = None,
    entry_asks: list[list[float]] | None = None,
    exit_bids: dict[int, list[list[float]]] | None = None,
) -> dict[str, Any]:
    spec = copy.deepcopy(spec or contract_spec())
    base_per_unit = float(spec["base_per_size_unit"])
    raw_one_base = 1.0 / base_per_unit
    entry_asks = entry_asks or [[100.0, raw_one_base]]
    exit_bids = exit_bids or {
        0: [[110.0, raw_one_base]],
        5: [[111.0, raw_one_base]],
        15: [[112.0, raw_one_base]],
        60: [[113.0, raw_one_base]],
    }

    entry_target = T0 + ENTRY_OFFSET_SEC
    snapshots = [
        # Attractive but received before target + latency, therefore unusable.
        depth(
            "entry-pre-latency",
            entry_target + 0.100,
            bids=[[49.0, raw_one_base]],
            asks=[[50.0, raw_one_base]],
            exchange_ts=entry_target + 10.0,
        ),
        depth(
            "entry-causal",
            entry_target + 0.250,
            bids=[[99.0, raw_one_base]],
            asks=entry_asks,
            exchange_ts=entry_target - 10.0,
        ),
    ]
    for offset in EXIT_OFFSETS_SEC:
        snapshots.extend(
            [
                depth(
                    f"exit-{offset}-pre-latency",
                    T0 + offset + 0.100,
                    bids=[[200.0, raw_one_base]],
                    asks=[[201.0, raw_one_base]],
                    exchange_ts=T0 + offset + 10.0,
                ),
                depth(
                    f"exit-{offset}-causal",
                    T0 + offset + 0.250,
                    bids=exit_bids[offset],
                    asks=[[exit_bids[offset][0][0] + 1.0, raw_one_base]],
                    exchange_ts=T0 + offset - 10.0,
                ),
            ]
        )

    observations = [
        mark_index("mark-entry", entry_target + 0.250, 100.0, 100.0),
        mark_index("mark-t0", T0 + 0.250, 110.0, 110.0),
        mark_index("mark-5", T0 + 5.250, 111.0, 111.0),
        mark_index("mark-15", T0 + 15.250, 112.0, 112.0),
        mark_index("mark-60", T0 + 60.250, 113.0, 113.0),
    ]
    return {
        "schema": "premarket_perp_execution_replay_request_v1",
        "event": {
            "event_id": "event_" + "1" * 64,
            "venue": spec["venue"],
            "contract_id": spec["contract_id"],
            "official_spot_t0": T0,
            "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
            "evidence_class": "SYNTHETIC_OFFLINE_ONLY",
        },
        "model": fixed_model(),
        "contract_spec": spec,
        "depth_snapshots": snapshots,
        "funding_observations": [],
        "mark_index_observations": observations,
    }


def replay(request: dict[str, Any]) -> dict[str, Any]:
    runtime = execution_runtime()
    try:
        report = runtime.replay_fixed_long(copy.deepcopy(request))
    except Exception as exc:  # RED must expose a contract failure, not an opaque error.
        raise AssertionError(
            "execution replay must return a deterministic fail-closed JSON report, "
            f"not raise {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(report, dict):
        raise AssertionError("execution replay must return a JSON-object report")
    return report


def horizon(report: dict[str, Any], offset_sec: int) -> dict[str, Any]:
    matches = [
        item for item in report.get("horizons", [])
        if item.get("offset_sec") == offset_sec
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one independent horizon for offset {offset_sec}, "
            f"got {len(matches)}"
        )
    return matches[0]


class CausalFixedModelTests(unittest.TestCase):
    def test_fixed_long_uses_received_clock_plus_latency_and_fixed_horizons(self) -> None:
        report = replay(complete_request())

        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["model"]["side"], "LONG")
        self.assertEqual(report["model"]["paper_notional_usdt"], 25.0)
        self.assertEqual(report["model"]["leverage_equivalent"], 1.0)
        self.assertEqual(report["model"]["entry_offset_sec"], -60)
        self.assertEqual(report["model"]["exit_offsets_sec"], [0, 5, 15, 60])
        self.assertEqual(report["model"]["latency_ms"], LATENCY_MS)
        self.assertEqual(report["model"]["order_ttl_ms"], ORDER_TTL_MS)

        # exchange_ts points in the opposite direction on purpose.  Only received_ts
        # plus fixed latency may make a snapshot eligible.
        self.assertEqual(report["entry"]["selected_snapshot_id"], "entry-causal")
        self.assertEqual(report["entry"]["side"], "BUY")
        self.assertEqual(report["entry"]["arrival_bbo_price"], 100.0)
        for offset in EXIT_OFFSETS_SEC:
            item = horizon(report, offset)
            self.assertEqual(
                item["exit"]["selected_snapshot_id"], f"exit-{offset}-causal"
            )
            self.assertEqual(item["exit"]["side"], "SELL")
            self.assertNotEqual(item["exit"]["arrival_bbo_price"], 200.0)

    def test_entry_sweeps_asks_and_exit_sweeps_bids_with_observed_slippage(self) -> None:
        request = complete_request(
            entry_asks=[[100.0, 0.10], [102.0, 0.30]],
            exit_bids={
                offset: [[110.0 + offset / 10.0, 0.10], [108.0, 0.30]]
                for offset in EXIT_OFFSETS_SEC
            },
        )
        report = replay(request)
        entry = report["entry"]
        exit_zero = horizon(report, 0)["exit"]

        self.assertEqual(entry["status"], "FULL")
        self.assertEqual(len(entry["fills"]), 2)
        self.assertGreater(entry["vwap_price"], entry["arrival_bbo_price"])
        self.assertGreater(entry["observed_slippage_bps"], 0.0)
        self.assertEqual(len(exit_zero["fills"]), 2)
        self.assertLess(exit_zero["vwap_price"], exit_zero["arrival_bbo_price"])
        self.assertGreater(exit_zero["observed_slippage_bps"], 0.0)


class ContractUnitNormalizationTests(unittest.TestCase):
    def test_bybit_okx_and_gate_sizes_normalize_to_the_same_base_economics(self) -> None:
        venue_specs = (
            contract_spec(
                "bybit",
                size_unit="BASE",
                base_per_size_unit=1.0,
                quantity_step_size_units=0.01,
            ),
            contract_spec(
                "okx",
                size_unit="CONTRACTS",
                base_per_size_unit=0.01,
                quantity_step_size_units=1.0,
            ),
            contract_spec(
                "gate",
                size_unit="CONTRACTS",
                base_per_size_unit=0.001,
                quantity_step_size_units=1.0,
            ),
        )
        economics: list[tuple[float, float, float, float]] = []
        for spec in venue_specs:
            with self.subTest(venue=spec["venue"]):
                report = replay(complete_request(spec=spec))
                zero = horizon(report, 0)
                self.assertEqual(report["entry"]["status"], "FULL")
                self.assertEqual(zero["exit"]["status"], "FULL")
                economics.append(
                    (
                        report["entry"]["filled_base_qty"],
                        report["entry"]["filled_quote_notional"],
                        zero["exit"]["filled_base_qty"],
                        zero["exit"]["filled_quote_notional"],
                    )
                )

        self.assertEqual(
            len(economics),
            len(venue_specs),
            "every venue must produce normalized economics before cross-venue comparison",
        )
        for values in economics[1:]:
            for actual, expected in zip(values, economics[0]):
                self.assertTrue(math.isclose(actual, expected, abs_tol=1e-9))
        self.assertTrue(math.isclose(economics[0][0], 0.25, abs_tol=1e-9))
        self.assertTrue(math.isclose(economics[0][1], 25.0, abs_tol=1e-9))


class FillAndCostTests(unittest.TestCase):
    def test_entry_can_partially_fill_and_only_the_filled_position_is_exited(self) -> None:
        request = complete_request(entry_asks=[[100.0, 0.10]])
        report = replay(request)
        zero = horizon(report, 0)

        self.assertEqual(report["entry"]["status"], "PARTIAL")
        self.assertTrue(
            math.isclose(report["entry"]["notional_utilization"], 0.4, abs_tol=1e-9)
        )
        self.assertEqual(zero["exit"]["status"], "FULL")
        self.assertTrue(zero["position_fully_closed"])
        self.assertIsNotNone(zero["net_pnl_usdt"])

    def test_partial_exit_retains_residual_and_does_not_invent_round_trip_pnl(self) -> None:
        request = complete_request(
            exit_bids={offset: [[110.0, 0.10]] for offset in EXIT_OFFSETS_SEC}
        )
        report = replay(request)
        zero = horizon(report, 0)

        self.assertEqual(report["entry"]["status"], "FULL")
        self.assertEqual(zero["exit"]["status"], "PARTIAL")
        self.assertGreater(zero["residual_base_qty"], 0.0)
        self.assertFalse(zero["position_fully_closed"])
        self.assertIsNone(zero["net_pnl_usdt"])

    def test_snapshot_after_ttl_is_unfilled_and_creates_no_virtual_position(self) -> None:
        request = complete_request()
        request["depth_snapshots"] = [
            row
            for row in request["depth_snapshots"]
            if not row["snapshot_id"].startswith("entry-")
        ]
        entry_target = T0 + ENTRY_OFFSET_SEC
        request["depth_snapshots"].append(
            depth(
                "entry-too-late",
                entry_target + LATENCY_MS / 1_000 + ORDER_TTL_MS / 1_000 + 0.001,
                bids=[[99.0, 1.0]],
                asks=[[100.0, 1.0]],
            )
        )

        report = replay(request)

        self.assertEqual(report["status"], "NO_POSITION_ENTRY_UNFILLED")
        self.assertEqual(report["entry"]["status"], "UNFILLED_NO_CAUSAL_DEPTH")
        self.assertEqual(report["virtual_positions_created"], 0)
        self.assertIsNone(report["net_pnl_usdt"])
        for item in report["horizons"]:
            self.assertIsNone(item["net_pnl_usdt"])

    def test_taker_fees_are_charged_on_both_observed_fills(self) -> None:
        report = replay(complete_request())
        zero = horizon(report, 0)

        self.assertTrue(math.isclose(zero["gross_pnl_usdt"], 2.5, abs_tol=1e-9))
        self.assertTrue(
            math.isclose(zero["fees_usdt"]["entry"], 0.025, abs_tol=1e-9)
        )
        self.assertTrue(
            math.isclose(zero["fees_usdt"]["exit"], 0.0275, abs_tol=1e-9)
        )
        self.assertTrue(
            math.isclose(zero["fees_usdt"]["total"], 0.0525, abs_tol=1e-9)
        )
        self.assertTrue(math.isclose(zero["funding_pnl_usdt"], 0.0, abs_tol=1e-9))
        self.assertTrue(math.isclose(zero["net_pnl_usdt"], 2.4475, abs_tol=1e-9))


class FundingAndLiquidationTests(unittest.TestCase):
    def test_funding_uses_only_settlements_crossed_by_each_independent_position(self) -> None:
        request = complete_request()
        request["funding_observations"] = [
            funding("before-entry", T0 - 70, 0.009),
            funding("crossed-before-t0", T0 - 10, 0.001),
            funding("crossed-after-t0", T0 + 10, 0.002),
            funding("after-last-exit", T0 + 70, 0.009),
        ]

        report = replay(request)

        self.assertEqual(
            horizon(report, 0)["funding"]["settlement_ids"],
            ["crossed-before-t0"],
        )
        self.assertEqual(
            horizon(report, 5)["funding"]["settlement_ids"],
            ["crossed-before-t0"],
        )
        self.assertEqual(
            horizon(report, 15)["funding"]["settlement_ids"],
            ["crossed-before-t0", "crossed-after-t0"],
        )
        self.assertEqual(
            horizon(report, 60)["funding"]["settlement_ids"],
            ["crossed-before-t0", "crossed-after-t0"],
        )
        for offset in EXIT_OFFSETS_SEC:
            self.assertLess(horizon(report, offset)["funding_pnl_usdt"], 0.0)

    def test_mark_not_index_drives_preregistered_2x_and_5x_liquidation_stress(self) -> None:
        request = complete_request()
        request["mark_index_observations"] = [
            mark_index("entry", T0 - 59.750, 100.0, 100.0),
            mark_index("divergence", T0 + 1.0, 75.0, 95.0),
            mark_index("recovery", T0 + 60.250, 113.0, 113.0),
        ]

        report = replay(request)
        item = horizon(report, 5)

        self.assertFalse(item["liquidation_stress"]["2x"]["liquidated"])
        self.assertTrue(item["liquidation_stress"]["5x"]["liquidated"])
        self.assertEqual(
            item["liquidation_stress"]["5x"]["trigger_observation_id"],
            "divergence",
        )
        self.assertEqual(item["liquidation_stress"]["5x"]["trigger_mark_price"], 75.0)
        self.assertEqual(item["liquidation_stress"]["5x"]["index_price_at_trigger"], 95.0)
        self.assertGreater(item["max_abs_mark_index_divergence_bps"], 1_500.0)


class FailClosedAndDeterminismTests(unittest.TestCase):
    def test_missing_mandatory_inputs_return_no_position_and_no_pnl(self) -> None:
        cases = (
            ("contract_spec", "contract_spec"),
            ("depth_snapshots", "depth_snapshots"),
            ("funding_observations", "funding_observations"),
            ("mark_index_observations", "mark_index_observations"),
            ("model.taker_fee_bps", "model.taker_fee_bps"),
            (
                "contract_spec.maintenance_margin_rate",
                "contract_spec.maintenance_margin_rate",
            ),
        )
        for path, expected_missing in cases:
            with self.subTest(missing=path):
                request = complete_request()
                if "." in path:
                    parent, child = path.split(".", 1)
                    del request[parent][child]
                else:
                    del request[path]

                report = replay(request)

                self.assertEqual(report["status"], "NOT_RUN_MANDATORY_INPUT_MISSING")
                self.assertIn(expected_missing, report["missing_inputs"])
                self.assertEqual(report["virtual_positions_created"], 0)
                self.assertEqual(report["orders_created"], 0)
                self.assertIsNone(report["net_pnl_usdt"])
                self.assertFalse(report["acceptance_capable"])

    def test_result_hash_is_canonical_and_independent_of_input_row_order(self) -> None:
        request = complete_request()
        first = replay(request)
        reordered = copy.deepcopy(request)
        reordered["depth_snapshots"].reverse()
        reordered["funding_observations"].reverse()
        reordered["mark_index_observations"].reverse()
        second = replay(reordered)

        self.assertEqual(first, second)
        self.assertEqual(first["result_hash"], second["result_hash"])
        material = dict(first)
        claimed = material.pop("result_hash")
        runtime = execution_runtime()
        self.assertEqual(claimed, runtime.canonical_result_hash(material))
        json.dumps(first, allow_nan=False, sort_keys=True)

    def test_runtime_has_no_network_files_private_api_or_order_capability(self) -> None:
        runtime = execution_runtime()
        source_path = Path(runtime.__file__).resolve()
        self.assertEqual(source_path, (SRC / "execution_replay.py").resolve())
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))

        forbidden_import_roots = {
            "capture",
            "http",
            "httpx",
            "os",
            "pathlib",
            "public_http",
            "requests",
            "socket",
            "subprocess",
            "tempfile",
            "urllib",
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
                "mkdir",
                "open",
                "place_order",
                "rename",
                "replace",
                "rmdir",
                "touch",
                "unlink",
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

        report = replay(complete_request())
        self.assertEqual(report["orders_created"], 0)
        self.assertFalse(report["live_execution"])
        self.assertFalse(report["private_api_used"])
        self.assertFalse(report["acceptance_capable"])


if __name__ == "__main__":
    unittest.main()
