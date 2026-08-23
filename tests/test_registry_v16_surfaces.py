from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import event_registry as registry  # noqa: E402


NOW = 1_800_000_000


class SurfaceContractTests(unittest.TestCase):
    def test_required_surfaces_are_explicit(self) -> None:
        self.assertEqual(
            {surface.surface_id for surface in registry.SURFACES},
            {
                "bybit_linear_prelaunch",
                "bybit_linear_trading",
                "okx_swap",
                "okx_futures",
                "gate_usdt_contracts",
            },
        )

    def test_bybit_fetch_requests_prelaunch_and_trading_separately(self) -> None:
        seen: list[dict[str, str]] = []

        def fetch(surface, params):  # noqa: ANN001
            seen.append(dict(params))
            return {
                "retCode": 0,
                "result": {"category": "linear", "list": []},
            }

        results = registry.fetch_required_surfaces("bybit", fetch)

        self.assertEqual(
            seen,
            [
                {"category": "linear", "status": "PreLaunch", "limit": "1000"},
                {"category": "linear", "status": "Trading", "limit": "1000"},
            ],
        )
        self.assertEqual(
            set(results), {"bybit_linear_prelaunch", "bybit_linear_trading"}
        )

    def test_okx_fetch_requests_swap_and_futures_separately(self) -> None:
        seen: list[dict[str, str]] = []

        def fetch(surface, params):  # noqa: ANN001
            seen.append(dict(params))
            return {
                "code": "0",
                "data": [{
                    "instId": params["instType"],
                    "instType": params["instType"],
                    "state": "live",
                    "ruleType": "normal",
                }],
            }

        results = registry.fetch_required_surfaces("okx", fetch)

        self.assertEqual(seen, [{"instType": "SWAP"}, {"instType": "FUTURES"}])
        self.assertEqual(set(results), {"okx_swap", "okx_futures"})

    def test_okx_surface_rejects_the_wrong_instrument_type(self) -> None:
        surface = next(item for item in registry.SURFACES if item.surface_id == "okx_swap")
        payload = {
            "code": "0",
            "data": [{
                "instId": "ABC-USDT-260930",
                "instType": "FUTURES",
                "state": "live",
                "ruleType": "normal",
            }],
        }

        with self.assertRaisesRegex(registry.EventRegistryError, "surface.*SWAP"):
            registry.fetch_surface(surface, lambda _surface, _params: payload)

    def test_bybit_surface_rejects_a_dated_linear_future(self) -> None:
        surface = next(
            item
            for item in registry.SURFACES
            if item.surface_id == "bybit_linear_prelaunch"
        )
        payload = {
            "retCode": 0,
            "result": {
                "category": "linear",
                "list": [
                    {
                        "symbol": "DATEDUSDT",
                        "contractType": "LinearFutures",
                        "status": "PreLaunch",
                        "isPreListing": True,
                        "launchTime": str(NOW * 1000),
                    }
                ],
            },
        }

        with self.assertRaisesRegex(registry.EventRegistryError, "LinearPerpetual"):
            registry.fetch_surface(surface, lambda _surface, _params: payload)

    def test_bybit_success_code_without_result_list_is_not_an_empty_surface(self) -> None:
        surface = next(
            item
            for item in registry.SURFACES
            if item.surface_id == "bybit_linear_prelaunch"
        )

        with self.assertRaisesRegex(registry.EventRegistryError, "result.list"):
            registry.fetch_surface(
                surface, lambda _surface, _params: {"retCode": 0, "result": {}}
            )

    def test_bybit_success_code_rejects_boolean_and_float_zero(self) -> None:
        surface = next(
            item
            for item in registry.SURFACES
            if item.surface_id == "bybit_linear_prelaunch"
        )
        for ret_code in (False, 0.0):
            with self.subTest(ret_code=ret_code):
                payload = {
                    "retCode": ret_code,
                    "result": {"category": "linear", "list": []},
                }
                with self.assertRaisesRegex(
                    registry.EventRegistryError, "successful retCode"
                ):
                    registry.fetch_surface(
                        surface, lambda _surface, _params, value=payload: value
                    )

    def test_bybit_surface_requires_exact_linear_category(self) -> None:
        surface = next(
            item
            for item in registry.SURFACES
            if item.surface_id == "bybit_linear_prelaunch"
        )
        for category in (None, "", "inverse"):
            with self.subTest(category=category):
                payload = {
                    "retCode": 0,
                    "result": {"category": category, "list": []},
                }
                with self.assertRaisesRegex(
                    registry.EventRegistryError, "category is not linear"
                ):
                    registry.fetch_surface(
                        surface, lambda _surface, _params, value=payload: value
                    )

    def test_bybit_rows_must_match_the_queried_surface(self) -> None:
        prelaunch = next(
            item
            for item in registry.SURFACES
            if item.surface_id == "bybit_linear_prelaunch"
        )
        trading = next(
            item
            for item in registry.SURFACES
            if item.surface_id == "bybit_linear_trading"
        )
        cases = (
            (prelaunch, "Trading", False),
            (prelaunch, "PreLaunch", False),
            (trading, "PreLaunch", True),
            (trading, "Trading", True),
        )
        for surface, status, is_prelisting in cases:
            with self.subTest(
                surface=surface.surface_id,
                status=status,
                is_prelisting=is_prelisting,
            ):
                payload = {
                    "retCode": 0,
                    "result": {
                        "category": "linear",
                        "list": [{
                            "symbol": "ABCUSDT",
                            "contractType": "LinearPerpetual",
                            "status": status,
                            "isPreListing": is_prelisting,
                            "launchTime": str(NOW * 1000),
                        }],
                    },
                }
                with self.assertRaisesRegex(
                    registry.EventRegistryError, "does not match queried surface"
                ):
                    registry.fetch_surface(
                        surface, lambda _surface, _params, value=payload: value
                    )

    def test_okx_success_code_without_data_is_not_an_empty_surface(self) -> None:
        surface = next(item for item in registry.SURFACES if item.surface_id == "okx_swap")

        with self.assertRaisesRegex(registry.EventRegistryError, "data array"):
            registry.fetch_surface(
                surface, lambda _surface, _params: {"code": "0"}
            )

    def test_mixed_valid_and_non_object_rows_fail_every_surface(self) -> None:
        cases = (
            (
                "bybit_linear_prelaunch",
                {
                    "retCode": 0,
                    "result": {
                        "category": "linear",
                        "list": [
                            {
                                "symbol": "VALIDUSDT",
                                "contractType": "LinearPerpetual",
                            },
                            None,
                        ],
                    },
                },
            ),
            (
                "okx_swap",
                {
                    "code": "0",
                    "data": [
                        {"instId": "VALID-USDT-SWAP", "instType": "SWAP"},
                        None,
                    ],
                },
            ),
            (
                "gate_usdt_contracts",
                [{"name": "VALID_USDT"}, None],
            ),
        )
        for surface_id, payload in cases:
            with self.subTest(surface_id=surface_id):
                surface = next(
                    item for item in registry.SURFACES if item.surface_id == surface_id
                )
                with self.assertRaisesRegex(
                    registry.EventRegistryError, "non-object element"
                ):
                    registry.fetch_surface(
                        surface, lambda _surface, _params, value=payload: value
                    )

    def test_missing_native_id_fails_every_surface(self) -> None:
        cases = (
            (
                "bybit_linear_prelaunch",
                {
                    "retCode": 0,
                    "result": {
                        "category": "linear",
                        "list": [{"contractType": "LinearPerpetual"}],
                    },
                },
                "symbol",
            ),
            (
                "okx_swap",
                {"code": "0", "data": [{"instType": "SWAP"}]},
                "instId",
            ),
            ("gate_usdt_contracts", [{"status": "trading"}], "name"),
        )
        for surface_id, payload, native_field in cases:
            with self.subTest(surface_id=surface_id):
                surface = next(
                    item for item in registry.SURFACES if item.surface_id == surface_id
                )
                with self.assertRaisesRegex(
                    registry.EventRegistryError, f"canonical {native_field}"
                ):
                    registry.fetch_surface(
                        surface, lambda _surface, _params, value=payload: value
                    )

    def test_gate_boolean_creation_timestamp_is_acquisition_failure(self) -> None:
        surface = next(
            item
            for item in registry.SURFACES
            if item.surface_id == "gate_usdt_contracts"
        )
        payload = [{
            "name": "ABC_USDT",
            "status": "trading",
            "is_pre_market": True,
            "create_time": True,
        }]

        with self.assertRaisesRegex(
            registry.EventRegistryError, "invalid create_time"
        ):
            registry.fetch_surface(surface, lambda _surface, _params: payload)


