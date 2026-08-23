from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import event_registry as registry  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import risk_gate  # noqa: E402


NOW = 1_800_000_000


def bybit_row(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "baseCoin": symbol.removesuffix("USDT"),
        "symbolType": "innovation",
        "status": "PreLaunch",
        "isPreListing": True,
        "contractType": "LinearPerpetual",
        "launchTime": str((NOW - 60) * 1000),
    }


def bybit_trading_row(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "baseCoin": symbol.removesuffix("USDT"),
        "symbolType": "innovation",
        "status": "Trading",
        "isPreListing": False,
        "contractType": "LinearPerpetual",
        "launchTime": str((NOW - 60) * 1000),
    }


def gate_row(symbol: str, *, status: str = "trading", premarket: bool = True) -> dict:
    return {
        "name": symbol,
        "contract_type": "crypto",
        "status": status,
        "is_pre_market": premarket,
        "create_time": NOW - 3600,
    }


class RelevantIdentitySnapshotTests(unittest.TestCase):
    @staticmethod
    def surface(surface_id: str):
        return next(item for item in registry.SURFACES if item.surface_id == surface_id)

    def test_identity_set_hash_is_order_independent(self) -> None:
        surface = self.surface("bybit_linear_prelaunch")

        left = registry.build_relevant_identity_snapshot(
            surface, [bybit_row("AAAUSDT"), bybit_row("BBBUSDT")],
            now_ts=NOW, tracked_ids=set(),
        )
        right = registry.build_relevant_identity_snapshot(
            surface, [bybit_row("BBBUSDT"), bybit_row("AAAUSDT")],
            now_ts=NOW, tracked_ids=set(),
        )

        self.assertEqual(left.relevant_identity_set_sha256, right.relevant_identity_set_sha256)
        self.assertEqual(left.relevant_ids, ("AAAUSDT", "BBBUSDT"))

    def test_duplicate_native_ids_are_rejected(self) -> None:
        surface = self.surface("bybit_linear_prelaunch")

        with self.assertRaisesRegex(registry.EventRegistryError, "duplicate.*AAAUSDT"):
            registry.build_relevant_identity_snapshot(
                surface, [bybit_row("AAAUSDT"), bybit_row("AAAUSDT")],
                now_ts=NOW, tracked_ids=set(),
            )

    def test_same_count_with_target_replaced_by_filler_is_incomplete(self) -> None:
        surface = self.surface("bybit_linear_prelaunch")

        snapshot = registry.build_relevant_identity_snapshot(
            surface,
            [bybit_row("FILLERUSDT")],
            now_ts=NOW,
            tracked_ids={"TARGETUSDT"},
        )

        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.missing_tracked_ids, ("TARGETUSDT",))

    def test_explicit_gate_terminal_row_can_close_target(self) -> None:
        surface = self.surface("gate_usdt_contracts")

        snapshot = registry.build_relevant_identity_snapshot(
            surface,
            [gate_row("TARGET_USDT", status="delisted")],
            now_ts=NOW,
            tracked_ids={"TARGET_USDT"},
        )

        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.explicit_terminal_ids, ("TARGET_USDT",))
        self.assertEqual(snapshot.missing_tracked_ids, ())

    def test_unknown_tracked_state_is_incomplete(self) -> None:
        surface = self.surface("gate_usdt_contracts")
        row = gate_row("TARGET_USDT", status="mystery")

        snapshot = registry.build_relevant_identity_snapshot(
            surface,
            [row],
            now_ts=NOW,
            tracked_ids={"TARGET_USDT"},
        )

        self.assertFalse(snapshot.complete)
        self.assertIn("UNKNOWN_LIFECYCLE", snapshot.problems[0])


class RelevantIdentityAuthorityTests(unittest.TestCase):
    def test_authority_hash_changes_when_identity_set_changes_at_same_count(self) -> None:
        first = {
            "bybit_linear_prelaunch": registry.canonical_hash({"ids": ["TARGETUSDT"]}),
            "okx_swap": registry.canonical_hash({"ids": []}),
            "okx_futures": registry.canonical_hash({"ids": []}),
            "gate_usdt_contracts": registry.canonical_hash({"ids": []}),
        }
        second = dict(
            first,
            bybit_linear_prelaunch=registry.canonical_hash({"ids": ["FILLERUSDT"]}),
        )

        self.assertNotEqual(
            registry.registry_authority_state_hash(
                active_generations={"bybit": {}, "okx": {}, "gate": {}},
                lifecycle_high_water={"bybit": {}, "okx": {}, "gate": {}},
                metadata_refresh_received_at="2026-08-23T00:00:00Z",
                raw_universe_rows_by_surface={key: 1 for key in first},
                relevant_identity_hashes_by_surface=first,
            ),
            registry.registry_authority_state_hash(
                active_generations={"bybit": {}, "okx": {}, "gate": {}},
                lifecycle_high_water={"bybit": {}, "okx": {}, "gate": {}},
                metadata_refresh_received_at="2026-08-23T00:00:00Z",
                raw_universe_rows_by_surface={key: 1 for key in second},
                relevant_identity_hashes_by_surface=second,
            ),
        )


