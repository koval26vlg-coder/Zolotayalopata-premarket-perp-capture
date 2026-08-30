"""Networkless contract tests for v43 public venue WebSocket schemas."""

from __future__ import annotations

import ast
import inspect
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    import venue_ws_v43  # type: ignore[import-not-found]  # noqa: E402
except ModuleNotFoundError:
    venue_ws_v43 = None


class VenueWsModulePresenceTests(unittest.TestCase):
    def test_venue_schema_module_exists(self) -> None:
        self.assertIsNotNone(venue_ws_v43, "src/venue_ws_v43.py has not been implemented")


@unittest.skipIf(venue_ws_v43 is None, "venue_ws_v43 implementation absent")
class ProfileAndSubscriptionTests(unittest.TestCase):
    def test_profiles_pin_exact_official_connections_and_channels(self) -> None:
        bybit = venue_ws_v43.venue_profile("bybit")
        bybit_public = bybit.connection("public_linear")
        self.assertEqual(
            (bybit_public.host, bybit_public.port, bybit_public.path),
            ("stream.bybit.com", 443, "/v5/public/linear"),
        )
        self.assertEqual(
            bybit_public.channels,
            frozenset({"orderbook.50", "publicTrade", "tickers"}),
        )

        okx = venue_ws_v43.venue_profile("okx")
        okx_public = okx.connection("public")
        okx_business = okx.connection("business")
        self.assertEqual(
            (okx_public.host, okx_public.port, okx_public.path),
            ("ws.okx.com", 8443, "/ws/v5/public"),
        )
        self.assertEqual(
            (okx_business.host, okx_business.port, okx_business.path),
            ("ws.okx.com", 8443, "/ws/v5/business"),
        )
        self.assertEqual(
            okx_public.channels,
            frozenset(
                {
                    "books",
                    "tickers",
                    "mark-price",
                    "index-tickers",
                    "funding-rate",
                    "open-interest",
                    "price-limit",
                }
            ),
        )
        self.assertEqual(okx_business.channels, frozenset({"trades-all"}))
        self.assertFalse(okx_public.transport_443_compatible)
        self.assertTrue(okx_public.transport_supported)
        self.assertTrue(okx_business.transport_supported)
        self.assertIsNone(okx_public.transport_blocker)

        gate = venue_ws_v43.venue_profile("gate")
        gate_public = gate.connection("public_usdt")
        self.assertEqual(
            (gate_public.host, gate_public.port, gate_public.path),
            ("fx-ws.gateio.ws", 443, "/v4/ws/usdt"),
        )
        self.assertEqual(
            gate_public.channels,
            frozenset(
                {
                    "futures.order_book_update",
                    "futures.book_ticker",
                    "futures.trades",
                    "futures.tickers",
                    "futures.contract_stats",
                }
            ),
        )
        self.assertEqual(
            gate_public.book_bootstrap,
            "REST_SNAPSHOT_REQUIRED_UNLESS_FULL_FRAME",
        )

        for profile in (bybit, okx, gate):
            for connection in profile.connections:
                with self.subTest(venue=profile.venue, connection=connection.name):
                    self.assertTrue(connection.public_only)
                    self.assertFalse(connection.connection_headers_allowed)
                    self.assertFalse(connection.query_allowed)
                    self.assertNotIn("?", connection.path)
                    self.assertNotIn("#", connection.path)

    def test_bybit_builder_is_contract_bound(self) -> None:
        requests = venue_ws_v43.build_subscriptions(
            "bybit", "ABCUSDT", request_id="req1"
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].connection, "public_linear")
        self.assertEqual(
            requests[0].message,
            {
                "req_id": "req1",
                "op": "subscribe",
                "args": [
                    "orderbook.50.ABCUSDT",
                    "publicTrade.ABCUSDT",
                    "tickers.ABCUSDT",
                ],
            },
        )

    def test_okx_builder_separates_public_and_business_connections(self) -> None:
        requests = venue_ws_v43.build_subscriptions(
            "okx",
            "ABC-USDT-SWAP",
            index_id="ABC-USDT",
            request_id="req2",
        )
        self.assertEqual([request.connection for request in requests], ["public", "business"])
        public_args = requests[0].message["args"]
        self.assertEqual(
            public_args,
            [
                {"channel": "books", "instId": "ABC-USDT-SWAP"},
                {"channel": "tickers", "instId": "ABC-USDT-SWAP"},
                {"channel": "mark-price", "instId": "ABC-USDT-SWAP"},
                {"channel": "index-tickers", "instId": "ABC-USDT"},
                {"channel": "funding-rate", "instId": "ABC-USDT-SWAP"},
                {"channel": "open-interest", "instId": "ABC-USDT-SWAP"},
                {"channel": "price-limit", "instId": "ABC-USDT-SWAP"},
            ],
        )
        self.assertEqual(
            requests[1].message,
            {
                "id": "req2",
                "op": "subscribe",
                "args": [{"channel": "trades-all", "instId": "ABC-USDT-SWAP"}],
            },
        )

    def test_gate_builder_uses_only_public_contract_channels(self) -> None:
        requests = venue_ws_v43.build_subscriptions(
            "gate", "ABC_USDT", request_time_sec=1_700_000_000
        )
        self.assertEqual({request.connection for request in requests}, {"public_usdt"})
        self.assertEqual(
            [request.message["channel"] for request in requests],
            [
                "futures.order_book_update",
                "futures.book_ticker",
                "futures.trades",
                "futures.tickers",
                "futures.contract_stats",
            ],
        )
        self.assertEqual(requests[0].message["payload"], ["ABC_USDT", "100ms", "100"])
        self.assertEqual(requests[-1].message["payload"], ["ABC_USDT", "1m"])
        for request in requests:
            self.assertEqual(request.message["event"], "subscribe")
            self.assertEqual(request.message["time"], 1_700_000_000)

    def test_builders_reject_unbound_or_malformed_identifiers(self) -> None:
        bad_calls = (
            lambda: venue_ws_v43.build_subscriptions("bybit", "*", request_id="x"),
            lambda: venue_ws_v43.build_subscriptions("bybit", "abcusdt", request_id="x"),
            lambda: venue_ws_v43.build_subscriptions(
                "okx", "ABC-USDT-SWAP", index_id="WRONG-USDT", request_id="x"
            ),
            lambda: venue_ws_v43.build_subscriptions(
                "okx", "ABC-USDT-SWAP", index_id="ABC-USDT", request_id="x!"
            ),
            lambda: venue_ws_v43.build_subscriptions(
                "gate", "ABC_USDT", request_time_sec=True
            ),
            lambda: venue_ws_v43.build_subscriptions(
                "gate", "ABC_USDT", request_time_sec=-1
            ),
            lambda: venue_ws_v43.build_subscriptions("unknown", "ABC"),
        )
        for call in bad_calls:
            with self.subTest(call=call), self.assertRaises(venue_ws_v43.VenueSchemaError):
                call()

    def test_builder_api_has_no_network_header_query_or_channel_override(self) -> None:
        parameters = inspect.signature(venue_ws_v43.build_subscriptions).parameters
        for forbidden in ("headers", "query", "url", "channel", "channels", "auth"):
            with self.subTest(parameter=forbidden):
                self.assertNotIn(forbidden, parameters)


