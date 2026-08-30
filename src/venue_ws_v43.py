"""Fail-closed public venue schemas for an eventual event-bound v43 capture.

This additive module has no network, filesystem, clock, retry, or state-writing
capability.  It pins official public connection profiles, builds only
contract-bound subscription objects, and normalizes already-decoded messages.
Sequence continuity is claimed only when the venue publishes enough metadata to
prove it; otherwise the normalized event carries an explicit uncertainty signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Iterable, Mapping


GAP_NONE = "NONE"
GAP_RESET = "RESET_REQUIRED"
GAP_DETECTED = "GAP_DETECTED"
GAP_STALE = "STALE_OR_OUT_OF_ORDER"
GAP_BASE_SNAPSHOT_REQUIRED = "BASE_SNAPSHOT_REQUIRED"
GAP_REST_SNAPSHOT_REQUIRED = "REST_SNAPSHOT_REQUIRED"
GAP_CONTINUITY_UNVERIFIABLE = "CONTINUITY_UNVERIFIABLE"


class VenueSchemaError(RuntimeError):
    """Base error for a message outside the pinned public schema."""


class UnknownMessage(VenueSchemaError):
    """The connection, channel, operation, or message family is not allowed."""


class MalformedMessage(VenueSchemaError):
    """A known public message family is missing required typed fields."""


class ContractMismatch(VenueSchemaError):
    """A message is not bound to the one requested contract/index pair."""


@dataclass(frozen=True)
class ConnectionProfile:
    name: str
    host: str
    port: int
    path: str
    channels: frozenset[str]
    public_only: bool = True
    connection_headers_allowed: bool = False
    query_allowed: bool = False
    transport_blocker: str | None = None
    book_bootstrap: str | None = None

    @property
    def transport_443_compatible(self) -> bool:
        return self.port == 443 and self.transport_blocker is None

    @property
    def transport_supported(self) -> bool:
        """Whether the additive exact-port transport can represent this endpoint."""
        return self.port in {443, 8443} and self.transport_blocker is None


@dataclass(frozen=True)
class VenueProfile:
    venue: str
    connections: tuple[ConnectionProfile, ...]
    websocket_unavailable: frozenset[str] = frozenset()

    def connection(self, name: str) -> ConnectionProfile:
        matches = [item for item in self.connections if item.name == name]
        if len(matches) != 1:
            raise UnknownMessage(f"connection is not declared for {self.venue}: {name}")
        return matches[0]


@dataclass(frozen=True)
class SubscriptionRequest:
    connection: str
    message: dict[str, Any]


@dataclass(frozen=True)
class NormalizedEvent:
    venue: str
    contract: str
    source_instrument: str
    connection: str
    channel: str
    kind: str
    action: str
    exchange_ts_ms: int | None
    gateway_ts_ms: int | None
    sequence_start: int | None = None
    sequence_end: int | None = None
    previous_sequence: int | None = None
    cross_sequence: int | None = None
    gap_signal: str = GAP_NONE
    rest_snapshot_required: bool = False
    bids: tuple[tuple[str, str], ...] = ()
    asks: tuple[tuple[str, str], ...] = ()
    fields: Mapping[str, Any] = MappingProxyType({})


_PROFILES = MappingProxyType(
    {
        "bybit": VenueProfile(
            venue="bybit",
            connections=(
                ConnectionProfile(
                    name="public_linear",
                    host="stream.bybit.com",
                    port=443,
                    path="/v5/public/linear",
                    channels=frozenset({"orderbook.50", "publicTrade", "tickers"}),
                ),
            ),
            websocket_unavailable=frozenset({"price_limit"}),
        ),
        "okx": VenueProfile(
            venue="okx",
            connections=(
                ConnectionProfile(
                    name="public",
                    host="ws.okx.com",
                    port=8443,
                    path="/ws/v5/public",
                    channels=frozenset(
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
                ),
                ConnectionProfile(
                    name="business",
                    host="ws.okx.com",
                    port=8443,
                    path="/ws/v5/business",
                    channels=frozenset({"trades-all"}),
                ),
            ),
        ),
        "gate": VenueProfile(
            venue="gate",
            connections=(
                ConnectionProfile(
                    name="public_usdt",
                    host="fx-ws.gateio.ws",
                    port=443,
                    path="/v4/ws/usdt",
                    channels=frozenset(
                        {
                            "futures.order_book_update",
                            "futures.book_ticker",
                            "futures.trades",
                            "futures.tickers",
                            "futures.contract_stats",
                        }
                    ),
                    book_bootstrap="REST_SNAPSHOT_REQUIRED_UNLESS_FULL_FRAME",
                ),
            ),
            websocket_unavailable=frozenset({"price_limit"}),
        ),
    }
)

_BYBIT_CONTRACT = re.compile(r"[A-Z0-9]{2,40}")
_OKX_CONTRACT = re.compile(r"[A-Z0-9]{1,24}(?:-[A-Z0-9]{1,24}){2,3}")
_OKX_INDEX = re.compile(r"[A-Z0-9]{1,24}-[A-Z0-9]{1,24}")
_GATE_CONTRACT = re.compile(r"[A-Z0-9]{1,40}_[A-Z0-9]{1,16}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9]{1,32}")


def venue_profile(venue: str) -> VenueProfile:
    if not isinstance(venue, str) or venue not in _PROFILES:
        raise VenueSchemaError(f"unknown venue: {venue}")
    return _PROFILES[venue]


def _contract(venue: str, value: object) -> str:
    patterns = {
        "bybit": _BYBIT_CONTRACT,
        "okx": _OKX_CONTRACT,
        "gate": _GATE_CONTRACT,
    }
    pattern = patterns.get(venue)
    if pattern is None or not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise VenueSchemaError(f"contract is not canonical for {venue}: {value}")
    return value


def _okx_index(contract: str, value: object) -> str:
    if not isinstance(value, str) or _OKX_INDEX.fullmatch(value) is None:
        raise VenueSchemaError("OKX index_id must be a canonical base-quote index")
    expected = "-".join(contract.split("-")[:2])
    if value != expected:
        raise VenueSchemaError(f"OKX index_id is not derived from the contract: {value}")
    return value


def _request_id(value: object) -> str:
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        raise VenueSchemaError("request_id must be 1-32 ASCII alphanumeric characters")
    return value


def _gate_time(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VenueSchemaError("request_time_sec must be a non-negative integer")
    return value


def build_subscriptions(
    venue: str,
    contract: str,
    *,
    index_id: str | None = None,
    request_id: str | None = None,
    request_time_sec: int | None = None,
) -> tuple[SubscriptionRequest, ...]:
    """Build only the fixed public subscriptions for one exact contract."""

    venue_profile(venue)
    symbol = _contract(venue, contract)
    if venue == "bybit":
        if index_id is not None or request_time_sec is not None:
            raise VenueSchemaError("Bybit builder received a field outside its schema")
        identifier = _request_id(request_id)
        return (
            SubscriptionRequest(
                connection="public_linear",
                message={
                    "req_id": identifier,
                    "op": "subscribe",
                    "args": [
                        f"orderbook.50.{symbol}",
                        f"publicTrade.{symbol}",
                        f"tickers.{symbol}",
                    ],
                },
            ),
        )
    if venue == "okx":
        if request_time_sec is not None:
            raise VenueSchemaError("OKX builder received a field outside its schema")
        identifier = _request_id(request_id)
        index = _okx_index(symbol, index_id)
        public_channels = (
            ("books", symbol),
            ("tickers", symbol),
            ("mark-price", symbol),
            ("index-tickers", index),
            ("funding-rate", symbol),
            ("open-interest", symbol),
            ("price-limit", symbol),
        )
        return (
            SubscriptionRequest(
                connection="public",
                message={
                    "id": identifier,
                    "op": "subscribe",
                    "args": [
                        {"channel": channel, "instId": instrument}
                        for channel, instrument in public_channels
                    ],
                },
            ),
            SubscriptionRequest(
                connection="business",
                message={
                    "id": identifier,
                    "op": "subscribe",
                    "args": [{"channel": "trades-all", "instId": symbol}],
                },
            ),
        )
    if venue == "gate":
        if index_id is not None or request_id is not None:
            raise VenueSchemaError("Gate builder received a field outside its schema")
        request_time = _gate_time(request_time_sec)
        specifications = (
            ("futures.order_book_update", [symbol, "100ms", "100"]),
            ("futures.book_ticker", [symbol]),
            ("futures.trades", [symbol]),
            ("futures.tickers", [symbol]),
            ("futures.contract_stats", [symbol, "1m"]),
        )
        return tuple(
            SubscriptionRequest(
                connection="public_usdt",
                message={
                    "time": request_time,
                    "channel": channel,
                    "event": "subscribe",
                    "payload": payload,
                },
            )
            for channel, payload in specifications
        )
    raise VenueSchemaError(f"unknown venue: {venue}")


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MalformedMessage(f"{label} must be an object")
    return value


def _array(value: object, label: str, *, nonempty: bool = False) -> list[Any] | tuple[Any, ...]:
    if not isinstance(value, (list, tuple)) or (nonempty and not value):
        raise MalformedMessage(f"{label} must be {'a non-empty' if nonempty else 'an'} array")
    return value


def _required(row: Mapping[str, Any], name: str, label: str) -> Any:
    if name not in row:
        raise MalformedMessage(f"{label} is missing {name}")
    return row[name]


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise MalformedMessage(f"{label} must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
        result = int(value)
    else:
        raise MalformedMessage(f"{label} must be an integer")
    if result < minimum:
        raise MalformedMessage(f"{label} must be >= {minimum}")
    return result


def _optional_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label)


def _decimal(
    value: object,
    label: str,
    *,
    positive: bool = False,
    signed: bool = False,
) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MalformedMessage(f"{label} must be a decimal string or integer")
    rendered = str(value)
    try:
        parsed = Decimal(rendered)
    except InvalidOperation as exc:
        raise MalformedMessage(f"{label} is not decimal") from exc
    if (
        not parsed.is_finite()
        or (positive and parsed <= 0)
        or (not positive and not signed and parsed < 0)
    ):
        raise MalformedMessage(f"{label} has an invalid sign or magnitude")
    return rendered


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise MalformedMessage(f"{label} must be a non-empty string")
    return value


def _match_contract(actual: object, expected: str, label: str = "instrument") -> str:
    value = _text(actual, label)
    if value != expected:
        raise ContractMismatch(f"{label} {value} does not match bound value {expected}")
    return value


def _levels(rows: object, label: str, *, objects: bool = False) -> tuple[tuple[str, str], ...]:
    values = _array(rows, label)
    normalized: list[tuple[str, str]] = []
    for index, raw in enumerate(values):
        if objects:
            row = _mapping(raw, f"{label}[{index}]")
            price = _decimal(_required(row, "p", label), f"{label}.price", positive=True)
            size = _decimal(_required(row, "s", label), f"{label}.size")
        else:
            row = _array(raw, f"{label}[{index}]")
            if len(row) < 2:
                raise MalformedMessage(f"{label}[{index}] needs price and size")
            price = _decimal(row[0], f"{label}.price", positive=True)
            size = _decimal(row[1], f"{label}.size")
        normalized.append((price, size))
    return tuple(normalized)


def _frozen_fields(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({key: value for key, value in values.items() if value is not None})


def _optional_decimal(
    row: Mapping[str, Any],
    source: str,
    label: str,
    *,
    positive: bool = False,
    signed: bool = False,
) -> str | None:
    if source not in row or row[source] == "":
        return None
    return _decimal(row[source], label, positive=positive, signed=signed)


def _event(
    *,
    venue: str,
    contract: str,
    source_instrument: str,
    connection: str,
    channel: str,
    kind: str,
    action: str = "update",
    exchange_ts_ms: int | None = None,
    gateway_ts_ms: int | None = None,
    sequence_start: int | None = None,
    sequence_end: int | None = None,
    previous_sequence: int | None = None,
    cross_sequence: int | None = None,
    gap_signal: str = GAP_NONE,
    rest_snapshot_required: bool = False,
    bids: tuple[tuple[str, str], ...] = (),
    asks: tuple[tuple[str, str], ...] = (),
    fields: Mapping[str, Any] | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        venue=venue,
        contract=contract,
        source_instrument=source_instrument,
        connection=connection,
        channel=channel,
        kind=kind,
        action=action,
        exchange_ts_ms=exchange_ts_ms,
        gateway_ts_ms=gateway_ts_ms,
        sequence_start=sequence_start,
        sequence_end=sequence_end,
        previous_sequence=previous_sequence,
        cross_sequence=cross_sequence,
        gap_signal=gap_signal,
        rest_snapshot_required=rest_snapshot_required,
        bids=bids,
        asks=asks,
        fields=_frozen_fields(fields or {}),
    )


def _validate_parse_context(
    venue: str,
    contract: str,
    connection: str,
    index_id: str | None,
    last_sequence: int | None,
) -> tuple[str, str | None, ConnectionProfile, int | None]:
    profile = venue_profile(venue)
    symbol = _contract(venue, contract)
    declared = profile.connection(connection)
    if venue == "okx":
        index = _okx_index(symbol, index_id)
    elif index_id is not None:
        raise VenueSchemaError(f"{venue} parser received index_id outside its schema")
    else:
        index = None
    if last_sequence is not None:
        if isinstance(last_sequence, bool) or not isinstance(last_sequence, int) or last_sequence < 0:
            raise VenueSchemaError("last_sequence must be a non-negative integer")
    return symbol, index, declared, last_sequence


def parse_message(
    venue: str,
    message: Mapping[str, Any],
    *,
    contract: str,
    connection: str,
    index_id: str | None = None,
    last_sequence: int | None = None,
) -> tuple[NormalizedEvent, ...]:
    """Normalize one decoded public message under an exact venue/contract context."""

    symbol, index, declared, last = _validate_parse_context(
        venue, contract, connection, index_id, last_sequence
    )
    payload = _mapping(message, "message")
    if venue == "bybit":
        return _parse_bybit(payload, symbol, declared, last)
    if venue == "okx":
        return _parse_okx(payload, symbol, index or "", declared, last)
    if venue == "gate":
        return _parse_gate(payload, symbol, declared, last)
    raise VenueSchemaError(f"unknown venue: {venue}")


def _parse_bybit(
    message: Mapping[str, Any],
    contract: str,
    connection: ConnectionProfile,
    last_sequence: int | None,
) -> tuple[NormalizedEvent, ...]:
    if "op" in message:
        operation = message.get("op")
        if operation == "subscribe" and message.get("success") is True:
            return (
                _event(
                    venue="bybit",
                    contract=contract,
                    source_instrument=contract,
                    connection=connection.name,
                    channel="subscribe",
                    kind="subscription_ack",
                ),
            )
        if operation == "pong":
            return (
                _event(
                    venue="bybit",
                    contract=contract,
                    source_instrument=contract,
                    connection=connection.name,
                    channel="pong",
                    kind="heartbeat",
                ),
            )
        raise UnknownMessage(f"Bybit operation is not a declared public control: {operation}")

    topic = _text(_required(message, "topic", "Bybit message"), "Bybit topic")
    topic_families = (
        ("orderbook.50.", "orderbook.50"),
        ("publicTrade.", "publicTrade"),
        ("tickers.", "tickers"),
    )
    matched = [(prefix, family) for prefix, family in topic_families if topic.startswith(prefix)]
    if len(matched) != 1:
        raise UnknownMessage(f"Bybit topic is not an allowed public family: {topic}")
    prefix, topic_family = matched[0]
    _match_contract(topic[len(prefix) :], contract, "Bybit topic instrument")
    gateway_ts = _integer(_required(message, "ts", "Bybit message"), "Bybit ts")
    data = _required(message, "data", "Bybit message")
    if topic_family == "orderbook.50":
        row = _mapping(data, "Bybit book data")
        _match_contract(_required(row, "s", "Bybit book"), contract)
        action_value = _required(message, "type", "Bybit book")
        if action_value not in {"snapshot", "delta"}:
            raise MalformedMessage("Bybit book type must be snapshot or delta")
        update = _integer(_required(row, "u", "Bybit book"), "Bybit update id")
        cross = _integer(_required(row, "seq", "Bybit book"), "Bybit cross sequence")
        exchange_ts = _integer(_required(message, "cts", "Bybit book"), "Bybit cts")
        action = "snapshot" if action_value == "snapshot" or update == 1 else "delta"
        if action == "snapshot":
            signal = GAP_RESET
        elif last_sequence is None:
            signal = GAP_BASE_SNAPSHOT_REQUIRED
        elif update <= last_sequence:
            signal = GAP_STALE
        else:
            signal = GAP_CONTINUITY_UNVERIFIABLE
        return (
            _event(
                venue="bybit",
                contract=contract,
                source_instrument=contract,
                connection=connection.name,
                channel="orderbook.50",
                kind="book",
                action=action,
                exchange_ts_ms=exchange_ts,
                gateway_ts_ms=gateway_ts,
                sequence_end=update,
                cross_sequence=cross,
                gap_signal=signal,
                bids=_levels(_required(row, "b", "Bybit book"), "Bybit bids"),
                asks=_levels(_required(row, "a", "Bybit book"), "Bybit asks"),
            ),
        )
    if topic_family == "publicTrade":
        if message.get("type") != "snapshot":
            raise MalformedMessage("Bybit trade type must be snapshot")
        rows = _array(data, "Bybit trades", nonempty=True)
        events: list[NormalizedEvent] = []
        for raw in rows:
            row = _mapping(raw, "Bybit trade")
            _match_contract(_required(row, "s", "Bybit trade"), contract)
            side_value = _required(row, "S", "Bybit trade")
            if side_value not in {"Buy", "Sell"}:
                raise MalformedMessage("Bybit trade side must be Buy or Sell")
            events.append(
                _event(
                    venue="bybit",
                    contract=contract,
                    source_instrument=contract,
                    connection=connection.name,
                    channel="publicTrade",
                    kind="trade",
                    exchange_ts_ms=_integer(_required(row, "T", "Bybit trade"), "Bybit trade time"),
                    gateway_ts_ms=gateway_ts,
                    cross_sequence=_integer(_required(row, "seq", "Bybit trade"), "Bybit trade sequence"),
                    fields={
                        "trade_id": _text(_required(row, "i", "Bybit trade"), "Bybit trade id"),
                        "side": side_value.lower(),
                        "price": _decimal(_required(row, "p", "Bybit trade"), "Bybit trade price", positive=True),
                        "size": _decimal(_required(row, "v", "Bybit trade"), "Bybit trade size", positive=True),
                    },
                )
            )
        return tuple(events)
    if topic_family == "tickers":
        action = message.get("type")
        if action not in {"snapshot", "delta"}:
            raise MalformedMessage("Bybit ticker type must be snapshot or delta")
        row = _mapping(data, "Bybit ticker")
        _match_contract(_required(row, "symbol", "Bybit ticker"), contract)
        aliases = {
            "last_price": ("lastPrice", True),
            "bid_price": ("bid1Price", True),
            "bid_size": ("bid1Size", False),
            "ask_price": ("ask1Price", True),
            "ask_size": ("ask1Size", False),
            "mark_price": ("markPrice", True),
            "index_price": ("indexPrice", True),
            "funding_rate": ("fundingRate", False),
            "open_interest": ("openInterest", False),
            "open_interest_value": ("openInterestValue", False),
            "pre_open_price": ("preOpenPrice", True),
            "pre_open_quantity": ("preQty", False),
        }
        fields = {
            target: _optional_decimal(
                row,
                source,
                f"Bybit {source}",
                positive=positive,
                signed=target == "funding_rate",
            )
            for target, (source, positive) in aliases.items()
        }
        if "curPreListingPhase" in row:
            fields["pre_listing_phase"] = _text(row["curPreListingPhase"], "Bybit pre-listing phase")
        if not any(value is not None for value in fields.values()):
            raise MalformedMessage("Bybit ticker contains no supported metric")
        return (
            _event(
                venue="bybit",
                contract=contract,
                source_instrument=contract,
                connection=connection.name,
                channel="tickers",
                kind="ticker",
                action=action,
                exchange_ts_ms=gateway_ts,
                gateway_ts_ms=gateway_ts,
                cross_sequence=_integer(_required(message, "cs", "Bybit ticker"), "Bybit ticker sequence"),
                fields=fields,
            ),
        )
    raise UnknownMessage(f"Bybit topic is not contract-bound and allowed: {topic}")


def _okx_source(channel: str, contract: str, index_id: str) -> str:
    return index_id if channel == "index-tickers" else contract


def _parse_okx(
    message: Mapping[str, Any],
    contract: str,
    index_id: str,
    connection: ConnectionProfile,
    last_sequence: int | None,
) -> tuple[NormalizedEvent, ...]:
    if "event" in message:
        event_name = message.get("event")
        if event_name != "subscribe":
            raise UnknownMessage(f"OKX control event is not an accepted subscription ack: {event_name}")
        arg = _mapping(_required(message, "arg", "OKX ack"), "OKX ack arg")
        channel = _text(_required(arg, "channel", "OKX ack"), "OKX ack channel")
        if channel not in connection.channels:
            raise UnknownMessage(f"OKX channel is not allowed on {connection.name}: {channel}")
        source = _okx_source(channel, contract, index_id)
        _match_contract(_required(arg, "instId", "OKX ack"), source)
        return (
            _event(
                venue="okx",
                contract=contract,
                source_instrument=source,
                connection=connection.name,
                channel=channel,
                kind="subscription_ack",
            ),
        )

    arg = _mapping(_required(message, "arg", "OKX message"), "OKX arg")
    channel = _text(_required(arg, "channel", "OKX arg"), "OKX channel")
    if channel not in connection.channels:
        raise UnknownMessage(f"OKX channel is not allowed on {connection.name}: {channel}")
    source = _okx_source(channel, contract, index_id)
    _match_contract(_required(arg, "instId", "OKX arg"), source)
    rows = _array(_required(message, "data", "OKX message"), "OKX data", nonempty=True)
    if channel == "books":
        if len(rows) != 1:
            raise MalformedMessage("OKX books message must contain one book object")
        action = message.get("action")
        if action not in {"snapshot", "update"}:
            raise MalformedMessage("OKX books action must be snapshot or update")
        row = _mapping(rows[0], "OKX book")
        previous = _integer(_required(row, "prevSeqId", "OKX book"), "OKX prevSeqId", minimum=-1)
        current = _integer(_required(row, "seqId", "OKX book"), "OKX seqId")
        if action == "snapshot":
            if previous != -1:
                raise MalformedMessage("OKX snapshot prevSeqId must be -1")
            signal = GAP_RESET
        elif last_sequence is None:
            signal = GAP_BASE_SNAPSHOT_REQUIRED
        elif current < previous:
            signal = GAP_RESET
        elif previous != last_sequence:
            signal = GAP_DETECTED
        else:
            signal = GAP_NONE
        checksum = _integer(_required(row, "checksum", "OKX book"), "OKX checksum", minimum=-2**31)
        return (
            _event(
                venue="okx",
                contract=contract,
                source_instrument=source,
                connection=connection.name,
                channel=channel,
                kind="book",
                action=action,
                exchange_ts_ms=_integer(_required(row, "ts", "OKX book"), "OKX book ts"),
                previous_sequence=previous,
                sequence_end=current,
                gap_signal=signal,
                bids=_levels(_required(row, "bids", "OKX book"), "OKX bids"),
                asks=_levels(_required(row, "asks", "OKX book"), "OKX asks"),
                fields={"checksum_deprecated": checksum},
            ),
        )
    if channel == "trades-all":
        events: list[NormalizedEvent] = []
        for raw in rows:
            row = _mapping(raw, "OKX trade")
            _match_contract(_required(row, "instId", "OKX trade"), contract)
            side = _required(row, "side", "OKX trade")
            if side not in {"buy", "sell"}:
                raise MalformedMessage("OKX trade side must be buy or sell")
            events.append(
                _event(
                    venue="okx",
                    contract=contract,
                    source_instrument=contract,
                    connection=connection.name,
                    channel=channel,
                    kind="trade",
                    exchange_ts_ms=_integer(_required(row, "ts", "OKX trade"), "OKX trade ts"),
                    fields={
                        "trade_id": _text(_required(row, "tradeId", "OKX trade"), "OKX trade id"),
                        "side": side,
                        "price": _decimal(_required(row, "px", "OKX trade"), "OKX trade price", positive=True),
                        "size": _decimal(_required(row, "sz", "OKX trade"), "OKX trade size", positive=True),
                    },
                )
            )
        return tuple(events)
    return tuple(
        _parse_okx_metric(channel, _mapping(raw, f"OKX {channel}"), contract, source, connection)
        for raw in rows
    )


def _parse_okx_metric(
    channel: str,
    row: Mapping[str, Any],
    contract: str,
    source: str,
    connection: ConnectionProfile,
) -> NormalizedEvent:
    _match_contract(_required(row, "instId", f"OKX {channel}"), source)
    ts = _integer(_required(row, "ts", f"OKX {channel}"), f"OKX {channel} ts")
    kind: str
    fields: dict[str, Any]
    if channel == "tickers":
        kind = "ticker"
        aliases = {
            "last_price": ("last", True),
            "bid_price": ("bidPx", True),
            "bid_size": ("bidSz", False),
            "ask_price": ("askPx", True),
            "ask_size": ("askSz", False),
        }
        fields = {
            target: _optional_decimal(row, key, f"OKX {key}", positive=positive)
            for target, (key, positive) in aliases.items()
        }
    elif channel == "mark-price":
        kind = "mark"
        fields = {"mark_price": _decimal(_required(row, "markPx", "OKX mark"), "OKX mark price", positive=True)}
    elif channel == "index-tickers":
        kind = "index"
        fields = {"index_price": _decimal(_required(row, "idxPx", "OKX index"), "OKX index price", positive=True)}
    elif channel == "funding-rate":
        kind = "funding"
        fields = {
            "funding_rate": _decimal(
                _required(row, "fundingRate", "OKX funding"),
                "OKX funding rate",
                signed=True,
            ),
            "next_funding_time_ms": _optional_integer(row.get("nextFundingTime"), "OKX next funding time"),
        }
    elif channel == "open-interest":
        kind = "open_interest"
        fields = {
            "open_interest": _decimal(_required(row, "oi", "OKX open interest"), "OKX open interest"),
            "open_interest_currency": _optional_decimal(row, "oiCcy", "OKX open interest currency"),
            "open_interest_usd": _optional_decimal(row, "oiUsd", "OKX open interest USD"),
        }
    elif channel == "price-limit":
        kind = "price_limit"
        enabled = _required(row, "enabled", "OKX price limit")
        if not isinstance(enabled, bool):
            raise MalformedMessage("OKX price-limit enabled must be bool")
        fields = {
            "buy_limit": _optional_decimal(row, "buyLmt", "OKX buy limit", positive=True),
            "sell_limit": _optional_decimal(row, "sellLmt", "OKX sell limit", positive=True),
            "enabled": enabled,
        }
    else:
        raise UnknownMessage(f"OKX metric channel is not implemented: {channel}")
    if not any(value is not None for value in fields.values()):
        raise MalformedMessage(f"OKX {channel} contains no supported metric")
    return _event(
        venue="okx",
        contract=contract,
        source_instrument=source,
        connection=connection.name,
        channel=channel,
        kind=kind,
        exchange_ts_ms=ts,
        fields=fields,
    )


def _parse_gate(
    message: Mapping[str, Any],
    contract: str,
    connection: ConnectionProfile,
    last_sequence: int | None,
) -> tuple[NormalizedEvent, ...]:
    channel = _text(_required(message, "channel", "Gate message"), "Gate channel")
    if channel not in connection.channels:
        raise UnknownMessage(f"Gate channel is not allowed: {channel}")
    event_name = _required(message, "event", "Gate message")
    if event_name == "subscribe":
        result = _mapping(_required(message, "result", "Gate ack"), "Gate ack result")
        if result.get("status") != "success":
            raise MalformedMessage("Gate subscription ack is not successful")
        return (
            _event(
                venue="gate",
                contract=contract,
                source_instrument=contract,
                connection=connection.name,
                channel=channel,
                kind="subscription_ack",
                gateway_ts_ms=_optional_integer(message.get("time_ms"), "Gate ack time"),
            ),
        )
    if event_name != "update":
        raise UnknownMessage(f"Gate event is not an allowed public update: {event_name}")
    gateway_ts = _integer(_required(message, "time_ms", "Gate update"), "Gate time_ms")
    result = _required(message, "result", "Gate update")
    if channel == "futures.order_book_update":
        row = _mapping(result, "Gate book")
        _match_contract(_required(row, "s", "Gate book"), contract)
        first = _integer(_required(row, "U", "Gate book"), "Gate first update")
        final = _integer(_required(row, "u", "Gate book"), "Gate final update")
        if first > final:
            raise MalformedMessage("Gate book update range is reversed")
        full = row.get("full", False)
        if not isinstance(full, bool):
            raise MalformedMessage("Gate book full marker must be bool")
        if full:
            action = "snapshot"
            signal = GAP_RESET
            rest_required = False
        elif last_sequence is None:
            action = "delta"
            signal = GAP_REST_SNAPSHOT_REQUIRED
            rest_required = True
        else:
            action = "delta"
            expected = last_sequence + 1
            rest_required = False
            if final < expected:
                signal = GAP_STALE
            elif first <= expected <= final:
                signal = GAP_NONE
            else:
                signal = GAP_DETECTED
        return (
            _event(
                venue="gate",
                contract=contract,
                source_instrument=contract,
                connection=connection.name,
                channel=channel,
                kind="book",
                action=action,
                exchange_ts_ms=_integer(_required(row, "t", "Gate book"), "Gate book time"),
                gateway_ts_ms=gateway_ts,
                sequence_start=first,
                sequence_end=final,
                gap_signal=signal,
                rest_snapshot_required=rest_required,
                bids=_levels(_required(row, "b", "Gate book"), "Gate bids", objects=True),
                asks=_levels(_required(row, "a", "Gate book"), "Gate asks", objects=True),
                fields={"depth_level": _text(_required(row, "l", "Gate book"), "Gate book level")},
            ),
        )
    if channel == "futures.book_ticker":
        row = _mapping(result, "Gate BBO")
        _match_contract(_required(row, "s", "Gate BBO"), contract)
        return (
            _event(
                venue="gate",
                contract=contract,
                source_instrument=contract,
                connection=connection.name,
                channel=channel,
                kind="bbo",
                exchange_ts_ms=_integer(_required(row, "t", "Gate BBO"), "Gate BBO time"),
                gateway_ts_ms=gateway_ts,
                sequence_end=_integer(_required(row, "u", "Gate BBO"), "Gate BBO sequence"),
                fields={
                    "bid_price": _optional_decimal(row, "b", "Gate bid", positive=True),
                    "bid_size": _decimal(_required(row, "B", "Gate BBO"), "Gate bid size"),
                    "ask_price": _optional_decimal(row, "a", "Gate ask", positive=True),
                    "ask_size": _decimal(_required(row, "A", "Gate BBO"), "Gate ask size"),
                },
            ),
        )
    rows = _array(result, f"Gate {channel}", nonempty=True)
    if channel == "futures.trades":
        events: list[NormalizedEvent] = []
        for raw in rows:
            row = _mapping(raw, "Gate trade")
            _match_contract(_required(row, "contract", "Gate trade"), contract)
            signed_size = _decimal(
                _required(row, "size", "Gate trade"),
                "Gate trade size signed",
                signed=True,
            )
            parsed_size = Decimal(signed_size)
            if parsed_size == 0:
                raise MalformedMessage("Gate trade size cannot be zero")
            events.append(
                _event(
                    venue="gate",
                    contract=contract,
                    source_instrument=contract,
                    connection=connection.name,
                    channel=channel,
                    kind="trade",
                    exchange_ts_ms=_integer(_required(row, "create_time_ms", "Gate trade"), "Gate trade time"),
                    gateway_ts_ms=gateway_ts,
                    fields={
                        "trade_id": str(_integer(_required(row, "id", "Gate trade"), "Gate trade id")),
                        "side": "buy" if parsed_size > 0 else "sell",
                        "price": _decimal(_required(row, "price", "Gate trade"), "Gate trade price", positive=True),
                        "size": str(abs(parsed_size)),
                    },
                )
            )
        return tuple(events)
    return tuple(
        _parse_gate_metric(channel, _mapping(raw, f"Gate {channel}"), contract, connection, gateway_ts)
        for raw in rows
    )


def _parse_gate_metric(
    channel: str,
    row: Mapping[str, Any],
    contract: str,
    connection: ConnectionProfile,
    gateway_ts: int,
) -> NormalizedEvent:
    _match_contract(_required(row, "contract", f"Gate {channel}"), contract)
    if channel == "futures.tickers":
        kind = "ticker"
        fields = {
            "last_price": _optional_decimal(row, "last", "Gate last", positive=True),
            "funding_rate": _optional_decimal(
                row, "funding_rate", "Gate funding rate", signed=True
            ),
            "funding_rate_indicative": _optional_decimal(
                row,
                "funding_rate_indicative",
                "Gate indicative funding",
                signed=True,
            ),
            "mark_price": _optional_decimal(row, "mark_price", "Gate mark", positive=True),
            "index_price": _optional_decimal(row, "index_price", "Gate index", positive=True),
            "open_interest": _optional_decimal(row, "total_size", "Gate total size"),
        }
        exchange_ts = gateway_ts
    elif channel == "futures.contract_stats":
        kind = "open_interest"
        fields = {
            "open_interest": _decimal(_required(row, "open_interest", "Gate stats"), "Gate open interest"),
            "mark_price": _optional_decimal(row, "mark_price", "Gate stats mark", positive=True),
        }
        exchange_ts = _integer(_required(row, "time", "Gate stats"), "Gate stats time") * 1000
    else:
        raise UnknownMessage(f"Gate metric channel is not implemented: {channel}")
    if not any(value is not None for value in fields.values()):
        raise MalformedMessage(f"Gate {channel} contains no supported metric")
    return _event(
        venue="gate",
        contract=contract,
        source_instrument=contract,
        connection=connection.name,
        channel=channel,
        kind=kind,
        exchange_ts_ms=exchange_ts,
        gateway_ts_ms=gateway_ts,
        fields=fields,
    )