class LifecycleClassificationTests(unittest.TestCase):
    @staticmethod
    def surface(surface_id: str):
        return next(item for item in registry.SURFACES if item.surface_id == surface_id)

    def test_okx_live_pre_market_swap_is_active(self) -> None:
        observation = registry.classify_lifecycle(
            self.surface("okx_swap"),
            {
                "instId": "ABC-USDT-SWAP",
                "instType": "SWAP",
                "uly": "ABC-USDT",
                "ruleType": "pre_market",
                "state": "live",
                "listTime": str((NOW - 60) * 1000),
            },
            now_ts=NOW,
            was_tracked=False,
        )

        self.assertEqual(observation.phase, registry.LIFECYCLE_ACTIVE_PREMARKET)
        self.assertFalse(observation.explicit_terminal)

    def test_bybit_trading_surface_terminalizes_a_tracked_premarket(self) -> None:
        observation = registry.classify_lifecycle(
            self.surface("bybit_linear_trading"),
            {
                "symbol": "ABCUSDT",
                "contractType": "LinearPerpetual",
                "status": "Trading",
                "isPreListing": False,
                "launchTime": str((NOW - 60) * 1000),
            },
            now_ts=NOW,
            was_tracked=True,
        )

        self.assertEqual(observation.phase, registry.LIFECYCLE_TRANSITIONED_STANDARD)
        self.assertTrue(observation.explicit_terminal)

    def test_okx_live_pre_market_future_is_active_premarket(self) -> None:
        observation = registry.classify_lifecycle(
            self.surface("okx_futures"),
            {
                "instId": "ABC-USDT-260930",
                "instType": "FUTURES",
                "uly": "ABC-USDT",
                "ruleType": "pre_market",
                "state": "live",
                "listTime": str((NOW - 60) * 1000),
            },
            now_ts=NOW,
            was_tracked=False,
        )

        self.assertEqual(observation.phase, registry.LIFECYCLE_ACTIVE_PREMARKET)

    def test_okx_future_switch_time_is_transition_scheduled_before_it_occurs(self) -> None:
        observation = registry.classify_lifecycle(
            self.surface("okx_futures"),
            {
                "instId": "ABC-USDT-260930",
                "instType": "FUTURES",
                "uly": "ABC-USDT",
                "ruleType": "pre_market",
                "state": "live",
                "listTime": str((NOW - 60) * 1000),
                "preMktSwTime": str((NOW + 300) * 1000),
            },
            now_ts=NOW,
            was_tracked=True,
        )

        self.assertEqual(observation.phase, registry.LIFECYCLE_TRANSITION_SCHEDULED)
        self.assertFalse(observation.explicit_terminal)

    def test_okx_xperp_is_transitioned_not_a_new_premarket(self) -> None:
        observation = registry.classify_lifecycle(
            self.surface("okx_futures"),
            {
                "instId": "ABC-USDT-260930",
                "instType": "FUTURES",
                "uly": "ABC-USDT",
                "ruleType": "xperp",
                "state": "live",
                "listTime": str((NOW - 3600) * 1000),
            },
            now_ts=NOW,
            was_tracked=True,
        )

        self.assertEqual(observation.phase, registry.LIFECYCLE_TRANSITIONED_STANDARD)
        self.assertTrue(observation.explicit_terminal)

    def test_explicit_okx_transition_timestamp_is_bound_to_tracked_generation(self) -> None:
        target_surface = self.surface("okx_futures")
        row = {
            "instId": "ABC-USDT-260930",
            "instType": "FUTURES",
            "uly": "ABC-USDT",
            "instCategory": "1",
            "ruleType": "xperp",
            "state": "live",
            "listTime": str((NOW - 3600) * 1000),
            "preMktSwTime": str((NOW - 10) * 1000),
        }
        rows_by_surface = {surface.surface_id: [] for surface in registry.SURFACES}
        rows_by_surface[target_surface.surface_id] = [row]
        snapshots = {
            surface.surface_id: registry.build_relevant_identity_snapshot(
                surface,
                rows_by_surface[surface.surface_id],
                now_ts=NOW,
                tracked_ids=("ABC-USDT-260930",)
                if surface is target_surface
                else (),
            )
            for surface in registry.SURFACES
        }

        observations = registry.build_terminal_lifecycle_observations(
            identity_snapshots=snapshots,
            rows_by_surface=rows_by_surface,
            previous_active={"okx": {"ABC-USDT-260930": 0}},
            received_at_utc="2027-01-15T08:00:00Z",
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["transition_ts"], NOW - 10)
        self.assertEqual(
            observations[0]["source_class"],
            registry.SOURCE_VENUE_INSTRUMENT_METADATA,
        )
        self.assertEqual(
            observations[0]["lifecycle_phase"],
            registry.LIFECYCLE_TRANSITIONED_STANDARD,
        )
        self.assertNotIn(
            "DETECTION_TIME_PROXY_NOT_ACTUAL_TRANSITION_TIME",
            observations[0]["caveats"],
        )

    def test_exact_okx_transition_wins_over_simultaneous_terminal_proxy(self) -> None:
        swap_surface = self.surface("okx_swap")
        futures_surface = self.surface("okx_futures")
        native_id = "ABC-USDT-260930"
        rows_by_surface = {surface.surface_id: [] for surface in registry.SURFACES}
        rows_by_surface[swap_surface.surface_id] = [{
            "instId": native_id,
            "instType": "SWAP",
            "uly": "ABC-USDT",
            "instCategory": "1",
            "ruleType": "pre_market",
            "state": "expired",
            "listTime": str((NOW - 3600) * 1000),
        }]
        rows_by_surface[futures_surface.surface_id] = [{
            "instId": native_id,
            "instType": "FUTURES",
            "uly": "ABC-USDT",
            "instCategory": "1",
            "ruleType": "xperp",
            "state": "live",
            "listTime": str((NOW - 3600) * 1000),
            "preMktSwTime": str((NOW - 10) * 1000),
        }]
        snapshots = {
            surface.surface_id: registry.build_relevant_identity_snapshot(
                surface,
                rows_by_surface[surface.surface_id],
                now_ts=NOW,
                tracked_ids=(native_id,) if surface.venue == "okx" else (),
            )
            for surface in registry.SURFACES
        }

        observations = registry.build_terminal_lifecycle_observations(
            identity_snapshots=snapshots,
            rows_by_surface=rows_by_surface,
            previous_active={"okx": {native_id: 4}},
            received_at_utc="2027-01-15T08:00:00Z",
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["transition_ts"], NOW - 10)
        self.assertEqual(
            observations[0]["source_class"],
            registry.SOURCE_VENUE_INSTRUMENT_METADATA,
        )
        self.assertEqual(
            observations[0]["lifecycle_phase"],
            registry.LIFECYCLE_TRANSITIONED_STANDARD,
        )
        self.assertNotIn(
            "DETECTION_TIME_PROXY_NOT_ACTUAL_TRANSITION_TIME",
            observations[0]["caveats"],
        )

    def test_expired_okx_xperp_retains_its_exact_transition_timestamp(self) -> None:
        target_surface = self.surface("okx_futures")
        native_id = "ABC-USDT-260930"
        row = {
            "instId": native_id,
            "instType": "FUTURES",
            "uly": "ABC-USDT",
            "instCategory": "1",
            "ruleType": "xperp",
            "state": "expired",
            "listTime": str((NOW - 3600) * 1000),
            "preMktSwTime": str((NOW - 10) * 1000),
        }
        rows_by_surface = {surface.surface_id: [] for surface in registry.SURFACES}
        rows_by_surface[target_surface.surface_id] = [row]
        snapshots = {
            surface.surface_id: registry.build_relevant_identity_snapshot(
                surface,
                rows_by_surface[surface.surface_id],
                now_ts=NOW,
                tracked_ids=(native_id,) if surface is target_surface else (),
            )
            for surface in registry.SURFACES
        }

        observations = registry.build_terminal_lifecycle_observations(
            identity_snapshots=snapshots,
            rows_by_surface=rows_by_surface,
            previous_active={"okx": {native_id: 7}},
            received_at_utc="2027-01-15T08:00:00Z",
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["transition_ts"], NOW - 10)
        self.assertEqual(
            observations[0]["source_class"],
            registry.SOURCE_VENUE_INSTRUMENT_METADATA,
        )
        self.assertEqual(
            observations[0]["lifecycle_phase"],
            registry.LIFECYCLE_TRANSITIONED_STANDARD,
        )

    def test_gate_terminal_reason_is_an_append_only_registry_observation(self) -> None:
        target_surface = self.surface("gate_usdt_contracts")
        row = {
            "name": "ABC_USDT",
            "status": "delisted",
            "is_pre_market": True,
            "create_time": NOW - 3600,
        }
        rows_by_surface = {surface.surface_id: [] for surface in registry.SURFACES}
        rows_by_surface[target_surface.surface_id] = [row]
        snapshots = {
            surface.surface_id: registry.build_relevant_identity_snapshot(
                surface,
                rows_by_surface[surface.surface_id],
                now_ts=NOW,
                tracked_ids=("ABC_USDT",) if surface is target_surface else (),
            )
            for surface in registry.SURFACES
        }

        observations = registry.build_terminal_lifecycle_observations(
            identity_snapshots=snapshots,
            rows_by_surface=rows_by_surface,
            previous_active={"gate": {"ABC_USDT": 2}},
            received_at_utc="2027-01-15T08:00:00Z",
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["timestamp_kind"], registry.TIMESTAMP_TRANSITION)
        self.assertEqual(observations[0]["transition_ts"], NOW)
        self.assertEqual(observations[0]["lifecycle_generation"], 2)
        self.assertEqual(observations[0]["lifecycle_phase"], registry.LIFECYCLE_DELISTED)
        self.assertIn(
            "DETECTION_TIME_PROXY_NOT_ACTUAL_TERMINAL_TIME",
            observations[0]["caveats"],
        )

        records = registry.build_stream_revisions([], observations)
        episode = registry.materialize_episodes(records)[0]
        terminal = next(
            item
            for item in episode["timestamp_observations"]
            if item["timestamp_kind"] == registry.TIMESTAMP_TRANSITION
        )
        self.assertEqual(terminal["lifecycle_phase"], registry.LIFECYCLE_DELISTED)

    def test_cross_surface_terminal_observation_wins_over_stale_active_row(self) -> None:
        prelaunch_surface = self.surface("bybit_linear_prelaunch")
        trading_surface = self.surface("bybit_linear_trading")
        rows_by_surface = {surface.surface_id: [] for surface in registry.SURFACES}
        rows_by_surface[prelaunch_surface.surface_id] = [{
            "symbol": "ABCUSDT",
            "baseCoin": "ABC",
            "contractType": "LinearPerpetual",
            "status": "PreLaunch",
            "isPreListing": True,
            "launchTime": str((NOW - 60) * 1000),
        }]
        rows_by_surface[trading_surface.surface_id] = [{
            "symbol": "ABCUSDT",
            "baseCoin": "ABC",
            "contractType": "LinearPerpetual",
            "status": "Trading",
            "isPreListing": False,
            "launchTime": str((NOW - 60) * 1000),
        }]
        snapshots = {
            surface.surface_id: registry.build_relevant_identity_snapshot(
                surface,
                rows_by_surface[surface.surface_id],
                now_ts=NOW,
                tracked_ids=("ABCUSDT",)
                if surface.venue == "bybit"
                else (),
            )
            for surface in registry.SURFACES
        }
        current_active = {
            "bybit": ["ABCUSDT"],
            "okx": [],
            "gate": [],
        }
        relevant = {
            surface_id: list(snapshot.relevant_ids)
            for surface_id, snapshot in snapshots.items()
        }

        resolved_active, resolved_relevant = (
            registry.apply_cross_surface_terminal_precedence(
                identity_snapshots=snapshots,
                current_active=current_active,
                relevant_identity_ids_by_surface=relevant,
            )
        )

        self.assertEqual(resolved_active["bybit"], [])
        self.assertNotIn(
            "ABCUSDT", resolved_relevant["bybit_linear_prelaunch"]
        )

    def test_gate_terminal_status_overrides_pre_market_flag(self) -> None:
        observation = registry.classify_lifecycle(
            self.surface("gate_usdt_contracts"),
            {
                "name": "ABC_USDT",
                "status": "delisted",
                "is_pre_market": True,
                "create_time": NOW - 3600,
                "delisted_time": NOW - 10,
            },
            now_ts=NOW,
            was_tracked=True,
        )

        self.assertEqual(observation.phase, registry.LIFECYCLE_DELISTED)
        self.assertTrue(observation.explicit_terminal)

    def test_gate_in_delisting_overrides_trading(self) -> None:
        observation = registry.classify_lifecycle(
            self.surface("gate_usdt_contracts"),
            {
                "name": "ABC_USDT",
                "status": "trading",
                "is_pre_market": True,
                "in_delisting": True,
                "create_time": NOW - 3600,
            },
            now_ts=NOW,
            was_tracked=True,
        )

        self.assertEqual(observation.phase, registry.LIFECYCLE_DELISTING)
        self.assertTrue(observation.explicit_terminal)

    def test_gate_unknown_tracked_state_fails_closed(self) -> None:
        observation = registry.classify_lifecycle(
            self.surface("gate_usdt_contracts"),
            {
                "name": "ABC_USDT",
                "status": "mystery",
                "is_pre_market": True,
                "create_time": NOW - 3600,
            },
            now_ts=NOW,
            was_tracked=True,
        )

        self.assertEqual(observation.phase, registry.LIFECYCLE_UNKNOWN)
        self.assertFalse(observation.explicit_terminal)