@unittest.skipIf(venue_ws_v43 is None, "venue_ws_v43 implementation absent")
class BybitParserTests(unittest.TestCase):
    def test_order_book_snapshot_preserves_sequence_and_reset_signal(self) -> None:
        message = {
            "topic": "orderbook.50.ABCUSDT",
            "type": "snapshot",
            "ts": 1_700_000_000_010,
            "cts": 1_700_000_000_005,
            "data": {
                "s": "ABCUSDT",
                "b": [["10.0", "2"], ["9.5", "3"]],
                "a": [["10.5", "4"]],
                "u": 100,
                "seq": 900,
            },
        }
        event = venue_ws_v43.parse_message(
            "bybit", message, contract="ABCUSDT", connection="public_linear"
        )[0]
        self.assertEqual((event.kind, event.action), ("book", "snapshot"))
        self.assertEqual((event.exchange_ts_ms, event.gateway_ts_ms), (1_700_000_000_005, 1_700_000_000_010))
        self.assertEqual((event.sequence_end, event.cross_sequence), (100, 900))
        self.assertEqual(event.gap_signal, venue_ws_v43.GAP_RESET)
        self.assertEqual(event.bids, (("10.0", "2"), ("9.5", "3")))
        self.assertEqual(event.asks, (("10.5", "4"),))

    def test_delta_does_not_claim_continuity_by_guessing_update_ids(self) -> None:
        message = {
            "topic": "orderbook.50.ABCUSDT",
            "type": "delta",
            "ts": 11,
            "cts": 10,
            "data": {"s": "ABCUSDT", "b": [["10", "0"]], "a": [], "u": 101, "seq": 901},
        }
        event = venue_ws_v43.parse_message(
            "bybit",
            message,
            contract="ABCUSDT",
            connection="public_linear",
            last_sequence=100,
        )[0]
        self.assertEqual(event.gap_signal, venue_ws_v43.GAP_CONTINUITY_UNVERIFIABLE)
        stale = dict(message)
        stale["data"] = dict(message["data"], u=100)
        stale_event = venue_ws_v43.parse_message(
            "bybit",
            stale,
            contract="ABCUSDT",
            connection="public_linear",
            last_sequence=100,
        )[0]
        self.assertEqual(stale_event.gap_signal, venue_ws_v43.GAP_STALE)

    def test_trades_and_ticker_are_normalized(self) -> None:
        trades = {
            "topic": "publicTrade.ABCUSDT",
            "type": "snapshot",
            "ts": 20,
            "data": [
                {"T": 18, "s": "ABCUSDT", "S": "Buy", "v": "2", "p": "10", "i": "t1", "seq": 77},
                {"T": 19, "s": "ABCUSDT", "S": "Sell", "v": "1", "p": "11", "i": "t2", "seq": 77},
            ],
        }
        events = venue_ws_v43.parse_message(
            "bybit", trades, contract="ABCUSDT", connection="public_linear"
        )
        self.assertEqual([(e.fields["side"], e.fields["trade_id"]) for e in events], [("buy", "t1"), ("sell", "t2")])
        self.assertEqual([e.cross_sequence for e in events], [77, 77])

        ticker = {
            "topic": "tickers.ABCUSDT",
            "type": "snapshot",
            "cs": 78,
            "ts": 21,
            "data": {
                "symbol": "ABCUSDT",
                "lastPrice": "10.2",
                "bid1Price": "10.1",
                "ask1Price": "10.3",
                "markPrice": "10.15",
                "indexPrice": "10.12",
                "fundingRate": "0.0001",
                "openInterest": "500",
            },
        }
        event = venue_ws_v43.parse_message(
            "bybit", ticker, contract="ABCUSDT", connection="public_linear"
        )[0]
        self.assertEqual(event.kind, "ticker")
        self.assertEqual(event.cross_sequence, 78)
        self.assertEqual(
            {key: event.fields[key] for key in ("last_price", "mark_price", "index_price", "funding_rate", "open_interest")},
            {
                "last_price": "10.2",
                "mark_price": "10.15",
                "index_price": "10.12",
                "funding_rate": "0.0001",
                "open_interest": "500",
            },
        )