class RefreshIdentityRetentionIntegrationTests(unittest.TestCase):
    @staticmethod
    def preflight(run_id: str) -> dict:
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
            "resolved_paths_hash": "7" * 64,
        }

    @staticmethod
    def payloads(bybit_symbol: str) -> dict:
        return {
            "bybit": {
                "retCode": 0,
                "result": {
                    "category": "linear",
                    "list": [bybit_row(bybit_symbol)],
                    "nextPageCursor": "",
                },
            },
            # A combined offline fixture represents the two separately requested
            # production surfaces. Production code must still issue both requests.
            "okx": {
                "code": "0",
                "data": [
                    {
                        "instId": "OKA-USDT-SWAP",
                        "instType": "SWAP",
                        "uly": "OKA-USDT",
                        "instCategory": "1",
                        "ruleType": "pre_market",
                        "state": "live",
                        "listTime": str((NOW - 60) * 1000),
                    },
                    {
                        "instId": "OKB-USDT-260930",
                        "instType": "FUTURES",
                        "uly": "OKB-USDT",
                        "instCategory": "1",
                        "ruleType": "pre_market",
                        "state": "live",
                        "listTime": str((NOW - 60) * 1000),
                    },
                ],
            },
            "gate": [gate_row("GATE_USDT")],
        }

    @classmethod
    def payloads_with_bybit_row(cls, row: dict) -> dict:
        payloads = cls.payloads("IGNOREDUSDT")
        payloads["bybit"]["result"]["list"] = [row]
        return payloads

    @classmethod
    def structured_bybit_payloads(
        cls,
        *,
        prelaunch_rows: list[dict],
        trading_rows: list[dict],
    ) -> dict:
        payloads = cls.payloads("IGNOREDUSDT")
        payloads["bybit"] = {
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
        }
        return payloads

    def test_same_count_replacement_cannot_commit_a_refresh(self) -> None:
        path = Path(tempfile.mkdtemp()) / "registry-v3.jsonl"
        with mock.patch.object(
            registry.risk_gate,
            "preflight",
            side_effect=lambda *, write_class, run_id: self.preflight(run_id),
        ):
            first = registry.refresh(
                payloads=self.payloads("TARGETUSDT"),
                path=path,
                observed_at_utc="2026-08-23T00:00:00Z",
                run_id="identity-first",
            )
            before = path.read_bytes()
            with self.assertRaisesRegex(
                registry.EventRegistryError, "MISSING_TRACKED_IDENTITIES"
            ):
                registry.refresh(
                    payloads=self.payloads("FILLERUSDT"),
                    path=path,
                    observed_at_utc="2026-08-23T01:00:00Z",
                    run_id="identity-replaced",
                )

        self.assertTrue(first["complete"])
        self.assertEqual(path.read_bytes(), before)

    def test_explicit_terminal_then_reappearance_allocates_next_generation(self) -> None:
        path = Path(tempfile.mkdtemp()) / "registry-v3.jsonl"
        terminal = bybit_trading_row("TARGETUSDT")
        with mock.patch.object(
            registry.risk_gate,
            "preflight",
            side_effect=lambda *, write_class, run_id: self.preflight(run_id),
        ):
            first = registry.refresh(
                payloads=self.payloads("TARGETUSDT"),
                path=path,
                observed_at_utc="2026-08-23T00:00:00Z",
                run_id="terminal-first",
            )
            closed = registry.refresh(
                payloads=self.structured_bybit_payloads(
                    prelaunch_rows=[],
                    trading_rows=[terminal],
                ),
                path=path,
                observed_at_utc="2026-08-23T01:00:00Z",
                run_id="terminal-explicit",
            )
            reopened = registry.refresh(
                payloads=self.payloads("TARGETUSDT"),
                path=path,
                observed_at_utc="2026-08-23T02:00:00Z",
                run_id="terminal-reopened",
            )

        self.assertEqual(
            first[registry.ACTIVE_LIFECYCLE_GENERATIONS_FIELD]["bybit"]["TARGETUSDT"],
            0,
        )
        self.assertEqual(
            closed[registry.ACTIVE_LIFECYCLE_GENERATIONS_FIELD]["bybit"], {}
        )
        self.assertEqual(
            closed[registry.EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD][
                "bybit_linear_trading"
            ],
            ["TARGETUSDT"],
        )
        self.assertEqual(
            reopened[registry.ACTIVE_LIFECYCLE_GENERATIONS_FIELD]["bybit"][
                "TARGETUSDT"
            ],
            1,
        )

    def test_bybit_transition_is_closed_by_the_separate_trading_surface(self) -> None:
        path = Path(tempfile.mkdtemp()) / "registry-v3.jsonl"
        with mock.patch.object(
            registry.risk_gate,
            "preflight",
            side_effect=lambda *, write_class, run_id: self.preflight(run_id),
        ):
            first = registry.refresh(
                payloads=self.structured_bybit_payloads(
                    prelaunch_rows=[bybit_row("TARGETUSDT")],
                    trading_rows=[bybit_trading_row("FILLERUSDT")],
                ),
                path=path,
                observed_at_utc="2026-08-23T00:00:00Z",
                run_id="bybit-prelaunch-first",
            )
            transitioned = registry.refresh(
                payloads=self.structured_bybit_payloads(
                    prelaunch_rows=[bybit_row("FILLERUSDT")],
                    trading_rows=[bybit_trading_row("TARGETUSDT")],
                ),
                path=path,
                observed_at_utc="2026-08-23T01:00:00Z",
                run_id="bybit-transition-second",
            )

        self.assertTrue(first["complete"])
        self.assertTrue(transitioned["complete"])
        self.assertNotIn(
            "TARGETUSDT",
            transitioned[registry.ACTIVE_LIFECYCLE_GENERATIONS_FIELD]["bybit"],
        )
        self.assertIn(
            "TARGETUSDT",
            transitioned[registry.EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD][
                "bybit_linear_trading"
            ],
        )
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        transition_records = [
            record
            for record in records
            if record.get("premarket_contract_id") == "TARGETUSDT"
            and record.get("timestamp_kind") == registry.TIMESTAMP_TRANSITION
            and record.get("source_class") == registry.SOURCE_OBSERVED_LIFECYCLE
        ]
        self.assertEqual(len(transition_records), 1)
        self.assertEqual(transition_records[0]["evidence_use"], "DESCRIPTIVE_ONLY")
        self.assertFalse(transition_records[0]["capture_eligible"])
        self.assertIn(
            "DETECTION_TIME_PROXY_NOT_ACTUAL_TRANSITION_TIME",
            transition_records[0]["caveats"],
        )

    def test_terminal_precedence_survives_one_refresh_without_becoming_required(self) -> None:
        path = Path(tempfile.mkdtemp()) / "registry-v3.jsonl"
        stale_active = bybit_row("TARGETUSDT")
        terminal = bybit_trading_row("TARGETUSDT")
        with mock.patch.object(
            registry.risk_gate,
            "preflight",
            side_effect=lambda *, write_class, run_id: self.preflight(run_id),
        ):
            first = registry.refresh(
                payloads=self.structured_bybit_payloads(
                    prelaunch_rows=[stale_active],
                    trading_rows=[bybit_trading_row("FILLERUSDT")],
                ),
                path=path,
                observed_at_utc="2026-08-23T00:00:00Z",
                run_id="terminal-memory-active",
            )
            first_terminal = registry.refresh(
                payloads=self.structured_bybit_payloads(
                    prelaunch_rows=[stale_active],
                    trading_rows=[terminal],
                ),
                path=path,
                observed_at_utc="2026-08-23T01:00:00Z",
                run_id="terminal-memory-first-terminal",
            )
            repeated_terminal = registry.refresh(
                payloads=self.structured_bybit_payloads(
                    prelaunch_rows=[stale_active],
                    trading_rows=[terminal],
                ),
                path=path,
                observed_at_utc="2026-08-23T02:00:00Z",
                run_id="terminal-memory-repeated-terminal",
            )
            reopened = registry.refresh(
                payloads=self.structured_bybit_payloads(
                    prelaunch_rows=[stale_active],
                    trading_rows=[bybit_trading_row("FILLERUSDT")],
                ),
                path=path,
                observed_at_utc="2026-08-23T03:00:00Z",
                run_id="terminal-memory-terminal-disappeared",
            )

        active_field = registry.ACTIVE_LIFECYCLE_GENERATIONS_FIELD
        terminal_field = registry.EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD
        self.assertEqual(first[active_field]["bybit"]["TARGETUSDT"], 0)
        self.assertNotIn("TARGETUSDT", first_terminal[active_field]["bybit"])
        self.assertNotIn("TARGETUSDT", repeated_terminal[active_field]["bybit"])
        self.assertEqual(
            repeated_terminal[terminal_field]["bybit_linear_trading"],
            ["TARGETUSDT"],
        )
        # Previous terminal ids are classification memory, not a permanent
        # completeness requirement. Once terminal evidence disappears, native-id
        # reappearance starts the next lifecycle generation.
        self.assertEqual(reopened[active_field]["bybit"]["TARGETUSDT"], 1)

if __name__ == "__main__":
    unittest.main()