class RuntimeAdapterSemanticsTests(unittest.TestCase):
    @staticmethod
    def adapter(venue: str):
        return next(item for item in registry.ADAPTERS if item.venue == venue)

    def test_untracked_terminal_rows_cannot_create_historical_episodes(self) -> None:
        observed_at = "2027-01-15T08:00:00Z"
        rows_by_venue = {
            "bybit": [{
                "symbol": "OLDUSDT",
                "baseCoin": "OLD",
                "symbolType": "innovation",
                "launchTime": str((NOW - 3600) * 1000),
                "status": "Closed",
                "contractType": "LinearPerpetual",
                "isPreListing": False,
            }],
            "okx": [{
                "instId": "OLD-USDT-260930",
                "instType": "FUTURES",
                "uly": "OLD-USDT",
                "instCategory": "1",
                "ruleType": "xperp",
                "state": "live",
                "listTime": str((NOW - 3600) * 1000),
                "preMktSwTime": str((NOW - 60) * 1000),
            }],
            "gate": [{
                "name": "OLD_USDT",
                "status": "delisted",
                "is_pre_market": True,
                "contract_type": "crypto",
                "create_time": NOW - 3600,
            }],
        }

        for venue, rows in rows_by_venue.items():
            with self.subTest(venue=venue):
                self.assertEqual(
                    registry.normalise_rows(
                        self.adapter(venue),
                        rows,
                        observed_at_utc=observed_at,
                    ),
                    [],
                )

    def test_gate_launch_time_is_not_treated_as_trading_launch(self) -> None:
        adapter = self.adapter("gate")

        self.assertEqual(adapter.t0_field, "create_time")
        self.assertEqual(adapter.t0_kind, registry.TIMESTAMP_CONTRACT_CREATED)
        self.assertIn("CONTRACT_CREATION_NOT_TRADING_START", adapter.caveats)

    def test_normalised_row_carries_explicit_asset_identity(self) -> None:
        events = registry.normalise_rows(
            self.adapter("bybit"),
            [{
                "symbol": "ABCUSDT",
                "baseCoin": "ABC",
                "symbolType": "innovation",
                "launchTime": str(NOW * 1000),
                "status": "PreLaunch",
                "contractType": "LinearPerpetual",
                "isPreListing": True,
            }],
            observed_at_utc="2026-08-23T00:00:00Z",
        )

        self.assertEqual(events[0]["asset_class"], registry.ASSET_CLASS_CRYPTO_TOKEN)
        self.assertEqual(events[0]["issuer_id"], "ABC")


if __name__ == "__main__":
    unittest.main()