@unittest.skipIf(venue_ws_v43 is None, "venue_ws_v43 implementation absent")
class OkxParserTests(unittest.TestCase):
    def _book(self, *, action="update", previous=100, current=101):
        return {
            "arg": {"channel": "books", "instId": "ABC-USDT-SWAP"},
            "action": action,
            "data": [
                {
                    "asks": [["10.5", "4", "0", "1"]],
                    "bids": [["10.0", "2", "0", "1"]],
                    "ts": "1000",
                    "checksum": 0,
                    "prevSeqId": previous,
                    "seqId": current,
                }
            ],
        }

    def test_books_sequence_continuity_gap_and_reset_are_explicit(self) -> None:
        snapshot = venue_ws_v43.parse_message(
            "okx",
            self._book(action="snapshot", previous=-1, current=100),
            contract="ABC-USDT-SWAP",
            index_id="ABC-USDT",
            connection="public",
        )[0]
        self.assertEqual(snapshot.gap_signal, venue_ws_v43.GAP_RESET)
        self.assertEqual((snapshot.previous_sequence, snapshot.sequence_end), (-1, 100))

        continuous = venue_ws_v43.parse_message(
            "okx",
            self._book(),
            contract="ABC-USDT-SWAP",
            index_id="ABC-USDT",
            connection="public",
            last_sequence=100,
        )[0]
        self.assertEqual(continuous.gap_signal, venue_ws_v43.GAP_NONE)

        gap = venue_ws_v43.parse_message(
            "okx",
            self._book(previous=99, current=101),
            contract="ABC-USDT-SWAP",
            index_id="ABC-USDT",
            connection="public",
            last_sequence=100,
        )[0]
        self.assertEqual(gap.gap_signal, venue_ws_v43.GAP_DETECTED)

        reset = venue_ws_v43.parse_message(
            "okx",
            self._book(previous=100, current=50),
            contract="ABC-USDT-SWAP",
            index_id="ABC-USDT",
            connection="public",
            last_sequence=100,
        )[0]
        self.assertEqual(reset.gap_signal, venue_ws_v43.GAP_RESET)

    def test_public_metrics_and_business_trades_are_normalized(self) -> None:
        cases = (
            ("tickers", "ABC-USDT-SWAP", {"instId": "ABC-USDT-SWAP", "last": "10", "bidPx": "9", "askPx": "11", "ts": "1"}, "ticker", "last_price", "10"),
            ("mark-price", "ABC-USDT-SWAP", {"instId": "ABC-USDT-SWAP", "markPx": "10.1", "ts": "2"}, "mark", "mark_price", "10.1"),
            ("index-tickers", "ABC-USDT", {"instId": "ABC-USDT", "idxPx": "10.2", "ts": "3"}, "index", "index_price", "10.2"),
            ("funding-rate", "ABC-USDT-SWAP", {"instId": "ABC-USDT-SWAP", "fundingRate": "0.001", "nextFundingTime": "99", "ts": "4"}, "funding", "funding_rate", "0.001"),
            ("open-interest", "ABC-USDT-SWAP", {"instId": "ABC-USDT-SWAP", "oi": "500", "oiCcy": "50", "ts": "5"}, "open_interest", "open_interest", "500"),
            ("price-limit", "ABC-USDT-SWAP", {"instId": "ABC-USDT-SWAP", "buyLmt": "12", "sellLmt": "8", "enabled": True, "ts": "6"}, "price_limit", "buy_limit", "12"),
        )
        for channel, source_id, row, kind, field, expected in cases:
            message = {"arg": {"channel": channel, "instId": source_id}, "data": [row]}
            with self.subTest(channel=channel):
                event = venue_ws_v43.parse_message(
                    "okx",
                    message,
                    contract="ABC-USDT-SWAP",
                    index_id="ABC-USDT",
                    connection="public",
                )[0]
                self.assertEqual((event.kind, event.fields[field]), (kind, expected))

        trade_message = {
            "arg": {"channel": "trades-all", "instId": "ABC-USDT-SWAP"},
            "data": [{"instId": "ABC-USDT-SWAP", "tradeId": "t1", "px": "10", "sz": "2", "side": "buy", "ts": "7"}],
        }
        trade = venue_ws_v43.parse_message(
            "okx",
            trade_message,
            contract="ABC-USDT-SWAP",
            index_id="ABC-USDT",
            connection="business",
        )[0]
        self.assertEqual((trade.kind, trade.fields["trade_id"], trade.fields["side"]), ("trade", "t1", "buy"))

    def test_business_channel_on_public_connection_is_rejected(self) -> None:
        message = {
            "arg": {"channel": "trades-all", "instId": "ABC-USDT-SWAP"},
            "data": [{"instId": "ABC-USDT-SWAP", "tradeId": "t1", "px": "10", "sz": "2", "side": "buy", "ts": "7"}],
        }
        with self.assertRaises(venue_ws_v43.UnknownMessage):
            venue_ws_v43.parse_message(
                "okx",
                message,
                contract="ABC-USDT-SWAP",
                index_id="ABC-USDT",
                connection="public",
            )


@unittest.skipIf(venue_ws_v43 is None, "venue_ws_v43 implementation absent")
class GateParserTests(unittest.TestCase):
    def _book(self, *, start=100, end=102, full=None):
        result = {
            "t": 1000,
            "s": "ABC_USDT",
            "U": start,
            "u": end,
            "b": [{"p": "10", "s": "2"}],
            "a": [{"p": "11", "s": "3"}],
            "l": "100",
        }
        if full is not None:
            result["full"] = full
        return {
            "time": 1,
            "time_ms": 1001,
            "channel": "futures.order_book_update",
            "event": "update",
            "result": result,
        }

    def test_delta_requires_rest_seed_then_checks_update_range(self) -> None:
        initial = venue_ws_v43.parse_message(
            "gate", self._book(), contract="ABC_USDT", connection="public_usdt"
        )[0]
        self.assertTrue(initial.rest_snapshot_required)
        self.assertEqual(initial.gap_signal, venue_ws_v43.GAP_REST_SNAPSHOT_REQUIRED)
        self.assertEqual((initial.sequence_start, initial.sequence_end), (100, 102))

        continuous = venue_ws_v43.parse_message(
            "gate",
            self._book(),
            contract="ABC_USDT",
            connection="public_usdt",
            last_sequence=99,
        )[0]
        self.assertEqual(continuous.gap_signal, venue_ws_v43.GAP_NONE)
        self.assertFalse(continuous.rest_snapshot_required)

        gap = venue_ws_v43.parse_message(
            "gate",
            self._book(start=105, end=106),
            contract="ABC_USDT",
            connection="public_usdt",
            last_sequence=102,
        )[0]
        self.assertEqual(gap.gap_signal, venue_ws_v43.GAP_DETECTED)

        full = venue_ws_v43.parse_message(
            "gate",
            self._book(full=True),
            contract="ABC_USDT",
            connection="public_usdt",
        )[0]
        self.assertEqual((full.action, full.gap_signal), ("snapshot", venue_ws_v43.GAP_RESET))
        self.assertFalse(full.rest_snapshot_required)

    def test_gate_trade_ticker_bbo_and_stats_are_normalized(self) -> None:
        messages = (
            (
                {"time_ms": 10, "channel": "futures.trades", "event": "update", "result": [{"size": "-2", "id": 7, "create_time_ms": 9, "price": "10", "contract": "ABC_USDT"}]},
                "trade",
                "side",
                "sell",
            ),
            (
                {"time_ms": 11, "channel": "futures.tickers", "event": "update", "result": [{"contract": "ABC_USDT", "last": "10", "funding_rate": "0.001", "mark_price": "10.1", "index_price": "10.2", "total_size": "500"}]},
                "ticker",
                "mark_price",
                "10.1",
            ),
            (
                {"time_ms": 12, "channel": "futures.book_ticker", "event": "update", "result": {"t": 11, "u": 103, "s": "ABC_USDT", "b": "9", "B": "2", "a": "11", "A": "3"}},
                "bbo",
                "bid_price",
                "9",
            ),
            (
                {"time_ms": 13, "channel": "futures.contract_stats", "event": "update", "result": [{"time": 1, "contract": "ABC_USDT", "open_interest": "600", "mark_price": "10.1"}]},
                "open_interest",
                "open_interest",
                "600",
            ),
        )
        for message, kind, field, expected in messages:
            with self.subTest(channel=message["channel"]):
                event = venue_ws_v43.parse_message(
                    "gate", message, contract="ABC_USDT", connection="public_usdt"
                )[0]
                self.assertEqual((event.kind, event.fields[field]), (kind, expected))


@unittest.skipIf(venue_ws_v43 is None, "venue_ws_v43 implementation absent")
class FailClosedParserAndCapabilityTests(unittest.TestCase):
    def test_negative_public_funding_rates_are_preserved(self) -> None:
        cases = (
            (
                "bybit",
                {
                    "topic": "tickers.ABCUSDT",
                    "type": "delta",
                    "cs": 1,
                    "ts": 1,
                    "data": {"symbol": "ABCUSDT", "fundingRate": "-0.001"},
                },
                {"contract": "ABCUSDT", "connection": "public_linear"},
            ),
            (
                "okx",
                {
                    "arg": {"channel": "funding-rate", "instId": "ABC-USDT-SWAP"},
                    "data": [
                        {
                            "instId": "ABC-USDT-SWAP",
                            "fundingRate": "-0.001",
                            "nextFundingTime": "99",
                            "ts": "2",
                        }
                    ],
                },
                {
                    "contract": "ABC-USDT-SWAP",
                    "index_id": "ABC-USDT",
                    "connection": "public",
                },
            ),
            (
                "gate",
                {
                    "time_ms": 3,
                    "channel": "futures.tickers",
                    "event": "update",
                    "result": [
                        {
                            "contract": "ABC_USDT",
                            "funding_rate": "-0.001",
                            "mark_price": "10",
                        }
                    ],
                },
                {"contract": "ABC_USDT", "connection": "public_usdt"},
            ),
        )
        for venue, message, context in cases:
            with self.subTest(venue=venue):
                event = venue_ws_v43.parse_message(venue, message, **context)[0]
                self.assertEqual(event.fields["funding_rate"], "-0.001")

    def test_unknown_malformed_contract_mismatch_and_nonpublic_messages_reject(self) -> None:
        calls = (
            lambda: venue_ws_v43.parse_message(
                "bybit", {"topic": "position.ABCUSDT", "data": {}}, contract="ABCUSDT", connection="public_linear"
            ),
            lambda: venue_ws_v43.parse_message(
                "okx", {"arg": {"channel": "orders", "instId": "ABC-USDT-SWAP"}, "data": []}, contract="ABC-USDT-SWAP", index_id="ABC-USDT", connection="public"
            ),
            lambda: venue_ws_v43.parse_message(
                "gate", {"channel": "futures.orders", "event": "update", "result": {}}, contract="ABC_USDT", connection="public_usdt"
            ),
            lambda: venue_ws_v43.parse_message(
                "bybit", {"topic": "orderbook.50.ABCUSDT", "type": "delta", "ts": 1, "data": {"s": "ABCUSDT", "b": [], "a": []}}, contract="ABCUSDT", connection="public_linear"
            ),
            lambda: venue_ws_v43.parse_message(
                "bybit", {"topic": "tickers.WRONGUSDT", "type": "snapshot", "ts": 1, "data": {"symbol": "WRONGUSDT"}}, contract="ABCUSDT", connection="public_linear"
            ),
        )
        expected = (
            venue_ws_v43.UnknownMessage,
            venue_ws_v43.UnknownMessage,
            venue_ws_v43.UnknownMessage,
            venue_ws_v43.MalformedMessage,
            venue_ws_v43.ContractMismatch,
        )
        for call, error in zip(calls, expected):
            with self.subTest(error=error), self.assertRaises(error):
                call()

    def test_module_has_no_network_filesystem_or_write_capability(self) -> None:
        source_path = SRC / "venue_ws_v43.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports: set[str] = set()
        calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)
        self.assertTrue(imports.isdisjoint({"socket", "ssl", "urllib", "pathlib", "subprocess", "requests"}))
        self.assertTrue(calls.isdisjoint({"open", "write", "write_text", "write_bytes", "unlink", "replace"}))


if __name__ == "__main__":
    unittest.main()
