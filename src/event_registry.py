"""The registry of listing events, and where each t0 actually came from.

The hypothesis this project exists to test is about the seconds around t0. A t0 that
is wrong by a minute makes every capture built on it worthless, so the registry treats
the provenance of that timestamp as the primary datum rather than an annotation.

The spot monitor's audit is the reason. There, MEXC's firstOpenTime and Gate's
min(buy_start, sell_start) were pooled into one `listed_ts` column: one is when trading
actually began, the other is a schedule that can still move. Nothing recorded which was
which, and nothing stopped them being averaged together. Here a source class travels
with every event and events of different classes are never merged.

Two further properties matter for pre-market listings specifically:

* venues move launch times, sometimes more than once. A revision is appended with the
  previous value, never written over it - a t0 that silently changed after a capture
  would invalidate the capture without leaving a trace.
* nothing here is an official announcement yet. OFFICIAL_ANNOUNCEMENT exists as a
  class so that adding such a source later is a different class by construction rather
  than a quiet upgrade of what venue metadata means.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import socket
import time
import unicodedata
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import project_config as config
import public_http
import risk_gate
import frozen_plan_bindings as trust_root
from canonical_hash import canonical_hash


def _has_forbidden_unicode_controls(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)


REGISTRY_V2_SCHEMA = "premarket_perp_event_registry_v2"
REGISTRY_SCHEMA = "premarket_perp_event_registry_v3"
REGISTRY_V1_PATH = config.PROJECT_ROOT / "docs/registry/listing-events.jsonl"
REGISTRY_V2_PATH = config.PROJECT_ROOT / "docs/registry/listing-events-v2.jsonl"
REGISTRY_V2_SUMMARY_PATH = config.PROJECT_ROOT / "docs/registry/listing-events-v2.summary.json"
REGISTRY_V2_MUTATION_RECEIPT_PATH = (
    config.PROJECT_ROOT
    / "docs/registry/listing-events-v2.jsonl.mutation-receipts"
    / (
        "00000000000000000000-"
        "d86913b6af93d3487ef3cdbe09a3b47f3519188eea65ebf2f18aaa8fd5976282.json"
    )
)
REGISTRY_PATH = config.PROJECT_ROOT / "docs/registry/listing-events-v3.jsonl"
REGISTRY_SUMMARY_PATH = config.PROJECT_ROOT / "docs/registry/listing-events-v3.summary.json"
REGISTRY_LOCK_PATH = config.PROJECT_ROOT / "docs/registry/listing-events-v3.lock"
LEGACY_V2_REGISTRY_SHA256 = (
    "fd3b864bc4b1b311b49a904246edd8980008ab5f1830df9042087df9619bc9a4"
)
LEGACY_V2_HEAD_RECORD_HASH = (
    "7d3459943cf2122c0eb39a27452a4ab28eb41a08ceb61e19011730e14d6e8696"
)
LEGACY_V2_MUTATION_RECEIPT_HASH = (
    "d86913b6af93d3487ef3cdbe09a3b47f3519188eea65ebf2f18aaa8fd5976282"
)
LEGACY_V2_SUMMARY_SHA256 = (
    "72a619aa893cd794dbc0e3702f2f6fd9ccd3a495c8b610dfa564c8e20b6df176"
)
LEGACY_V2_MUTATION_RECEIPT_FILE_SHA256 = (
    "1c8eab6576a2d4452dfbcd9aba87563a0a50da4382da0447012e6f95cb1d865b"
)
LEGACY_V2_SUMMARY_CONTENT_HASH = (
    "ba4e8bbc99391cc0049810d3fd0e430f6806407b43347f4e5c6e0d1dd29b08a8"
)
ACTIVE_CONTRACTS_FIELD = "active_contract_ids_by_venue"
ACTIVE_LIFECYCLE_GENERATIONS_FIELD = "active_lifecycle_generations_by_venue"
LIFECYCLE_GENERATION_HIGH_WATER_FIELD = "lifecycle_generation_high_water_by_venue"
LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD = (
    "last_complete_metadata_refresh_received_at_utc"
)
MAX_COMPLETE_METADATA_REFRESH_AGE_SEC = config.MAX_COMPLETE_METADATA_REFRESH_AGE_SEC
RAW_UNIVERSE_ROWS_FIELD = "raw_universe_rows_by_venue"
RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD = "raw_universe_rows_by_surface"
RELEVANT_IDENTITY_IDS_BY_SURFACE_FIELD = "relevant_identity_ids_by_surface"
RELEVANT_IDENTITY_HASHES_BY_SURFACE_FIELD = (
    "relevant_identity_set_sha256_by_surface"
)
EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD = "explicit_terminal_ids_by_surface"
MIN_FULL_UNIVERSE_RETENTION_RATIO = config.MIN_FULL_UNIVERSE_RETENTION_RATIO
FULL_UNIVERSE_SURFACE_IDS = tuple(config.FULL_UNIVERSE_SURFACE_IDS)
LEGITIMATELY_EMPTY_SURFACE_IDS = tuple(config.LEGITIMATELY_EMPTY_SURFACE_IDS)
MUTATION_RECEIPT_SCHEMA = "premarket_perp_registry_mutation_receipt_v1"
MUTATION_RECEIPT_DIR_SUFFIX = ".mutation-receipts"
MUTATION_SUMMARY_LINK_FIELDS = frozenset(
    {
        "mutation_receipt_schema",
        "mutation_seq",
        "previous_mutation_receipt_hash",
        "mutation_receipt_hash",
    }
)
# How a t0 was learned. These are never mixed inside one analysis, and the ordering
# here is deliberate: metadata is what we have, an announcement is what we would prefer.
SOURCE_OFFICIAL_ANNOUNCEMENT = "OFFICIAL_ANNOUNCEMENT"
SOURCE_VENUE_INSTRUMENT_METADATA = "VENUE_INSTRUMENT_METADATA"
SOURCE_OBSERVED_PUBLIC_TRADE = "OBSERVED_PUBLIC_TRADE"
SOURCE_OBSERVED_LIFECYCLE = "OBSERVED_LIFECYCLE"
SOURCE_CLASSES = (
    SOURCE_OFFICIAL_ANNOUNCEMENT,
    SOURCE_VENUE_INSTRUMENT_METADATA,
    SOURCE_OBSERVED_PUBLIC_TRADE,
    SOURCE_OBSERVED_LIFECYCLE,
)

TIMESTAMP_PREMARKET_CONTRACT_LAUNCH = "premarket_contract_launch_ts"
TIMESTAMP_OFFICIAL_SPOT_T0 = "official_spot_t0"
TIMESTAMP_FIRST_TRADE = "first_trade_ts"
TIMESTAMP_TRANSITION = "transition_ts"
TIMESTAMP_CONTRACT_CREATED = "contract_created_ts"
TIMESTAMP_KINDS = (
    TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
    TIMESTAMP_OFFICIAL_SPOT_T0,
    TIMESTAMP_FIRST_TRADE,
    TIMESTAMP_TRANSITION,
    TIMESTAMP_CONTRACT_CREATED,
)
INSTRUMENT_ROLES = ("premarket_perp", "spot", "standard_perp")

# Asset identity is authority, while the venue symbol is only a display/mapping field.
# A token ticker and an equity/pre-IPO ticker can be identical.  Treating the ticker as
# identity would let an official crypto announcement attach to an unrelated stock-like
# perpetual (the exact OPENAI/ANTHROPIC failure found in the v15 registry audit).
ASSET_CLASS_CRYPTO_TOKEN = "CRYPTO_TOKEN"
ASSET_CLASS_EQUITY_ISSUER = "EQUITY_ISSUER"
ASSET_CLASS_TOKENIZED_EQUITY = "TOKENIZED_EQUITY"
ASSET_CLASS_TRADFI_OTHER = "TRADFI_OTHER"
ASSET_CLASS_UNCLASSIFIED = "UNCLASSIFIED"
ASSET_CLASSES = frozenset(
    {
        ASSET_CLASS_CRYPTO_TOKEN,
        ASSET_CLASS_EQUITY_ISSUER,
        ASSET_CLASS_TOKENIZED_EQUITY,
        ASSET_CLASS_TRADFI_OTHER,
        ASSET_CLASS_UNCLASSIFIED,
    }
)
IDENTITY_EVIDENCE_VENUE_EXPLICIT_METADATA = "VENUE_EXPLICIT_METADATA"
IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION = "OFFICIAL_IDENTITY_ATTESTATION"
IDENTITY_EVIDENCE_LEGACY_UNCLASSIFIED = "LEGACY_UNCLASSIFIED"
IDENTITY_EVIDENCE_CLASSES = frozenset(
    {
        IDENTITY_EVIDENCE_VENUE_EXPLICIT_METADATA,
        IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
        IDENTITY_EVIDENCE_LEGACY_UNCLASSIFIED,
    }
)


class EventRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class AssetIdentity:
    asset_class: str
    issuer_namespace: str
    issuer_id: str
    evidence_class: str

    def __post_init__(self) -> None:
        if self.asset_class not in ASSET_CLASSES:
            raise EventRegistryError(f"unknown asset class: {self.asset_class}")
        if self.evidence_class not in IDENTITY_EVIDENCE_CLASSES:
            raise EventRegistryError(
                f"unknown asset identity evidence class: {self.evidence_class}"
            )
        for name, value in (
            ("issuer_namespace", self.issuer_namespace),
            ("issuer_id", self.issuer_id),
        ):
            if not isinstance(value, str) or not value or value != value.strip():
                raise EventRegistryError(f"{name} must be a non-empty canonical string")
            if _has_forbidden_unicode_controls(value):
                raise EventRegistryError(f"{name} contains forbidden control characters")

    def as_record_fields(self) -> dict[str, str]:
        return {
            "asset_class": self.asset_class,
            "issuer_namespace": self.issuer_namespace,
            "issuer_id": self.issuer_id,
            "identity_evidence_class": self.evidence_class,
            "asset_identity_hash": canonical_hash(
                {
                    "asset_class": self.asset_class,
                    "issuer_namespace": self.issuer_namespace,
                    "issuer_id": self.issuer_id,
                }
            ),
        }


def asset_identities_equivalent(left: AssetIdentity, right: AssetIdentity) -> bool:
    """Known identities match only by class, namespace and authority-native id."""
    if (
        left.asset_class == ASSET_CLASS_UNCLASSIFIED
        or right.asset_class == ASSET_CLASS_UNCLASSIFIED
    ):
        return False
    return (
        left.asset_class,
        left.issuer_namespace,
        left.issuer_id,
    ) == (
        right.asset_class,
        right.issuer_namespace,
        right.issuer_id,
    )


def _normalised_underlying_id(value: Any) -> str:
    text = str(value or "").strip().upper()
    for suffix in ("-USDT-SWAP", "-USDT", "_USDT", "USDT"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text or "UNRESOLVED"


def classify_asset_identity(
    adapter: "VenueAdapter", row: Mapping[str, Any]
) -> AssetIdentity:
    """Classify only from explicit venue metadata; unknown values fail closed."""
    native_symbol = str(row.get(adapter.symbol_field) or "").strip()
    if adapter.venue == "bybit":
        issuer_id = _normalised_underlying_id(row.get("baseCoin") or native_symbol)
        symbol_type = str(row.get("symbolType") or "").strip().lower()
        if symbol_type == "stock":
            asset_class = ASSET_CLASS_EQUITY_ISSUER
            namespace = "bybit_stock_underlying"
        elif symbol_type in {"commodity", "forex", "etf"}:
            asset_class = ASSET_CLASS_TRADFI_OTHER
            namespace = "bybit_tradfi_underlying"
        elif symbol_type == "innovation":
            asset_class = ASSET_CLASS_CRYPTO_TOKEN
            namespace = "crypto_asset"
        else:
            asset_class = ASSET_CLASS_UNCLASSIFIED
            namespace = "unresolved"
    elif adapter.venue == "okx":
        issuer_id = _normalised_underlying_id(
            row.get("uly") or row.get("instFamily") or native_symbol
        )
        category = str(row.get("instCategory") or "").strip()
        if category == "1":
            asset_class = ASSET_CLASS_CRYPTO_TOKEN
            namespace = "crypto_asset"
        elif category == "3":
            asset_class = ASSET_CLASS_EQUITY_ISSUER
            namespace = "okx_stock_underlying"
        elif category in {"4", "5", "6"}:
            asset_class = ASSET_CLASS_TRADFI_OTHER
            namespace = "okx_tradfi_underlying"
        else:
            asset_class = ASSET_CLASS_UNCLASSIFIED
            namespace = "unresolved"
    elif adapter.venue == "gate":
        issuer_id = _normalised_underlying_id(native_symbol)
        contract_type = str(row.get("contract_type") or "").strip().lower()
        if contract_type in {"stock", "stocks", "equity", "equities"}:
            asset_class = ASSET_CLASS_EQUITY_ISSUER
            namespace = "gate_stock_underlying"
        elif contract_type in {
            "metal",
            "metals",
            "index",
            "indices",
            "forex",
            "commodity",
            "commodities",
        }:
            asset_class = ASSET_CLASS_TRADFI_OTHER
            namespace = "gate_tradfi_underlying"
        elif contract_type in {"crypto", "cryptocurrency", "digital_asset"}:
            asset_class = ASSET_CLASS_CRYPTO_TOKEN
            namespace = "crypto_asset"
        else:
            asset_class = ASSET_CLASS_UNCLASSIFIED
            namespace = "unresolved"
    else:
        raise EventRegistryError(f"unsupported venue adapter: {adapter.venue}")
    evidence_class = (
        IDENTITY_EVIDENCE_LEGACY_UNCLASSIFIED
        if asset_class == ASSET_CLASS_UNCLASSIFIED
        else IDENTITY_EVIDENCE_VENUE_EXPLICIT_METADATA
    )
    return AssetIdentity(
        asset_class=asset_class,
        issuer_namespace=namespace,
        issuer_id=issuer_id,
        evidence_class=evidence_class,
    )


def project_legacy_asset_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a non-mutating, descriptive projection of a pre-v16 registry row."""
    projected = dict(record)
    identity = AssetIdentity(
        asset_class=ASSET_CLASS_UNCLASSIFIED,
        issuer_namespace="unresolved",
        issuer_id=_normalised_underlying_id(
            record.get("premarket_contract_id") or record.get("symbol")
        ),
        evidence_class=IDENTITY_EVIDENCE_LEGACY_UNCLASSIFIED,
    )
    projected.update(identity.as_record_fields())
    projected["evidence_use"] = "DESCRIPTIVE_ONLY"
    projected["capture_eligible"] = False
    return projected


def load_legacy_v2_projection() -> list[dict[str, Any]]:
    """Read the pinned v2 generation without mutating or promoting its records."""
    try:
        raw = REGISTRY_V2_PATH.read_bytes()
    except OSError as exc:
        raise EventRegistryError(f"legacy v2 registry is unreadable: {exc}") from exc
    if hashlib.sha256(raw).hexdigest() != LEGACY_V2_REGISTRY_SHA256:
        raise EventRegistryError("legacy v2 registry hash does not match the pinned source")
    rows = _load_registry_bytes(raw, path=REGISTRY_V2_PATH)
    if len(rows) != 16 or (rows[-1].get("record_hash") if rows else None) != (
        LEGACY_V2_HEAD_RECORD_HASH
    ):
        raise EventRegistryError("legacy v2 registry count/head does not match the pin")
    try:
        summary_raw = REGISTRY_V2_SUMMARY_PATH.read_bytes()
    except OSError as exc:
        raise EventRegistryError(f"legacy v2 summary is unreadable: {exc}") from exc
    if hashlib.sha256(summary_raw).hexdigest() != LEGACY_V2_SUMMARY_SHA256:
        raise EventRegistryError("legacy v2 summary hash does not match the pinned source")
    try:
        summary = json.loads(summary_raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise EventRegistryError(f"legacy v2 summary is unreadable: {exc}") from exc
    if not isinstance(summary, Mapping):
        raise EventRegistryError("legacy v2 summary is not an object")
    if summary.get("mutation_receipt_hash") != LEGACY_V2_MUTATION_RECEIPT_HASH:
        raise EventRegistryError("legacy v2 mutation receipt hash does not match the pin")
    try:
        receipt_raw = REGISTRY_V2_MUTATION_RECEIPT_PATH.read_bytes()
    except OSError as exc:
        raise EventRegistryError(
            f"legacy v2 mutation receipt is unreadable: {exc}"
        ) from exc
    if (
        hashlib.sha256(receipt_raw).hexdigest()
        != LEGACY_V2_MUTATION_RECEIPT_FILE_SHA256
    ):
        raise EventRegistryError(
            "legacy v2 mutation receipt file hash does not match the pinned source"
        )
    try:
        receipt = json.loads(receipt_raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise EventRegistryError(
            f"legacy v2 mutation receipt is unreadable: {exc}"
        ) from exc
    if not isinstance(receipt, Mapping):
        raise EventRegistryError("legacy v2 mutation receipt is not an object")
    receipt_without_hash = {
        key: value for key, value in receipt.items() if key != "receipt_hash"
    }
    if (
        receipt.get("receipt_hash") != LEGACY_V2_MUTATION_RECEIPT_HASH
        or canonical_hash(receipt_without_hash) != LEGACY_V2_MUTATION_RECEIPT_HASH
    ):
        raise EventRegistryError("legacy v2 mutation receipt content hash is invalid")
    receipt_expectations = {
        "schema": MUTATION_RECEIPT_SCHEMA,
        "mutation_seq": 0,
        "previous_mutation_receipt_hash": None,
        "registry_path_name": REGISTRY_V2_PATH.name,
        "registry_sha256": LEGACY_V2_REGISTRY_SHA256,
        "registry_entries": 16,
        "registry_head_record_hash": LEGACY_V2_HEAD_RECORD_HASH,
        "summary_content_hash": LEGACY_V2_SUMMARY_CONTENT_HASH,
        "plan_id": "premarket_perp_capture_20260822_v15",
        "plan_hash": (
            "41accb18028f6ccbee59264c8564d36ff9efd3d2c28aea9119e3a2d2741a062c"
        ),
    }
    for field, expected in receipt_expectations.items():
        if receipt.get(field) != expected:
            raise EventRegistryError(
                f"legacy v2 mutation receipt {field} does not match the pin"
            )
    if _summary_content_hash(summary) != LEGACY_V2_SUMMARY_CONTENT_HASH:
        raise EventRegistryError("legacy v2 summary content hash does not match receipt")
    projected: list[dict[str, Any]] = []
    for row in rows:
        item = project_legacy_asset_identity(row)
        item["legacy_origin"] = {
            "schema": REGISTRY_V2_SCHEMA,
            "registry_sha256": LEGACY_V2_REGISTRY_SHA256,
            "record_hash": row.get("record_hash"),
        }
        projected.append(item)
    return projected


def _is_sha256_text(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _preflight_is_exact(
    receipt: Any,
    *,
    write_class: str,
    run_id: str,
    decision: str,
    action: str,
) -> bool:
    """Accept only the current gate schema and external trust-root identity."""
    return bool(
        isinstance(receipt, Mapping)
        and receipt.get("schema") == risk_gate.PREFLIGHT_RESULT_SCHEMA
        and receipt.get("ok") is True
        and receipt.get("verified") is True
        and receipt.get("decision") == decision
        and receipt.get("write_class") == write_class
        and receipt.get("run_id") == run_id
        and receipt.get("action") == action
        and receipt.get("plan_id") == trust_root.PLAN_ID
        and receipt.get("plan_hash") == trust_root.PLAN_HASH
        and _is_sha256_text(receipt.get("resolved_paths_hash"))
    )


@dataclass(frozen=True)
class RegistryLockOwner:
    path: Path
    owner_pid: int
    owner_host: str
    run_id: str
    nonce: str
    plan_hash: str
    acquired_at_utc: str


def acquire_registry_lock(
    path: Path,
    *,
    run_id: str,
    plan_hash: str,
) -> RegistryLockOwner:
    """Atomically claim the short metadata-registry critical section."""
    if not run_id.strip():
        raise EventRegistryError("registry lock run_id is required")
    if len(plan_hash) != 64:
        raise EventRegistryError("registry lock requires a SHA-256 plan hash")
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = RegistryLockOwner(
        path=path,
        owner_pid=os.getpid(),
        owner_host=socket.gethostname(),
        run_id=run_id,
        nonce=secrets.token_hex(32),
        plan_hash=plan_hash,
        acquired_at_utc=utc_now_iso(),
    )
    payload = {
        "schema": "premarket_perp_registry_lock_v1",
        "owner_pid": owner.owner_pid,
        "owner_host": owner.owner_host,
        "run_id": owner.run_id,
        "nonce": owner.nonce,
        "plan_hash": owner.plan_hash,
        "acquired_at_utc": owner.acquired_at_utc,
    }
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise EventRegistryError(f"REGISTRY_LOCKED: {path}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise
    return owner


def _assert_registry_lock_owner(owner: RegistryLockOwner) -> None:
    try:
        payload = json.loads(owner.path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EventRegistryError(
            f"registry lock owner mismatch or unreadable lock: {owner.path}"
        ) from exc
    expected = {
        "owner_pid": owner.owner_pid,
        "owner_host": owner.owner_host,
        "run_id": owner.run_id,
        "nonce": owner.nonce,
        "plan_hash": owner.plan_hash,
        "acquired_at_utc": owner.acquired_at_utc,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise EventRegistryError(f"registry lock owner mismatch: {owner.path}")


def release_registry_lock(owner: RegistryLockOwner) -> None:
    """Release only the exact claim we acquired; stale locks remain fail-closed."""
    _assert_registry_lock_owner(owner)
    owner.path.unlink()


@contextmanager
def registry_lock(
    path: Path,
    *,
    run_id: str,
    plan_hash: str,
):
    owner = acquire_registry_lock(path, run_id=run_id, plan_hash=plan_hash)
    try:
        yield owner
    finally:
        release_registry_lock(owner)


@dataclass(frozen=True)
class VenueAdapter:
    venue: str
    url: str
    params: dict[str, str]
    symbol_field: str
    t0_field: str
    t0_unit: str                 # "ms" or "s"
    t0_semantics: str
    # Which named timestamp this field actually is. Declared here, beside the field,
    # because it was previously inferred from the venue name in the normaliser - so
    # changing which field a venue reads left the kind quietly saying the old thing.
    t0_kind: str
    t0_precision_sec: int
    caveats: tuple[str, ...] = ()
    rows_path: tuple[str, ...] = ()
    # Bybit pages its instrument list and caps a page at 500. Following the cursor is
    # not an optimisation: without it the registry silently held 500 of 833 linear
    # instruments and looked complete while doing so.
    cursor_path: tuple[str, ...] = ()
    cursor_param: str = ""
    max_pages: int = 20

    def rows(self, payload: Any) -> list[Mapping[str, Any]]:
        current: Any = payload
        for key in self.rows_path:
            if not isinstance(current, Mapping):
                raise EventRegistryError(
                    f"venue {self.venue} rows path is missing before {key}"
                )
            current = current.get(key)
        if not isinstance(current, list):
            raise EventRegistryError(
                f"venue {self.venue} rows payload is not an array"
            )
        if not all(isinstance(row, Mapping) for row in current):
            raise EventRegistryError(
                f"venue {self.venue} rows contain a non-object element"
            )
        return list(current)

    def next_cursor(self, payload: Any) -> str:
        if not self.cursor_param:
            return ""
        current: Any = payload
        for key in self.cursor_path:
            if not isinstance(current, Mapping):
                return ""
            current = current.get(key)
        return str(current or "").strip()


@dataclass(frozen=True)
class VenueSurface:
    surface_id: str
    venue: str
    url: str
    params: dict[str, str]
    rows_path: tuple[str, ...]
    native_id_field: str
    instrument_type_field: str | None = None
    expected_instrument_type: str | None = None
    unexpected_instrument_type_policy: str = "REJECT"

    def rows(self, payload: Any) -> list[Mapping[str, Any]]:
        current: Any = payload
        for key in self.rows_path:
            if not isinstance(current, Mapping):
                raise EventRegistryError(
                    f"surface {self.surface_id} rows path is missing before {key}"
                )
            current = current.get(key)
        if not isinstance(current, list):
            raise EventRegistryError(
                f"surface {self.surface_id} rows payload is not an array"
            )
        if not all(isinstance(row, Mapping) for row in current):
            raise EventRegistryError(
                f"surface {self.surface_id} rows contain a non-object element"
            )
        return list(current)


SURFACES: tuple[VenueSurface, ...] = (
    VenueSurface(
        surface_id="bybit_linear_prelaunch",
        venue="bybit",
        url="https://api.bybit.com/v5/market/instruments-info",
        params={"category": "linear", "status": "PreLaunch", "limit": "1000"},
        rows_path=("result", "list"),
        native_id_field="symbol",
        instrument_type_field="contractType",
        expected_instrument_type="LinearPerpetual",
    ),
    VenueSurface(
        surface_id="bybit_linear_trading",
        venue="bybit",
        url="https://api.bybit.com/v5/market/instruments-info",
        params={"category": "linear", "status": "Trading", "limit": "1000"},
        rows_path=("result", "list"),
        native_id_field="symbol",
        instrument_type_field="contractType",
        expected_instrument_type="LinearPerpetual",
        unexpected_instrument_type_policy="FILTER",
    ),
    VenueSurface(
        surface_id="okx_swap",
        venue="okx",
        url="https://www.okx.com/api/v5/public/instruments",
        params={"instType": "SWAP"},
        rows_path=("data",),
        native_id_field="instId",
        instrument_type_field="instType",
        expected_instrument_type="SWAP",
    ),
    VenueSurface(
        surface_id="okx_futures",
        venue="okx",
        url="https://www.okx.com/api/v5/public/instruments",
        params={"instType": "FUTURES"},
        rows_path=("data",),
        native_id_field="instId",
        instrument_type_field="instType",
        expected_instrument_type="FUTURES",
    ),
    VenueSurface(
        surface_id="gate_usdt_contracts",
        venue="gate",
        url="https://api.gateio.ws/api/v4/futures/usdt/contracts",
        params={},
        rows_path=(),
        native_id_field="name",
    ),
)


LIFECYCLE_SCHEDULED = "SCHEDULED"
LIFECYCLE_ACTIVE_PREMARKET = "ACTIVE_PREMARKET"
LIFECYCLE_TRANSITION_SCHEDULED = "TRANSITION_SCHEDULED"
LIFECYCLE_TRANSITIONED_STANDARD = "TRANSITIONED_STANDARD"
LIFECYCLE_CANCELLED = "CANCELLED"
LIFECYCLE_DELISTING = "DELISTING"
LIFECYCLE_DELISTED = "DELISTED"
LIFECYCLE_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class LifecycleObservation:
    surface_id: str
    venue: str
    native_contract_id: str
    venue_underlying_id: str | None
    phase: str
    launch_ts: int | None
    transition_ts: int | None
    explicit_terminal: bool


@dataclass(frozen=True)
class RelevantIdentitySnapshot:
    surface_id: str
    raw_row_count: int
    observations_by_id: Mapping[str, LifecycleObservation]
    relevant_ids: tuple[str, ...]
    explicit_terminal_ids: tuple[str, ...]
    missing_tracked_ids: tuple[str, ...]
    relevant_identity_set_sha256: str
    complete: bool
    problems: tuple[str, ...]


def _filter_surface_instrument_rows(
    surface: VenueSurface,
    rows: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    filtered = list(rows)
    if not surface.expected_instrument_type:
        return filtered
    type_field = surface.instrument_type_field
    if not type_field:
        raise EventRegistryError(
            f"surface {surface.surface_id} has no instrument type field"
        )
    wrong = [
        str(row.get(type_field) or "")
        for row in filtered
        if str(row.get(type_field) or "") != surface.expected_instrument_type
    ]
    if not wrong:
        return filtered
    if surface.unexpected_instrument_type_policy == "FILTER":
        return [
            row
            for row in filtered
            if str(row.get(type_field) or "") == surface.expected_instrument_type
        ]
    if surface.unexpected_instrument_type_policy == "REJECT":
        raise EventRegistryError(
            f"surface {surface.surface_id} requires "
            f"{surface.expected_instrument_type}; received {sorted(set(wrong))}"
        )
    raise EventRegistryError(
        f"surface {surface.surface_id} has an invalid type policy"
    )


def _bybit_success_code_is_exact(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    value = payload.get("retCode")
    return bool(
        (type(value) is int and value == 0)
        or (type(value) is str and value == "0")
    )


def _surface_payload_rows(surface: VenueSurface, payload: Any) -> list[Mapping[str, Any]]:
    if surface.venue == "bybit":
        if not _bybit_success_code_is_exact(payload):
            raise EventRegistryError("bybit surface payload has no successful retCode")
        result = payload.get("result")
        if not isinstance(result, Mapping) or not isinstance(result.get("list"), list):
            raise EventRegistryError("bybit surface payload has no result.list array")
        category = result.get("category")
        if not isinstance(category, str) or category != "linear":
            raise EventRegistryError("bybit surface payload category is not linear")
    elif surface.venue == "okx":
        if not isinstance(payload, Mapping) or str(payload.get("code")) != "0":
            raise EventRegistryError("okx surface payload has no successful code")
        if not isinstance(payload.get("data"), list):
            raise EventRegistryError("okx surface payload has no data array")
    elif surface.venue == "gate" and not isinstance(payload, list):
        raise EventRegistryError("gate surface payload is not an array")
    rows = surface.rows(payload)
    native_ids: list[str] = []
    for row in rows:
        native_id = row.get(surface.native_id_field)
        if (
            not isinstance(native_id, str)
            or not native_id
            or native_id != native_id.strip()
            or any(character.isspace() for character in native_id)
        ):
            raise EventRegistryError(
                f"surface {surface.surface_id} row has no canonical "
                f"{surface.native_id_field}"
            )
        if surface.venue == "bybit":
            status = row.get("status")
            is_prelisting = row.get("isPreListing")
            if (
                not isinstance(status, str)
                or not status
                or status != status.strip()
                or not isinstance(is_prelisting, bool)
            ):
                raise EventRegistryError(
                    f"surface {surface.surface_id} row has invalid lifecycle fields"
                )
            expected_status = surface.params.get("status")
            expected_is_prelisting = expected_status == "PreLaunch"
            if (
                status != expected_status
                or is_prelisting is not expected_is_prelisting
            ):
                raise EventRegistryError(
                    f"surface {surface.surface_id} row does not match queried surface"
                )
            launch_raw = row.get("launchTime")
            if launch_raw not in (None, "") and _to_seconds(launch_raw, "ms") is None:
                raise EventRegistryError(
                    f"surface {surface.surface_id} row has invalid launchTime"
                )
        elif surface.venue == "okx":
            state = row.get("state")
            rule_type = row.get("ruleType")
            # ruleType may be empty: OKX returns "" for an instrument under no special
            # rule. Measured 2026-08-23, exactly one live SWAP row does so
            # (JP225-USDT-SWAP, state preopen), and requiring it non-empty rejected the
            # whole 454-row surface, made every refresh incomplete and blocked every
            # registry write. An absent rule is a fact about the instrument, not a
            # malformed payload; downstream simply never matches it as pre_market.
            if (
                not isinstance(state, str)
                or not state
                or state != state.strip()
                or not isinstance(rule_type, str)
                or rule_type != rule_type.strip()
            ):
                raise EventRegistryError(
                    f"surface {surface.surface_id} row has invalid lifecycle fields"
                )
            list_time = row.get("listTime")
            if list_time not in (None, "") and _to_seconds(list_time, "ms") is None:
                raise EventRegistryError(
                    f"surface {surface.surface_id} row has invalid listTime"
                )
            switch_time = row.get("preMktSwTime")
            if switch_time not in (None, "") and _to_seconds(switch_time, "ms") is None:
                raise EventRegistryError(
                    f"surface {surface.surface_id} row has invalid preMktSwTime"
                )
        elif surface.venue == "gate":
            status = row.get("status")
            if (
                not isinstance(status, str)
                or not status
                or status != status.strip()
                or not isinstance(row.get("is_pre_market"), bool)
                or (
                    "in_delisting" in row
                    and not isinstance(row.get("in_delisting"), bool)
                )
            ):
                raise EventRegistryError(
                    f"surface {surface.surface_id} row has invalid lifecycle fields"
                )
            create_time = row.get("create_time")
            if (
                create_time not in (None, "")
                and _to_seconds(create_time, "s") is None
            ):
                raise EventRegistryError(
                    f"surface {surface.surface_id} row has invalid create_time"
                )
        native_ids.append(native_id)
    if len(native_ids) != len(set(native_ids)):
        raise EventRegistryError(
            f"surface {surface.surface_id} contains duplicate native contract ids"
        )
    return _filter_surface_instrument_rows(surface, rows)


def fetch_surface(surface: VenueSurface, fetch: Any) -> list[Mapping[str, Any]]:
    payload = fetch(surface, dict(surface.params))
    return _surface_payload_rows(surface, payload)


def fetch_required_surfaces(venue: str, fetch: Any) -> dict[str, list[Mapping[str, Any]]]:
    surfaces = [surface for surface in SURFACES if surface.venue == venue]
    if not surfaces:
        raise EventRegistryError(f"unknown venue surface set: {venue}")
    return {
        surface.surface_id: fetch_surface(surface, fetch)
        for surface in surfaces
    }


def classify_lifecycle(
    surface: VenueSurface,
    row: Mapping[str, Any],
    *,
    now_ts: int,
    was_tracked: bool,
) -> LifecycleObservation | None:
    native_id = str(row.get(surface.native_id_field) or "").strip()
    if not native_id:
        return None
    underlying = str(row.get("uly") or row.get("instFamily") or "").strip() or None
    launch_ts: int | None = None
    transition_ts: int | None = None
    phase = LIFECYCLE_UNKNOWN
    terminal = False
    if surface.venue == "bybit":
        launch_ts = _to_seconds(row.get("launchTime"), "ms")
        status = str(row.get("status") or "")
        if status == "PreLaunch" and row.get("isPreListing") is True:
            phase = LIFECYCLE_SCHEDULED if launch_ts and launch_ts > now_ts else LIFECYCLE_ACTIVE_PREMARKET
        elif was_tracked and status == "Trading" and row.get("isPreListing") is False:
            phase, terminal = LIFECYCLE_TRANSITIONED_STANDARD, True
        elif status in {"Settled", "Closed", "Cancelled", "Canceled"}:
            phase, terminal = LIFECYCLE_CANCELLED, True
    elif surface.venue == "okx":
        state = str(row.get("state") or "").lower()
        rule_type = str(row.get("ruleType") or "")
        launch_ts = _to_seconds(row.get("listTime"), "ms")
        transition_ts = _to_seconds(row.get("preMktSwTime"), "ms")
        if state in {"expired", "suspend"}:
            phase, terminal = LIFECYCLE_DELISTED, True
        elif rule_type == "pre_market" and state in {"preopen", "live"}:
            if transition_ts is not None and transition_ts > now_ts:
                phase = LIFECYCLE_TRANSITION_SCHEDULED
            else:
                phase = (
                    LIFECYCLE_SCHEDULED
                    if state == "preopen"
                    else LIFECYCLE_ACTIVE_PREMARKET
                )
        elif rule_type in {"xperp", "normal"} and was_tracked:
            phase, terminal = LIFECYCLE_TRANSITIONED_STANDARD, True
    elif surface.venue == "gate":
        status = str(row.get("status") or "").lower()
        in_delisting = row.get("in_delisting") is True
        if status in {"delisted"}:
            phase, terminal = LIFECYCLE_DELISTED, True
        elif in_delisting or status == "delisting":
            phase, terminal = LIFECYCLE_DELISTING, True
        elif status in {"cancelled", "canceled"}:
            phase, terminal = LIFECYCLE_CANCELLED, True
        elif status == "prelaunch" and row.get("is_pre_market") is True:
            phase = LIFECYCLE_SCHEDULED
        elif status == "trading" and row.get("is_pre_market") is True:
            phase = LIFECYCLE_ACTIVE_PREMARKET
        elif status == "trading" and was_tracked and row.get("is_pre_market") is False:
            phase, terminal = LIFECYCLE_TRANSITIONED_STANDARD, True
    return LifecycleObservation(
        surface_id=surface.surface_id,
        venue=surface.venue,
        native_contract_id=native_id,
        venue_underlying_id=underlying,
        phase=phase,
        launch_ts=launch_ts,
        transition_ts=transition_ts,
        explicit_terminal=terminal,
    )


def build_relevant_identity_snapshot(
    surface: VenueSurface,
    rows: Sequence[Mapping[str, Any]],
    *,
    now_ts: int,
    tracked_ids: set[str] | frozenset[str] | Sequence[str],
    classification_ids: set[str] | frozenset[str] | Sequence[str] = (),
) -> RelevantIdentitySnapshot:
    """Bind completeness to identities and lifecycle evidence, never row count alone."""
    rows = _filter_surface_instrument_rows(surface, rows)
    tracked = {str(value) for value in tracked_ids}
    classification_known = tracked | {str(value) for value in classification_ids}
    observations: dict[str, LifecycleObservation] = {}
    seen_native_ids: set[str] = set()
    identity_rows: list[dict[str, Any]] = []
    problems: list[str] = []
    adapter = next(item for item in ADAPTERS if item.venue == surface.venue)
    for row in rows:
        native_id = str(row.get(surface.native_id_field) or "").strip()
        if not native_id:
            continue
        if native_id in seen_native_ids:
            raise EventRegistryError(
                f"duplicate native contract id on {surface.surface_id}: {native_id}"
            )
        seen_native_ids.add(native_id)
        lifecycle = classify_lifecycle(
            surface,
            row,
            now_ts=now_ts,
            was_tracked=native_id in classification_known,
        )
        if lifecycle is None:
            continue
        if lifecycle.phase == LIFECYCLE_UNKNOWN and native_id not in tracked:
            continue
        observations[native_id] = lifecycle
        asset_identity = classify_asset_identity(adapter, row)
        identity_rows.append(
            {
                "native_contract_id": native_id,
                "venue_underlying_id": lifecycle.venue_underlying_id,
                "phase": lifecycle.phase,
                "explicit_terminal": lifecycle.explicit_terminal,
                "asset_identity_hash": asset_identity.as_record_fields()[
                    "asset_identity_hash"
                ],
            }
        )
        if native_id in tracked and lifecycle.phase == LIFECYCLE_UNKNOWN:
            problems.append(
                f"UNKNOWN_LIFECYCLE:{surface.surface_id}:{native_id}"
            )
    seen = set(observations)
    missing = tuple(sorted(tracked - seen))
    if missing:
        problems.append(
            f"MISSING_TRACKED_IDENTITIES:{surface.surface_id}:" + ",".join(missing)
        )
    terminal_ids = tuple(
        sorted(
            native_id
            for native_id, observation in observations.items()
            if observation.explicit_terminal
        )
    )
    relevant_ids = tuple(sorted(seen - set(terminal_ids)))
    identity_rows.sort(key=lambda item: item["native_contract_id"])
    return RelevantIdentitySnapshot(
        surface_id=surface.surface_id,
        raw_row_count=len(rows),
        observations_by_id=dict(sorted(observations.items())),
        relevant_ids=relevant_ids,
        explicit_terminal_ids=terminal_ids,
        missing_tracked_ids=missing,
        relevant_identity_set_sha256=canonical_hash(
            {
                "schema": "premarket_relevant_identity_set_v1",
                "surface_id": surface.surface_id,
                "identities": identity_rows,
            }
        ),
        complete=not problems,
        problems=tuple(problems),
    )


def apply_cross_surface_terminal_precedence(
    *,
    identity_snapshots: Mapping[str, RelevantIdentitySnapshot],
    current_active: Mapping[str, Sequence[str]],
    relevant_identity_ids_by_surface: Mapping[str, Sequence[str]],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Resolve sequential cross-surface races with explicit terminal evidence.

    PreLaunch and Trading (or SWAP and FUTURES) are fetched sequentially. Seeing a
    stale active row on the earlier surface and a terminal row on the later surface
    must never keep the generation active. The raw per-surface hashes still preserve
    the conflict; this function only resolves the current lifecycle state.
    """
    terminal_by_venue: dict[str, set[str]] = {
        adapter.venue: set() for adapter in ADAPTERS
    }
    surfaces_by_id = {surface.surface_id: surface for surface in SURFACES}
    for surface_id, snapshot in identity_snapshots.items():
        surface = surfaces_by_id.get(surface_id)
        if surface is None:
            raise EventRegistryError(
                f"unknown surface in terminal precedence: {surface_id}"
            )
        terminal_by_venue[surface.venue].update(snapshot.explicit_terminal_ids)

    resolved_active = {
        venue: sorted(set(contracts) - terminal_by_venue.get(venue, set()))
        for venue, contracts in sorted(current_active.items())
    }
    resolved_relevant: dict[str, list[str]] = {}
    for surface_id, contracts in sorted(relevant_identity_ids_by_surface.items()):
        surface = surfaces_by_id.get(surface_id)
        if surface is None:
            raise EventRegistryError(
                f"unknown relevant-identity surface: {surface_id}"
            )
        resolved_relevant[surface_id] = sorted(
            set(contracts) - terminal_by_venue[surface.venue]
        )
    return resolved_active, resolved_relevant


def build_terminal_lifecycle_observations(
    *,
    identity_snapshots: Mapping[str, RelevantIdentitySnapshot],
    rows_by_surface: Mapping[str, Sequence[Mapping[str, Any]]],
    previous_active: Mapping[str, Mapping[str, int]],
    received_at_utc: str,
) -> list[dict[str, Any]]:
    """Persist the strongest tracked terminal cause in the append-only registry.

    A summary says what is current, so its terminal-id set legitimately changes on the
    next refresh. It cannot be the only record of why a lifecycle generation ended.
    When a venue supplies no applicable terminal timestamp, the completed-refresh
    time is stored as an OBSERVED_LIFECYCLE detection proxy, never as the actual
    transition time. When sequential venue surfaces expose simultaneous terminal
    causes for one native id, an exact OKX xperp preMktSwTime deterministically wins
    over a detection-time proxy. It is retained only for a lifecycle generation that
    was already tracked locally.
    """
    received = _parse_explicit_utc(received_at_utc)
    if received is None:
        raise EventRegistryError("transition detection received_at_utc is invalid")
    detection_ts = int(received.timestamp())
    adapters = {adapter.venue: adapter for adapter in ADAPTERS}
    selected: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    key_order: list[tuple[str, str]] = []
    for surface in SURFACES:
        snapshot = identity_snapshots.get(surface.surface_id)
        if snapshot is None:
            raise EventRegistryError(
                f"transition detection snapshot is missing: {surface.surface_id}"
            )
        rows = _filter_surface_instrument_rows(
            surface, rows_by_surface.get(surface.surface_id, ())
        )
        rows_by_id = {
            str(row.get(surface.native_id_field) or "").strip(): row
            for row in rows
            if str(row.get(surface.native_id_field) or "").strip()
        }
        for native_id, lifecycle in snapshot.observations_by_id.items():
            key = (surface.venue, native_id)
            if not lifecycle.explicit_terminal:
                continue
            generation = previous_active.get(surface.venue, {}).get(native_id)
            if generation is None:
                # A venue may expose already-cancelled historical instruments that
                # this registry never tracked. They do not terminate a local episode.
                continue
            row = rows_by_id.get(native_id)
            if row is None:
                raise EventRegistryError(
                    f"transitioned identity row is missing: {surface.surface_id}:{native_id}"
                )
            asset_identity = classify_asset_identity(adapters[surface.venue], row)
            has_exact_transition = (
                surface.venue == "okx"
                and str(row.get("ruleType") or "") in {"xperp", "normal"}
                and lifecycle.transition_ts is not None
            )
            effective_phase = (
                LIFECYCLE_TRANSITIONED_STANDARD
                if has_exact_transition
                else lifecycle.phase
            )
            timestamp_ts = (
                int(lifecycle.transition_ts)
                if has_exact_transition
                else detection_ts
            )
            source_class = (
                SOURCE_VENUE_INSTRUMENT_METADATA
                if has_exact_transition
                else SOURCE_OBSERVED_LIFECYCLE
            )
            source_identity = (
                "okx:instrument_metadata:preMktSwTime"
                if has_exact_transition
                else (
                    f"{surface.venue}:surface:{surface.surface_id}:"
                    f"terminal_detection:{effective_phase}"
                )
            )
            caveats = (
                (
                    "VENUE_METADATA_TRANSITION_TIMESTAMP",
                    f"LIFECYCLE_PHASE={effective_phase}",
                )
                if has_exact_transition
                else (
                    (
                        "DETECTION_TIME_PROXY_NOT_ACTUAL_TRANSITION_TIME"
                        if effective_phase == LIFECYCLE_TRANSITIONED_STANDARD
                        else "DETECTION_TIME_PROXY_NOT_ACTUAL_TERMINAL_TIME"
                    ),
                    f"LIFECYCLE_PHASE={effective_phase}",
                )
            )
            observation = make_timestamp_observation(
                episode_id=make_episode_id(surface.venue, native_id, int(generation)),
                venue=surface.venue,
                premarket_contract_id=native_id,
                spot_symbol=None,
                timestamp_kind=TIMESTAMP_TRANSITION,
                timestamp_ts=timestamp_ts,
                instrument_role=(
                    "standard_perp"
                    if effective_phase == LIFECYCLE_TRANSITIONED_STANDARD
                    else "premarket_perp"
                ),
                source_class=source_class,
                source_identity=source_identity,
                source_url=surface.url,
                received_at_utc=received_at_utc,
                precision_sec=1,
                caveats=caveats,
                lifecycle_generation=int(generation),
                asset_identity=asset_identity,
            )
            observation["lifecycle_phase"] = effective_phase
            evidence_rank = 1 if has_exact_transition else 0
            current = selected.get(key)
            if current is None:
                key_order.append(key)
            if current is None or evidence_rank > current[0]:
                selected[key] = (evidence_rank, observation)
    return [selected[key][1] for key in key_order]


def registry_authority_state_hash(
    *,
    active_generations: Mapping[str, Any],
    lifecycle_high_water: Mapping[str, Any],
    metadata_refresh_received_at: str,
    raw_universe_rows_by_surface: Mapping[str, int],
    relevant_identity_hashes_by_surface: Mapping[str, str],
    explicit_terminal_ids_by_surface: Mapping[str, Sequence[str]] | None = None,
) -> str:
    return canonical_hash(
        {
            "schema": "premarket_registry_authority_state_v2",
            ACTIVE_LIFECYCLE_GENERATIONS_FIELD: active_generations,
            LIFECYCLE_GENERATION_HIGH_WATER_FIELD: lifecycle_high_water,
            LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD: (
                metadata_refresh_received_at
            ),
            RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD: raw_universe_rows_by_surface,
            RELEVANT_IDENTITY_HASHES_BY_SURFACE_FIELD: (
                relevant_identity_hashes_by_surface
            ),
            EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD: (
                explicit_terminal_ids_by_surface or {}
            ),
        }
    )


ADAPTERS: tuple[VenueAdapter, ...] = (
    VenueAdapter(
        venue="bybit",
        url="https://api.bybit.com/v5/market/instruments-info",
        params={"category": "linear", "status": "PreLaunch", "limit": "1000"},
        symbol_field="symbol",
        t0_field="launchTime",
        t0_unit="ms",
        t0_semantics="venue-declared contract launch time",
        t0_kind=TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
        t0_precision_sec=1,
        rows_path=("result", "list"),
        cursor_path=("result", "nextPageCursor"),
        cursor_param="cursor",
    ),
    VenueAdapter(
        venue="okx",
        url="https://www.okx.com/api/v5/public/instruments",
        # SWAP, because this project observes perpetuals and OKX carries its
        # pre-market perpetuals there. Measured 2026-08-23: instType=SWAP holds three
        # rows with ruleType=pre_market (ANTHROPIC, MOONSHOT, OPENAI - the same
        # underlyings Bybit lists), while instType=FUTURES holds none at all. The
        # dated xperp contracts under FUTURES are a different instrument class and
        # carry an expTime.
        params={"instType": "SWAP"},
        symbol_field="instId",
        t0_field="listTime",
        t0_unit="ms",
        t0_semantics="venue-declared instrument listing time",
        t0_kind=TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
        t0_precision_sec=1,
        rows_path=("data",),
    ),
    VenueAdapter(
        venue="gate",
        url="https://api.gateio.ws/api/v4/futures/usdt/contracts",
        params={},
        symbol_field="name",
        # Gate documents launch_time as the contract expiry timestamp, not the start
        # of trading.  create_time is retained only as contract-creation provenance;
        # neither field is an official spot t0 or observed first trade.
        t0_field="create_time",
        t0_unit="s",
        t0_semantics="venue-declared contract creation time",
        t0_kind=TIMESTAMP_CONTRACT_CREATED,
        t0_precision_sec=1,
        caveats=("CONTRACT_CREATION_NOT_TRADING_START",),
        rows_path=(),
    ),
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _writer_refresh_completed_at_utc() -> str:
    """Writer-owned timestamp taken only after all live venue fetches finish."""
    return utc_now_iso()


def _to_seconds(raw: Any, unit: str) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    if isinstance(raw, bool) or unit not in {"ms", "s"}:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    seconds = value / 1000 if unit == "ms" else value
    if seconds < 1:
        return None
    return int(seconds)


def _is_premarket_row(adapter: VenueAdapter, row: Mapping[str, Any]) -> bool:
    """Return rows relevant to lifecycle history, including terminal transition rows."""
    if adapter.venue == "bybit":
        status = str(row.get("status") or "")
        return str(row.get("contractType") or "") == "LinearPerpetual" and (
            (status == "PreLaunch" and row.get("isPreListing") is True)
            or status in {"Settled", "Closed", "Cancelled", "Canceled"}
        )
    if adapter.venue == "okx":
        rule_type = str(row.get("ruleType") or "")
        instrument_type = str(row.get("instType") or "")
        # A pre-market perpetual. This is the row this project is about.
        if instrument_type in {"SWAP", "FUTURES"} and rule_type == "pre_market":
            return True
        # xperp is the post-conversion lifecycle row; retain it only when the venue
        # supplies an explicit switch time so it cannot masquerade as a new launch.
        return (
            instrument_type == "FUTURES"
            and rule_type == "xperp"
            and (
                _to_seconds(row.get("preMktSwTime"), "ms") is not None
                or str(row.get("state") or "").lower() in {"live", "expired"}
            )
        )
    if adapter.venue == "gate":
        status = str(row.get("status") or "").lower()
        return row.get("is_pre_market") is True and status in {
            "prelaunch",
            "trading",
            "delisting",
            "delisted",
            "cancelled",
            "canceled",
        }
    return False


def _is_currently_active_premarket_row(
    adapter: VenueAdapter, row: Mapping[str, Any]
) -> bool:
    """Return only contracts currently advertised as an active pre-market market."""
    if adapter.venue == "bybit":
        return (
            str(row.get("contractType") or "") == "LinearPerpetual"
            and str(row.get("status") or "") == "PreLaunch"
            and row.get("isPreListing") is True
        )
    if adapter.venue == "okx":
        return (
            str(row.get("instType") or "") in {"SWAP", "FUTURES"}
            and str(row.get("ruleType") or "") == "pre_market"
            and str(row.get("state") or "").lower() in {"preopen", "live"}
        )
    if adapter.venue == "gate":
        status = str(row.get("status") or "").lower()
        return (
            row.get("is_pre_market") is True
            and row.get("in_delisting") is not True
            and status in {"prelaunch", "trading"}
        )
    return _is_premarket_row(adapter, row)


def event_id(venue: str, symbol: str) -> str:
    return f"{venue}:{symbol}"


def make_episode_id(
    venue: str, native_premarket_contract_id: str, lifecycle_generation: int = 0
) -> str:
    """Return identity that survives schedule and symbol-mapping revisions."""
    if venue not in {adapter.venue for adapter in ADAPTERS}:
        raise EventRegistryError(f"unknown venue: {venue}")
    native_id = native_premarket_contract_id.strip()
    if not native_id:
        raise EventRegistryError("native pre-market contract id is required")
    if lifecycle_generation < 0:
        raise EventRegistryError("lifecycle_generation must be non-negative")
    digest = canonical_hash(
        {
            "venue": venue,
            "native_premarket_contract_id": native_id,
            "lifecycle_generation": lifecycle_generation,
        }
    )
    return f"ep_{digest}"


def _stream_id(
    *,
    episode_id: str,
    timestamp_kind: str,
    instrument_role: str,
    source_class: str,
    source_identity: str,
) -> str:
    return canonical_hash(
        {
            "episode_id": episode_id,
            "timestamp_kind": timestamp_kind,
            "instrument_role": instrument_role,
            "source_class": source_class,
            "source_identity": source_identity,
        }
    )


def make_timestamp_observation(
    *,
    episode_id: str,
    venue: str,
    premarket_contract_id: str,
    spot_symbol: str | None,
    timestamp_kind: str,
    timestamp_ts: int,
    instrument_role: str,
    source_class: str,
    source_identity: str,
    source_url: str,
    received_at_utc: str,
    precision_sec: int = 1,
    caveats: Sequence[str] = (),
    lifecycle_generation: int = 0,
    asset_identity: AssetIdentity | None = None,
) -> dict[str, Any]:
    """Build one validated timestamp observation for an immutable listing episode."""
    if not episode_id.strip():
        raise EventRegistryError("episode_id is required")
    if venue not in {adapter.venue for adapter in ADAPTERS}:
        raise EventRegistryError(f"unknown venue: {venue}")
    if not premarket_contract_id.strip():
        raise EventRegistryError("premarket_contract_id is required")
    if timestamp_kind not in TIMESTAMP_KINDS:
        raise EventRegistryError(f"unknown timestamp kind: {timestamp_kind}")
    if int(timestamp_ts) <= 0:
        raise EventRegistryError("timestamp_ts must be positive")
    if instrument_role not in INSTRUMENT_ROLES:
        raise EventRegistryError(f"unknown instrument role: {instrument_role}")
    if source_class not in SOURCE_CLASSES:
        raise EventRegistryError(f"unknown source class: {source_class}")
    if not source_identity.strip():
        raise EventRegistryError("source_identity is required")
    if not source_url.strip():
        raise EventRegistryError("source_url is required")
    if not received_at_utc.strip():
        raise EventRegistryError("received_at_utc is required")
    if int(lifecycle_generation) < 0:
        raise EventRegistryError("lifecycle_generation must be non-negative")
    if timestamp_kind == TIMESTAMP_OFFICIAL_SPOT_T0:
        if source_class != SOURCE_OFFICIAL_ANNOUNCEMENT:
            raise EventRegistryError("official_spot_t0 requires OFFICIAL_ANNOUNCEMENT")
        if instrument_role != "spot" or not str(spot_symbol or "").strip():
            raise EventRegistryError("official_spot_t0 requires a spot instrument and symbol")
    if timestamp_kind == TIMESTAMP_FIRST_TRADE and source_class != SOURCE_OBSERVED_PUBLIC_TRADE:
        raise EventRegistryError("first_trade_ts requires OBSERVED_PUBLIC_TRADE")

    if asset_identity is None:
        if source_class == SOURCE_OFFICIAL_ANNOUNCEMENT:
            default_identity_evidence = IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION
        elif source_class == SOURCE_VENUE_INSTRUMENT_METADATA:
            default_identity_evidence = IDENTITY_EVIDENCE_VENUE_EXPLICIT_METADATA
        else:
            default_identity_evidence = IDENTITY_EVIDENCE_LEGACY_UNCLASSIFIED
        asset_identity = AssetIdentity(
            asset_class=ASSET_CLASS_UNCLASSIFIED,
            issuer_namespace="unresolved",
            issuer_id=_normalised_underlying_id(premarket_contract_id),
            evidence_class=default_identity_evidence,
        )
    if not isinstance(asset_identity, AssetIdentity):
        raise EventRegistryError("asset_identity must be an AssetIdentity")
    acceptance_anchor = (
        timestamp_kind == TIMESTAMP_OFFICIAL_SPOT_T0
        and asset_identity.asset_class == ASSET_CLASS_CRYPTO_TOKEN
        and asset_identity.evidence_class
        in {
            IDENTITY_EVIDENCE_VENUE_EXPLICIT_METADATA,
            IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
        }
    )
    observation = {
        "schema": REGISTRY_SCHEMA,
        "record_type": "timestamp_observation",
        "event_id": episode_id,
        "episode_id": episode_id,
        "venue": venue,
        "symbol": premarket_contract_id,
        "premarket_contract_id": premarket_contract_id,
        "spot_symbol": spot_symbol,
        "lifecycle_generation": int(lifecycle_generation),
        "premarket_contract_launch_ts": None,
        "official_spot_t0": None,
        "first_trade_ts": None,
        "transition_ts": None,
        "contract_created_ts": None,
        "timestamp_kind": timestamp_kind,
        "timestamp_ts": int(timestamp_ts),
        "instrument_role": instrument_role,
        "t0_source_class": source_class,
        "source_class": source_class,
        "source_identity": source_identity,
        "source_url": source_url,
        "received_at_utc": received_at_utc,
        "observed_at_utc": received_at_utc,
        "t0_precision_sec": int(precision_sec),
        "caveats": list(caveats),
        "evidence_use": "ACCEPTANCE_ANCHOR" if acceptance_anchor else "DESCRIPTIVE_ONLY",
        "capture_eligible": acceptance_anchor,
    }
    observation.update(asset_identity.as_record_fields())
    observation[timestamp_kind] = int(timestamp_ts)
    observation["stream_id"] = _stream_id(
        episode_id=episode_id,
        timestamp_kind=timestamp_kind,
        instrument_role=instrument_role,
        source_class=source_class,
        source_identity=source_identity,
    )
    return observation


@dataclass(frozen=True)
class VenueFetch:
    rows: list[Mapping[str, Any]]
    pages: int
    truncated: bool


def fetch_venue(adapter: VenueAdapter, fetch: Any) -> VenueFetch:
    """Follow the venue's cursor to the end - and say so when we stop early.

    A page cap has to exist or a misbehaving cursor loops forever, but stopping at it
    silently is how a partial universe passes for a whole one."""
    rows: list[Mapping[str, Any]] = []
    cursor = ""
    seen_cursors: set[str] = set()
    pages = 0
    while True:
        params = dict(adapter.params)
        if cursor:
            params[adapter.cursor_param] = cursor
        payload = fetch(adapter, params)
        _validate_payload_shape(adapter, payload)
        rows.extend(adapter.rows(payload))
        pages += 1
        cursor = adapter.next_cursor(payload)
        if not cursor:
            return VenueFetch(rows=rows, pages=pages, truncated=False)
        if cursor in seen_cursors:
            raise EventRegistryError(
                f"{adapter.venue} repeated pagination cursor: {cursor}"
            )
        seen_cursors.add(cursor)
        if pages >= adapter.max_pages:
            return VenueFetch(rows=rows, pages=pages, truncated=True)


def _validate_payload_shape(adapter: VenueAdapter, payload: Any) -> None:
    if adapter.venue == "bybit":
        if not _bybit_success_code_is_exact(payload):
            raise EventRegistryError("bybit payload has no successful retCode")
        result = payload.get("result")
        if not isinstance(result, Mapping) or not isinstance(result.get("list"), list):
            raise EventRegistryError("bybit payload has no result.list array")
        cursor = result.get("nextPageCursor")
        if "nextPageCursor" not in result or not isinstance(cursor, str):
            raise EventRegistryError(
                "bybit payload has no explicit string nextPageCursor"
            )
        if cursor != cursor.strip():
            raise EventRegistryError(
                "bybit payload has no canonical nextPageCursor"
            )
        category = result.get("category")
        if not isinstance(category, str) or category != "linear":
            raise EventRegistryError("bybit payload category is not linear")
        return
    if adapter.venue == "okx":
        if not isinstance(payload, Mapping) or str(payload.get("code")) != "0":
            raise EventRegistryError("okx payload has no successful code")
        if not isinstance(payload.get("data"), list):
            raise EventRegistryError("okx payload has no data array")
        return
    if adapter.venue == "gate":
        if not isinstance(payload, list):
            raise EventRegistryError("gate payload is not an array")
        return
    raise EventRegistryError(f"unsupported venue adapter: {adapter.venue}")


def normalise_rows(
    adapter: VenueAdapter,
    rows: Sequence[Mapping[str, Any]],
    *,
    observed_at_utc: str,
    lifecycle_generations: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    lifecycle_generations = lifecycle_generations or {}
    for row in rows:
        # Discovery may create an episode only from a genuinely active/scheduled
        # pre-market row. Historical terminal rows are admitted exclusively by
        # build_terminal_lifecycle_observations after proving that the generation
        # was already tracked locally.
        if not _is_currently_active_premarket_row(adapter, row):
            continue
        symbol = str(row.get(adapter.symbol_field) or "").strip()
        if not symbol:
            continue
        t0_ts = _to_seconds(row.get(adapter.t0_field), adapter.t0_unit)
        if t0_ts is None:
            # A missing launch time is not a zero: the event is simply not usable yet.
            continue
        lifecycle_generation = int(lifecycle_generations.get(symbol, 0))
        timestamp_kind = adapter.t0_kind
        asset_identity = classify_asset_identity(adapter, row)
        observation = make_timestamp_observation(
            episode_id=make_episode_id(
                adapter.venue, symbol, lifecycle_generation
            ),
            venue=adapter.venue,
            premarket_contract_id=symbol,
            spot_symbol=None,
            timestamp_kind=timestamp_kind,
            timestamp_ts=t0_ts,
            instrument_role="premarket_perp",
            source_class=SOURCE_VENUE_INSTRUMENT_METADATA,
            source_identity=(
                f"{adapter.venue}:instrument_metadata:{adapter.t0_field}"
            ),
            source_url=adapter.url,
            received_at_utc=observed_at_utc,
            precision_sec=adapter.t0_precision_sec,
            caveats=adapter.caveats,
            lifecycle_generation=lifecycle_generation,
            asset_identity=asset_identity,
        )
        # Kept only as a descriptive v1 read-compatibility projection.  Runtime
        # selection never reads this generic field.
        observation.update(
            {
                "legacy_event_id": event_id(adapter.venue, symbol),
                "t0_ts": t0_ts,
                "t0_source_field": adapter.t0_field,
                "t0_semantics": adapter.t0_semantics,
            }
        )
        events.append(observation)
        if adapter.venue == "okx":
            transition_ts = _to_seconds(row.get("preMktSwTime"), "ms")
            if transition_ts is not None:
                received_moment = _parse_explicit_utc(observed_at_utc)
                if received_moment is None:
                    raise EventRegistryError("observed_at_utc is invalid")
                if transition_ts <= int(received_moment.timestamp()):
                    # A stale active surface does not prove that the transition
                    # happened. The terminal surface must close the tracked
                    # generation before this timestamp becomes terminal evidence.
                    continue
                lifecycle_phase = LIFECYCLE_TRANSITION_SCHEDULED
                transition = make_timestamp_observation(
                    episode_id=observation["episode_id"],
                    venue=adapter.venue,
                    premarket_contract_id=symbol,
                    spot_symbol=None,
                    timestamp_kind=TIMESTAMP_TRANSITION,
                    timestamp_ts=transition_ts,
                    instrument_role="standard_perp",
                    source_class=SOURCE_VENUE_INSTRUMENT_METADATA,
                    source_identity="okx:instrument_metadata:preMktSwTime",
                    source_url=adapter.url,
                    received_at_utc=observed_at_utc,
                    precision_sec=1,
                    caveats=("VENUE_METADATA_TRANSITION_TIMESTAMP",),
                    lifecycle_generation=lifecycle_generation,
                    asset_identity=asset_identity,
                )
                transition.update(
                    {
                        "lifecycle_phase": lifecycle_phase,
                        "legacy_event_id": event_id(adapter.venue, symbol),
                        "t0_source_field": "preMktSwTime",
                        "t0_semantics": (
                            "venue-declared transition from pre-market to xperp"
                        ),
                    }
                )
                events.append(transition)
    events.sort(key=lambda item: (item["timestamp_ts"], item["venue"], item["symbol"]))
    return events


def normalise(adapter: VenueAdapter, payload: Any, *, observed_at_utc: str) -> list[dict[str, Any]]:
    """Single-page convenience wrapper."""
    return normalise_rows(adapter, adapter.rows(payload), observed_at_utc=observed_at_utc)


def _active_contract_ids(observed: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    active: dict[str, set[str]] = {adapter.venue: set() for adapter in ADAPTERS}
    for entry in observed:
        venue = str(entry.get("venue") or "")
        contract = str(entry.get("premarket_contract_id") or "")
        if venue in active and contract:
            active[venue].add(contract)
    return {venue: sorted(contracts) for venue, contracts in sorted(active.items())}


def _active_contract_ids_from_rows(
    staged_rows: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[str]]:
    """Return every currently advertised pre-market id, even without a usable t0."""
    active: dict[str, set[str]] = {adapter.venue: set() for adapter in ADAPTERS}
    adapters = {adapter.venue: adapter for adapter in ADAPTERS}
    for venue, rows in staged_rows.items():
        adapter = adapters.get(venue)
        if adapter is None:
            continue
        for row in rows:
            if not _is_currently_active_premarket_row(adapter, row):
                continue
            contract = str(row.get(adapter.symbol_field) or "").strip()
            if contract:
                active[venue].add(contract)
    return {venue: sorted(contracts) for venue, contracts in sorted(active.items())}


def _lifecycle_contract_ids_from_rows(
    staged_rows: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[str]]:
    relevant: dict[str, set[str]] = {adapter.venue: set() for adapter in ADAPTERS}
    adapters = {adapter.venue: adapter for adapter in ADAPTERS}
    for venue, rows in staged_rows.items():
        adapter = adapters.get(venue)
        if adapter is None:
            continue
        for row in rows:
            if not _is_premarket_row(adapter, row):
                continue
            contract = str(row.get(adapter.symbol_field) or "").strip()
            if contract:
                relevant[venue].add(contract)
    return {
        venue: sorted(contracts)
        for venue, contracts in sorted(relevant.items())
    }


def _derived_lifecycle_high_water(
    existing: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {
        adapter.venue: {} for adapter in ADAPTERS
    }
    for entry in existing:
        venue = str(entry.get("venue") or "")
        contract = str(
            entry.get("premarket_contract_id") or entry.get("symbol") or ""
        ).strip()
        if venue not in result or not contract:
            continue
        generation = int(entry.get("lifecycle_generation", 0) or 0)
        result[venue][contract] = max(
            result[venue].get(contract, -1), generation
        )
    return {
        venue: dict(sorted(values.items()))
        for venue, values in sorted(result.items())
    }


def _parse_lifecycle_generation_state(
    raw: Any,
    *,
    field_name: str,
) -> dict[str, dict[str, int]]:
    if not isinstance(raw, Mapping):
        raise EventRegistryError(
            f"LIFECYCLE_GENERATION_STATE_MISSING: {field_name} is not an object"
        )
    expected_venues = {adapter.venue for adapter in ADAPTERS}
    if set(raw) != expected_venues:
        raise EventRegistryError(
            f"LIFECYCLE_GENERATION_STATE_MISSING: {field_name} venue set is invalid"
        )
    result: dict[str, dict[str, int]] = {}
    for venue in sorted(expected_venues):
        contracts = raw.get(venue)
        if not isinstance(contracts, Mapping):
            raise EventRegistryError(
                f"LIFECYCLE_GENERATION_STATE_MISSING: {field_name}.{venue} is invalid"
            )
        parsed: dict[str, int] = {}
        for contract, generation in contracts.items():
            if (
                not isinstance(contract, str)
                or not contract
                or contract != contract.strip()
                or isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation < 0
            ):
                raise EventRegistryError(
                    f"LIFECYCLE_GENERATION_STATE_MISSING: "
                    f"{field_name}.{venue} entry is invalid"
                )
            parsed[contract] = generation
        result[venue] = dict(sorted(parsed.items()))
    return result


def _parse_active_contract_ids(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, Mapping):
        raise EventRegistryError(
            "LIFECYCLE_GENERATION_STATE_MISSING: invalid active contract state"
        )
    expected_venues = {adapter.venue for adapter in ADAPTERS}
    if set(raw) != expected_venues:
        raise EventRegistryError(
            "LIFECYCLE_GENERATION_STATE_MISSING: active contract venue set is invalid"
        )
    result: dict[str, list[str]] = {}
    for venue in sorted(expected_venues):
        values = raw.get(venue)
        if not isinstance(values, list) or not all(
            isinstance(value, str)
            and bool(value)
            and value == value.strip()
            for value in values
        ):
            raise EventRegistryError(
                "LIFECYCLE_GENERATION_STATE_MISSING: invalid active contract state"
            )
        if len(values) != len(set(values)):
            raise EventRegistryError(
                "LIFECYCLE_GENERATION_STATE_MISSING: duplicate active contract id"
            )
        result[venue] = sorted(values)
    return result


def _parse_raw_universe_counts(raw: Any) -> dict[str, int]:
    if not isinstance(raw, Mapping) or set(raw) != {
        adapter.venue for adapter in ADAPTERS
    }:
        raise EventRegistryError("raw universe row counts are invalid")
    result: dict[str, int] = {}
    for adapter in ADAPTERS:
        value = raw.get(adapter.venue)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EventRegistryError("raw universe row counts are invalid")
        result[adapter.venue] = value
    return dict(sorted(result.items()))


def _summary_raw_universe_counts(summary_path: Path) -> dict[str, int] | None:
    if not summary_path.is_file():
        return None
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EventRegistryError(f"raw universe summary is unreadable: {exc}") from exc
    if not isinstance(summary, Mapping):
        raise EventRegistryError("raw universe summary is invalid")
    if RAW_UNIVERSE_ROWS_FIELD not in summary:
        return None
    return _parse_raw_universe_counts(summary[RAW_UNIVERSE_ROWS_FIELD])


def _summary_raw_universe_counts_by_surface(
    summary_path: Path,
) -> dict[str, int] | None:
    if not summary_path.is_file():
        return None
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EventRegistryError(
            f"raw surface universe summary is unreadable: {exc}"
        ) from exc
    if not isinstance(summary, Mapping):
        raise EventRegistryError("raw surface universe summary is invalid")
    raw = summary.get(RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD)
    if raw is None:
        return None
    expected = {surface.surface_id for surface in SURFACES}
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise EventRegistryError("raw surface universe summary set is invalid")
    parsed: dict[str, int] = {}
    for surface_id in sorted(expected):
        value = raw.get(surface_id)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EventRegistryError(
                f"raw surface universe summary count is invalid: {surface_id}"
            )
        parsed[surface_id] = value
    return parsed


def _parse_surface_string_lists(raw: Any, *, field_name: str) -> dict[str, list[str]]:
    expected = {surface.surface_id for surface in SURFACES}
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise EventRegistryError(f"{field_name} surface set is invalid")
    parsed: dict[str, list[str]] = {}
    for surface_id in sorted(expected):
        values = raw.get(surface_id)
        if (
            not isinstance(values, list)
            or not all(
                isinstance(value, str) and value and value == value.strip()
                for value in values
            )
            or len(values) != len(set(values))
        ):
            raise EventRegistryError(f"{field_name}.{surface_id} is invalid")
        parsed[surface_id] = sorted(values)
    return parsed


def _summary_relevant_identity_ids(summary_path: Path) -> dict[str, list[str]] | None:
    if not summary_path.is_file():
        return None
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EventRegistryError(
            f"relevant identity summary is unreadable: {exc}"
        ) from exc
    if not isinstance(summary, Mapping):
        raise EventRegistryError("relevant identity summary is invalid")
    if RELEVANT_IDENTITY_IDS_BY_SURFACE_FIELD not in summary:
        return None
    return _parse_surface_string_lists(
        summary[RELEVANT_IDENTITY_IDS_BY_SURFACE_FIELD],
        field_name=RELEVANT_IDENTITY_IDS_BY_SURFACE_FIELD,
    )


def _summary_explicit_terminal_identity_ids(
    summary_path: Path,
) -> dict[str, list[str]] | None:
    """Load the previous terminal set for lifecycle classification only.

    Terminal ids are intentionally not required to remain present on later refreshes.
    They only preserve enough one-generation memory to recognise a terminal row while
    another surface still reports a stale active copy of the same native contract.
    """
    if not summary_path.is_file():
        return None
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EventRegistryError(
            f"explicit terminal identity summary is unreadable: {exc}"
        ) from exc
    if not isinstance(summary, Mapping):
        raise EventRegistryError("explicit terminal identity summary is invalid")
    if EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD not in summary:
        return None
    return _parse_surface_string_lists(
        summary[EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD],
        field_name=EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD,
    )


def _load_lifecycle_generation_state(
    summary_path: Path,
    *,
    existing: Sequence[Mapping[str, Any]],
    current_active_for_legacy: Mapping[str, Sequence[str]] | None = None,
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Return exact active generations and monotonic per-contract high-water state."""
    derived_high_water = _derived_lifecycle_high_water(existing)
    summary: Mapping[str, Any] | None = None
    if summary_path.is_file():
        try:
            loaded = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EventRegistryError(
                f"LIFECYCLE_GENERATION_STATE_MISSING: summary unreadable: {exc}"
            ) from exc
        if not isinstance(loaded, Mapping):
            raise EventRegistryError(
                "LIFECYCLE_GENERATION_STATE_MISSING: summary root is not an object"
            )
        summary = loaded

    if summary is not None and (
        ACTIVE_LIFECYCLE_GENERATIONS_FIELD in summary
        or LIFECYCLE_GENERATION_HIGH_WATER_FIELD in summary
    ):
        if (
            ACTIVE_LIFECYCLE_GENERATIONS_FIELD not in summary
            or LIFECYCLE_GENERATION_HIGH_WATER_FIELD not in summary
        ):
            raise EventRegistryError(
                "LIFECYCLE_GENERATION_STATE_MISSING: active and high-water state "
                "must be persisted together"
            )
        active = _parse_lifecycle_generation_state(
            summary[ACTIVE_LIFECYCLE_GENERATIONS_FIELD],
            field_name=ACTIVE_LIFECYCLE_GENERATIONS_FIELD,
        )
        high_water = _parse_lifecycle_generation_state(
            summary[LIFECYCLE_GENERATION_HIGH_WATER_FIELD],
            field_name=LIFECYCLE_GENERATION_HIGH_WATER_FIELD,
        )
        active_ids = _parse_active_contract_ids(summary.get(ACTIVE_CONTRACTS_FIELD))
        for venue in active:
            if sorted(active[venue]) != active_ids[venue]:
                raise EventRegistryError(
                    "LIFECYCLE_GENERATION_STATE_MISSING: active ids and generations differ"
                )
            for contract, generation in active[venue].items():
                if high_water[venue].get(contract, -1) != generation:
                    raise EventRegistryError(
                        "LIFECYCLE_GENERATION_STATE_MISSING: active generation must "
                        "equal its high-water generation"
                    )
            for contract, generation in derived_high_water[venue].items():
                if high_water[venue].get(contract, -1) < generation:
                    raise EventRegistryError(
                        "LIFECYCLE_GENERATION_STATE_MISSING: high-water state regressed"
                    )
        return active, high_water

    # One-way compatibility bootstrap for fixtures and pre-v9 receipts.  Every new
    # mutation writes both structured fields, after which they are authoritative.
    if summary is not None and summary.get(ACTIVE_CONTRACTS_FIELD) is not None:
        active_ids = _parse_active_contract_ids(summary[ACTIVE_CONTRACTS_FIELD])
    elif current_active_for_legacy is not None:
        active_ids = {
            adapter.venue: sorted(
                set(current_active_for_legacy.get(adapter.venue, ()))
            )
            for adapter in ADAPTERS
        }
    else:
        active_ids = _active_contract_ids(existing)
    active: dict[str, dict[str, int]] = {
        adapter.venue: {} for adapter in ADAPTERS
    }
    high_water = {
        venue: dict(values) for venue, values in derived_high_water.items()
    }
    for venue, contracts in active_ids.items():
        for contract in contracts:
            generation = high_water[venue].get(contract, 0)
            active[venue][contract] = generation
            high_water[venue][contract] = max(
                high_water[venue].get(contract, -1), generation
            )
    return (
        {venue: dict(sorted(values.items())) for venue, values in sorted(active.items())},
        {
            venue: dict(sorted(values.items()))
            for venue, values in sorted(high_water.items())
        },
    )


def _allocate_current_lifecycle_state(
    current_active: Mapping[str, Sequence[str]],
    *,
    previous_active: Mapping[str, Mapping[str, int]],
    previous_high_water: Mapping[str, Mapping[str, int]],
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    active: dict[str, dict[str, int]] = {}
    high_water: dict[str, dict[str, int]] = {
        adapter.venue: dict(previous_high_water.get(adapter.venue, {}))
        for adapter in ADAPTERS
    }
    for adapter in ADAPTERS:
        venue = adapter.venue
        active[venue] = {}
        for contract in sorted(set(current_active.get(venue, ()))):
            if contract in previous_active.get(venue, {}):
                generation = int(previous_active[venue][contract])
            else:
                generation = int(high_water[venue].get(contract, -1)) + 1
            active[venue][contract] = generation
            high_water[venue][contract] = max(
                int(high_water[venue].get(contract, -1)), generation
            )
    return (
        {venue: dict(sorted(values.items())) for venue, values in sorted(active.items())},
        {
            venue: dict(sorted(values.items()))
            for venue, values in sorted(high_water.items())
        },
    )


def _bind_relevant_lifecycle_generations(
    lifecycle_contracts: Mapping[str, Sequence[str]],
    *,
    active_generations: Mapping[str, Mapping[str, int]],
    previous_active: Mapping[str, Mapping[str, int]],
    lifecycle_high_water: Mapping[str, Mapping[str, int]],
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Bind terminal rows to their just-ended episode without keeping it active."""
    bound = {
        adapter.venue: dict(active_generations.get(adapter.venue, {}))
        for adapter in ADAPTERS
    }
    high_water = {
        adapter.venue: dict(lifecycle_high_water.get(adapter.venue, {}))
        for adapter in ADAPTERS
    }
    for adapter in ADAPTERS:
        venue = adapter.venue
        for contract in sorted(set(lifecycle_contracts.get(venue, ()))):
            if contract in bound[venue]:
                generation = bound[venue][contract]
            elif contract in previous_active.get(venue, {}):
                generation = int(previous_active[venue][contract])
            else:
                # Lifecycle-only terminal rows may be historical venue inventory.
                # They cannot allocate a local generation that was never observed
                # active or scheduled by this registry.
                continue
            bound[venue][contract] = generation
            high_water[venue][contract] = max(
                int(high_water[venue].get(contract, -1)), generation
            )
    return (
        {venue: dict(sorted(values.items())) for venue, values in sorted(bound.items())},
        {
            venue: dict(sorted(values.items()))
            for venue, values in sorted(high_water.items())
        },
    )


def _summary_active_contracts(
    summary_path: Path,
    *,
    existing: Sequence[Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Read active lifecycle ids for a non-metadata mutation without changing them."""
    active, _high_water = _load_lifecycle_generation_state(
        summary_path, existing=existing
    )
    return {
        venue: sorted(generations)
        for venue, generations in sorted(active.items())
    }


def _summary_active_lifecycle_generations(
    summary_path: Path,
    *,
    existing: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    active, _high_water = _load_lifecycle_generation_state(
        summary_path, existing=existing
    )
    return active


def _summary_lifecycle_generation_high_water(
    summary_path: Path,
    *,
    existing: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    _active, high_water = _load_lifecycle_generation_state(
        summary_path, existing=existing
    )
    return high_water


def _summary_complete_metadata_refresh_received_at(summary_path: Path) -> str:
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EventRegistryError(
            f"complete metadata refresh anchor is unreadable: {exc}"
        ) from exc
    value = (
        summary.get(LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD)
        if isinstance(summary, Mapping)
        else None
    )
    if _parse_explicit_utc(value) is None:
        raise EventRegistryError("complete metadata refresh anchor is invalid")
    return str(value)


def _bind_lifecycle_generations(
    observed: Sequence[Mapping[str, Any]],
    *,
    active_generations: Mapping[str, Mapping[str, int]],
) -> list[dict[str, Any]]:
    bound: list[dict[str, Any]] = []
    for raw in observed:
        entry = dict(raw)
        venue = str(entry.get("venue") or "")
        contract = str(entry.get("premarket_contract_id") or entry.get("symbol") or "")
        try:
            generation = int(active_generations[venue][contract])
        except (KeyError, TypeError, ValueError) as exc:
            raise EventRegistryError(
                f"observed contract has no active lifecycle generation: {venue}:{contract}"
            ) from exc
        entry["lifecycle_generation"] = generation
        entry["episode_id"] = make_episode_id(venue, contract, generation)
        entry["event_id"] = entry["episode_id"]
        entry["stream_id"] = _stream_id(
            episode_id=entry["episode_id"],
            timestamp_kind=str(entry.get("timestamp_kind") or ""),
            instrument_role=str(entry.get("instrument_role") or ""),
            source_class=str(entry.get("source_class") or ""),
            source_identity=str(entry.get("source_identity") or ""),
        )
        bound.append(entry)
    return bound


def load_registry(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or REGISTRY_PATH
    if not path.is_file():
        return []
    return _load_registry_bytes(path.read_bytes(), path=path)


def _load_registry_bytes(raw: bytes, *, path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EventRegistryError(f"{path}: registry is not UTF-8: {exc}") from exc
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError as exc:
            raise EventRegistryError(f"{path}:{number}: unreadable registry line: {exc}") from exc
        entries.append(entry)
    return entries


def latest_by_event(entries: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """The current view: the most recent revision of each event."""
    current: dict[str, dict[str, Any]] = {}
    for entry in entries:
        current[str(entry.get("event_id"))] = dict(entry)
    return current


def _stream_heads(
    entries: Iterable[Mapping[str, Any]],
) -> dict[str, tuple[int, int, dict[str, Any]]]:
    heads: dict[str, tuple[int, int, dict[str, Any]]] = {}
    for position, raw in enumerate(entries):
        entry = dict(raw)
        stream_id = str(entry.get("stream_id") or "").strip()
        if not stream_id:
            continue
        revision = int(entry.get("stream_revision", entry.get("revision", 0)) or 0)
        previous = heads.get(stream_id)
        if previous is None or (revision, position) > (previous[0], previous[1]):
            heads[stream_id] = (revision, position, entry)
    return heads


def materialize_episodes(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Materialize lifecycle episodes without collapsing independent source streams.

    Each timestamp/source pair has its own stream head.  The episode view combines
    those heads only after preserving their provenance.  Conflicting official spot
    timestamps deliberately make the episode ineligible instead of selecting the
    last value observed.
    """
    episodes: dict[str, dict[str, Any]] = {}
    for _revision, _position, head in _stream_heads(entries).values():
        timestamp_kind = str(head.get("timestamp_kind") or "").strip()
        if timestamp_kind not in TIMESTAMP_KINDS:
            continue
        episode_id = str(head.get("episode_id") or head.get("event_id"))
        episode = episodes.setdefault(
            episode_id,
            {
                "event_id": episode_id,
                "episode_id": episode_id,
                "venue": head.get("venue"),
                "symbol": head.get("symbol") or head.get("premarket_contract_id"),
                "premarket_contract_id": head.get("premarket_contract_id")
                or head.get("symbol"),
                "spot_symbol": head.get("spot_symbol"),
                "lifecycle_generation": int(head.get("lifecycle_generation", 0) or 0),
                "premarket_contract_launch_ts": None,
                "official_spot_t0": None,
                "first_trade_ts": None,
                "transition_ts": None,
                "contract_created_ts": None,
                "timestamp_observations": [],
                "official_conflict": False,
                "asset_identity_conflict": False,
                "asset_class": ASSET_CLASS_UNCLASSIFIED,
                "issuer_namespace": "unresolved",
                "issuer_id": _normalised_underlying_id(
                    head.get("premarket_contract_id") or head.get("symbol")
                ),
                "asset_identity_hash": None,
                "evidence_use": "DESCRIPTIVE_ONLY",
                "capture_eligible": False,
            },
        )
        if episode.get("venue") != head.get("venue"):
            raise EventRegistryError(f"episode {episode_id} spans multiple venues")
        if not episode.get("spot_symbol") and head.get("spot_symbol"):
            episode["spot_symbol"] = head.get("spot_symbol")

        kind = str(head["timestamp_kind"])
        value = int(head.get("timestamp_ts") or head.get(kind) or 0)
        if value <= 0:
            continue
        episode["timestamp_observations"].append(
            {
                "stream_id": head["stream_id"],
                "stream_revision": int(
                    head.get("stream_revision", head.get("revision", 0)) or 0
                ),
                "timestamp_kind": kind,
                "timestamp_ts": value,
                "instrument_role": head.get("instrument_role"),
                "source_class": head.get("source_class")
                or head.get("t0_source_class"),
                "source_identity": head.get("source_identity"),
                "source_url": head.get("source_url"),
                "received_at_utc": head.get("received_at_utc"),
                "t0_precision_sec": int(head.get("t0_precision_sec", 0) or 0),
                "lifecycle_phase": head.get("lifecycle_phase"),
                "caveats": list(head.get("caveats") or []),
                "attestation": dict(head.get("attestation") or {}),
                "record_hash": head.get("record_hash") or head.get("entry_hash"),
                "asset_class": head.get("asset_class") or ASSET_CLASS_UNCLASSIFIED,
                "issuer_namespace": head.get("issuer_namespace") or "unresolved",
                "issuer_id": head.get("issuer_id")
                or _normalised_underlying_id(
                    head.get("premarket_contract_id") or head.get("symbol")
                ),
                "identity_evidence_class": head.get("identity_evidence_class")
                or IDENTITY_EVIDENCE_LEGACY_UNCLASSIFIED,
                "asset_identity_hash": head.get("asset_identity_hash"),
            }
        )

    for episode in episodes.values():
        def identity_key(item: Mapping[str, Any]) -> tuple[str, str, str] | None:
            asset_class = str(item.get("asset_class") or ASSET_CLASS_UNCLASSIFIED)
            if asset_class == ASSET_CLASS_UNCLASSIFIED:
                return None
            return (
                asset_class,
                str(item.get("issuer_namespace") or ""),
                str(item.get("issuer_id") or ""),
            )

        known_identity_keys = {
            key
            for observation in episode["timestamp_observations"]
            if (key := identity_key(observation)) is not None
        }
        if len(known_identity_keys) > 1:
            episode["asset_identity_conflict"] = True
        elif len(known_identity_keys) == 1:
            asset_class, namespace, issuer_id = next(iter(known_identity_keys))
            episode.update(
                {
                    "asset_class": asset_class,
                    "issuer_namespace": namespace,
                    "issuer_id": issuer_id,
                    "asset_identity_hash": canonical_hash(
                        {
                            "asset_class": asset_class,
                            "issuer_namespace": namespace,
                            "issuer_id": issuer_id,
                        }
                    ),
                }
            )
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for observation in episode["timestamp_observations"]:
            by_kind.setdefault(observation["timestamp_kind"], []).append(observation)
        for kind, observations in by_kind.items():
            values = {int(item["timestamp_ts"]) for item in observations}
            if len(values) == 1:
                episode[kind] = next(iter(values))
            elif kind == TIMESTAMP_OFFICIAL_SPOT_T0:
                episode["official_conflict"] = True

        official_observations = [
            item
            for item in by_kind.get(TIMESTAMP_OFFICIAL_SPOT_T0, [])
            if item.get("source_class") == SOURCE_OFFICIAL_ANNOUNCEMENT
        ]
        official_values = {item["timestamp_ts"] for item in official_observations}
        if len(official_values) > 1:
            episode["official_conflict"] = True
            episode[TIMESTAMP_OFFICIAL_SPOT_T0] = None
        elif len(official_values) == 1 and not episode["official_conflict"]:
            episode[TIMESTAMP_OFFICIAL_SPOT_T0] = next(iter(official_values))
            episode["t0_source_class"] = SOURCE_OFFICIAL_ANNOUNCEMENT
            official = official_observations[0]
            episode["t0_precision_sec"] = official["t0_precision_sec"]
            episode["caveats"] = list(official["caveats"])
            episode["official_t0_provenance"] = {
                "stream_id": official["stream_id"],
                "stream_revision": official["stream_revision"],
                "record_hash": official["record_hash"],
                "source_class": official["source_class"],
                "source_identity": official["source_identity"],
                "source_url": official["source_url"],
                "received_at_utc": official["received_at_utc"],
                "t0_precision_sec": official["t0_precision_sec"],
                "caveats": list(official["caveats"]),
                "attestation": dict(official["attestation"]),
            }
            official_identity = identity_key(official)
            metadata_identity_keys = {
                key
                for item in episode["timestamp_observations"]
                if item.get("source_class") == SOURCE_VENUE_INSTRUMENT_METADATA
                and (key := identity_key(item)) is not None
            }
            acceptance_anchor = (
                not episode["asset_identity_conflict"]
                and official_identity is not None
                and official_identity[0] == ASSET_CLASS_CRYPTO_TOKEN
                and metadata_identity_keys == {official_identity}
            )
            episode["evidence_use"] = (
                "ACCEPTANCE_ANCHOR" if acceptance_anchor else "DESCRIPTIVE_ONLY"
            )
            episode["capture_eligible"] = acceptance_anchor

        episode["timestamp_observations"].sort(
            key=lambda item: (
                item["timestamp_kind"],
                str(item.get("source_class")),
                str(item.get("source_identity")),
            )
        )

    return sorted(
        episodes.values(),
        key=lambda item: (str(item.get("venue")), str(item.get("episode_id"))),
    )


def _record_hash(record: Mapping[str, Any]) -> str:
    return canonical_hash({k: v for k, v in record.items() if k != "record_hash"})


def _entry_hash(entry: Mapping[str, Any]) -> str:
    """Compatibility alias for tooling that still labels a registry row an entry."""
    return _record_hash(entry)


def _observation_fingerprint(observation: Mapping[str, Any]) -> str:
    """Hash semantic stream state while ignoring when the same state was re-read."""
    ignored = {
        "record_hash",
        "record_seq",
        "previous_record_hash",
        "stream_revision",
        "supersedes_record_hash",
        "revision",
        "entry_hash",
        "observed_at_utc",
        "received_at_utc",
    }
    semantic = {k: v for k, v in observation.items() if k not in ignored}
    attestation = semantic.get("attestation")
    if isinstance(attestation, Mapping):
        semantic["attestation"] = {
            key: value
            for key, value in attestation.items()
            if key != "lead_sec_at_attestation"
        }
    return canonical_hash(semantic)


def build_stream_revisions(
    existing: Sequence[Mapping[str, Any]],
    observed: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Bind changed observations to global and per-source append-only hash chains."""
    if existing:
        problems = _verify_records(existing)
        if problems:
            raise EventRegistryError(
                "existing registry lineage is invalid: " + "; ".join(problems)
            )

    stream_heads: dict[str, dict[str, Any]] = {}
    for raw in existing:
        stream_heads[str(raw["stream_id"])] = dict(raw)

    global_head = dict(existing[-1]) if existing else None
    appended: list[dict[str, Any]] = []
    for raw in observed:
        observation = dict(raw)
        stream_id = str(observation.get("stream_id") or "").strip()
        if not stream_id:
            raise EventRegistryError("timestamp observation has no stream_id")
        previous_stream = stream_heads.get(stream_id)
        if previous_stream is not None and _observation_fingerprint(
            previous_stream
        ) == _observation_fingerprint(observation):
            continue

        for field in (
            "record_hash",
            "record_seq",
            "previous_record_hash",
            "stream_revision",
            "supersedes_record_hash",
            "revision",
            "entry_hash",
        ):
            observation.pop(field, None)
        observation["schema"] = REGISTRY_SCHEMA
        observation["record_type"] = "timestamp_observation"
        observation["record_seq"] = (
            int(global_head["record_seq"]) + 1 if global_head is not None else 0
        )
        observation["previous_record_hash"] = (
            global_head.get("record_hash") if global_head is not None else None
        )
        observation["stream_revision"] = (
            int(previous_stream["stream_revision"]) + 1
            if previous_stream is not None
            else 0
        )
        observation["supersedes_record_hash"] = (
            previous_stream.get("record_hash") if previous_stream is not None else None
        )
        observation["record_hash"] = _record_hash(observation)
        appended.append(observation)
        stream_heads[stream_id] = observation
        global_head = observation
    return appended


def merge_observations(
    existing: Sequence[Mapping[str, Any]],
    observed: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Compatibility name for the v2 stream-revision builder."""
    return build_stream_revisions(existing, observed)


def append_entries(
    entries: Sequence[Mapping[str, Any]],
    path: Path | None = None,
    *,
    lock_owner: RegistryLockOwner,
) -> int:
    """Append already lineage-bound records and fsync them to stable storage."""
    path = path or REGISTRY_PATH
    expected_lock_path = (
        REGISTRY_LOCK_PATH
        if path.resolve(strict=False) == REGISTRY_PATH.resolve(strict=False)
        else path.with_suffix(".lock")
    )
    if lock_owner.path.resolve(strict=False) != expected_lock_path.resolve(strict=False):
        raise EventRegistryError("registry lock does not cover the target registry")
    _assert_registry_lock_owner(lock_owner)
    if not entries:
        return 0
    for entry in entries:
        if entry.get("record_hash") != _record_hash(entry):
            raise EventRegistryError("refusing to append a record with an invalid hash")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(json.dumps(dict(entry), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return len(entries)


def _parse_explicit_utc(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() != timezone.utc.utcoffset(None):
        return None
    return moment.astimezone(timezone.utc)


def _normalise_symbol(value: Any) -> str:
    return "".join(character for character in str(value or "").upper() if character.isalnum())


def _normalise_contract_market(venue: str, value: Any) -> str:
    """Map a native perpetual id to the comparable spot market identity.

    OKX appends ``-SWAP`` to the native perpetual id while its spot market does not.
    Bybit and Gate use only separators that ``_normalise_symbol`` already removes.
    Keeping the venue-specific transform here makes the contract/spot check strict
    without rejecting every legitimate OKX mapping.
    """
    text = str(value or "").upper().strip()
    if venue == "okx" and text.endswith("-SWAP"):
        text = text[:-5]
    elif venue == "okx":
        # OKX pre-market instruments are reported as dated FUTURES before their
        # conversion.  The YYMMDD/YYYYMMDD suffix is lifecycle metadata, not part of
        # the spot market identity.
        text = re.sub(r"-\d{6,8}$", "", text)
    return _normalise_symbol(text)


def market_symbols_equivalent(venue: str, contract: Any, spot_symbol: Any) -> bool:
    return bool(_normalise_symbol(spot_symbol)) and (
        _normalise_contract_market(venue, contract) == _normalise_symbol(spot_symbol)
    )


def _parse_quoted_utc(value: Any) -> datetime | None:
    text = " ".join(str(value or "").split())
    for form in (
        "%b %d, %Y, %I:%M%p UTC",
        "%B %d, %Y, %I:%M%p UTC",
        "%b %d, %Y, %H:%M UTC",
        "%B %d, %Y, %H:%M UTC",
        "%Y-%m-%d %H:%M UTC",
    ):
        try:
            return datetime.strptime(text, form).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return _parse_explicit_utc(text)


def _official_record_problems(
    entry: Mapping[str, Any],
    *,
    number: int,
    metadata_episode_ids: set[str],
    metadata_asset_identities: Mapping[str, set[tuple[str, str, str]]],
) -> list[str]:
    """Validate the semantics that make an OFFICIAL row an acceptance anchor."""
    problems: list[str] = []
    prefix = f"line {number}: "
    venue = str(entry.get("venue") or "")
    contract = str(entry.get("premarket_contract_id") or entry.get("symbol") or "")
    spot_symbol = str(entry.get("spot_symbol") or "")
    for field, value in (
        ("venue", venue),
        ("premarket_contract_id", contract),
        ("spot_symbol", spot_symbol),
    ):
        if (
            not value
            or value != value.strip()
            or any(character.isspace() for character in value)
        ):
            problems.append(prefix + f"official {field} is not canonical")
    try:
        generation = int(entry.get("lifecycle_generation", -1))
    except (TypeError, ValueError):
        generation = -1
    expected_episode = ""
    try:
        expected_episode = make_episode_id(venue, contract, generation)
    except (EventRegistryError, ValueError):
        pass
    if entry.get("episode_id") != expected_episode:
        problems.append(prefix + "official episode_id does not match venue/contract/generation")
    if expected_episode not in metadata_episode_ids:
        problems.append(prefix + "official episode has no matching metadata lifecycle record")
    if not market_symbols_equivalent(venue, contract, spot_symbol):
        problems.append(prefix + "official spot_symbol mapping does not match premarket contract")
    official_asset_class = str(
        entry.get("asset_class") or ASSET_CLASS_UNCLASSIFIED
    )
    official_identity = (
        official_asset_class,
        str(entry.get("issuer_namespace") or ""),
        str(entry.get("issuer_id") or ""),
    )
    acceptance_anchor = (
        entry.get("capture_eligible") is True
        and entry.get("evidence_use") == "ACCEPTANCE_ANCHOR"
    )
    if acceptance_anchor:
        if official_asset_class != ASSET_CLASS_CRYPTO_TOKEN:
            problems.append(prefix + "official acceptance anchor is not a crypto token")
        if official_identity not in metadata_asset_identities.get(expected_episode, set()):
            problems.append(
                prefix + "official asset identity does not match metadata lifecycle identity"
            )

    source_url = str(entry.get("source_url") or "")
    parsed = urllib.parse.urlsplit(source_url)
    try:
        source_port: int | str | None = parsed.port
    except ValueError:
        source_port = "INVALID"
    official_hosts = config.OFFICIAL_ANNOUNCEMENT_HOSTS.get(venue, ())
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() not in official_hosts
        or parsed.username is not None
        or parsed.password is not None
        or source_port is not None
        or "\\" in source_url
        or _has_forbidden_unicode_controls(source_url)
        or source_url != source_url.strip()
    ):
        problems.append(prefix + "official source_url is not an approved venue announcement host")
    received = _parse_explicit_utc(entry.get("received_at_utc"))
    if received is None:
        problems.append(prefix + "official received_at_utc is not explicit UTC")

    attestation = entry.get("attestation")
    if not isinstance(attestation, Mapping):
        problems.append(prefix + "official attestation object is missing")
        return problems
    if attestation.get("schema") != config.OFFICIAL_ATTESTATION_SCHEMA:
        problems.append(prefix + "official attestation schema is invalid")
    if str(attestation.get("announcement_url") or "") != source_url:
        problems.append(prefix + "attestation announcement_url does not match source_url")
    raw_attested_by = str(attestation.get("attested_by") or "")
    attested_by = raw_attested_by.strip()
    if (
        not attested_by
        or raw_attested_by != attested_by
        or _has_forbidden_unicode_controls(raw_attested_by)
        or entry.get("source_identity") != f"human_attestation:{attested_by}"
    ):
        problems.append(
            prefix
            + "attestation author is not canonical or does not match source_identity"
        )
    announced = _parse_explicit_utc(attestation.get("announced_utc"))
    if announced is None or int(announced.timestamp()) != int(entry.get("timestamp_ts") or 0):
        problems.append(prefix + "attestation announced_utc does not match official timestamp")
    if received is not None and announced is not None:
        actual_lead = int(announced.timestamp()) - int(received.timestamp())
        try:
            declared_lead = int(attestation.get("lead_sec_at_attestation"))
        except (TypeError, ValueError):
            declared_lead = -1
        if actual_lead < config.CAPTURE_WINDOW_BEFORE_SEC:
            problems.append(
                prefix + "official received_at_utc is noncausal or has insufficient lead"
            )
        if declared_lead != actual_lead:
            problems.append(prefix + "attestation lead does not equal t0 minus received_at")
    if int(entry.get("t0_precision_sec", 0) or 0) != 60:
        problems.append(prefix + "official attestation precision must be exactly 60 seconds")
    caveats = tuple(entry.get("caveats") or ())
    if caveats != ("OFFICIAL_T0_READ_BY_A_PERSON_FROM_ANNOUNCEMENT_PROSE",):
        problems.append(prefix + "official attestation caveat is missing or changed")
    quote = str(attestation.get("quoted_sentence") or "")
    quoted_time = str(attestation.get("quoted_time_text") or "")
    quoted_symbol = str(attestation.get("quoted_symbol_text") or "")
    for field, value in (
        ("quoted sentence", quote),
        ("quoted time fragment", quoted_time),
        ("quoted symbol fragment", quoted_symbol),
    ):
        if (
            not value
            or value != value.strip()
            or _has_forbidden_unicode_controls(value)
        ):
            problems.append(prefix + f"attestation {field} is not verbatim canonical text")
    if not quote or not quoted_time or quoted_time not in quote:
        problems.append(prefix + "attestation quoted time fragment is missing from sentence")
    if not quote or not quoted_symbol or quoted_symbol not in quote:
        problems.append(prefix + "attestation quoted symbol fragment is missing from sentence")
    if _normalise_symbol(quoted_symbol) != _normalise_symbol(spot_symbol):
        problems.append(prefix + "attestation quoted symbol does not match spot_symbol")
    quoted_moment = _parse_quoted_utc(quoted_time)
    if (
        quoted_moment is None
        or announced is None
        or int(quoted_moment.timestamp()) != int(announced.timestamp())
    ):
        problems.append(prefix + "attestation quoted time does not match announced_utc")
    return problems


def _verify_records(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    problems: list[str] = []
    metadata_episode_ids: set[str] = set()
    metadata_asset_identities: dict[str, set[tuple[str, str, str]]] = {}
    stream_heads: dict[str, Mapping[str, Any]] = {}
    previous_global_hash: str | None = None
    seen_hashes: set[str] = set()
    for index, entry in enumerate(entries):
        number = index + 1
        if entry.get("schema") != REGISTRY_SCHEMA:
            problems.append(f"line {number}: unknown registry schema")
        if entry.get("record_type") != "timestamp_observation":
            problems.append(f"line {number}: unknown record_type")
        if int(entry.get("record_seq", -1)) != index:
            problems.append(
                f"line {number}: record_seq {entry.get('record_seq')} does not follow {index - 1}"
            )
        if entry.get("previous_record_hash") != previous_global_hash:
            problems.append(f"line {number}: previous_record_hash does not match global head")
        claimed_hash = str(entry.get("record_hash") or "")
        if claimed_hash != _record_hash(entry):
            problems.append(f"line {number}: record_hash does not match its content")
        if claimed_hash in seen_hashes:
            problems.append(f"line {number}: duplicate record_hash")
        seen_hashes.add(claimed_hash)

        episode_id = str(entry.get("episode_id") or "").strip()
        if not episode_id:
            problems.append(f"line {number}: missing episode_id")
        try:
            generation = int(entry.get("lifecycle_generation", -1))
            expected_episode_id = make_episode_id(
                str(entry.get("venue") or ""),
                str(entry.get("premarket_contract_id") or entry.get("symbol") or ""),
                generation,
            )
            if episode_id != expected_episode_id:
                problems.append(
                    f"line {number}: episode_id does not match venue/contract/generation"
                )
        except (EventRegistryError, TypeError, ValueError):
            problems.append(f"line {number}: lifecycle_generation or episode identity is invalid")
        timestamp_kind = str(entry.get("timestamp_kind") or "")
        if timestamp_kind not in TIMESTAMP_KINDS:
            problems.append(f"line {number}: unknown timestamp_kind")
        if entry.get("source_class") not in SOURCE_CLASSES:
            problems.append(f"line {number}: unknown source_class")
        if entry.get("instrument_role") not in INSTRUMENT_ROLES:
            problems.append(f"line {number}: unknown instrument_role")
        lifecycle_phase = entry.get("lifecycle_phase")
        if lifecycle_phase is not None:
            if (
                timestamp_kind != TIMESTAMP_TRANSITION
                or lifecycle_phase
                not in {
                    LIFECYCLE_TRANSITION_SCHEDULED,
                    LIFECYCLE_TRANSITIONED_STANDARD,
                    LIFECYCLE_CANCELLED,
                    LIFECYCLE_DELISTING,
                    LIFECYCLE_DELISTED,
                }
            ):
                problems.append(f"line {number}: lifecycle_phase is invalid")
        identity: AssetIdentity | None = None
        try:
            identity = AssetIdentity(
                asset_class=entry.get("asset_class"),
                issuer_namespace=entry.get("issuer_namespace"),
                issuer_id=entry.get("issuer_id"),
                evidence_class=entry.get("identity_evidence_class"),
            )
        except (EventRegistryError, TypeError, ValueError) as exc:
            problems.append(f"line {number}: asset identity is invalid: {exc}")
        if identity is not None:
            expected_identity_hash = identity.as_record_fields()[
                "asset_identity_hash"
            ]
            if entry.get("asset_identity_hash") != expected_identity_hash:
                problems.append(
                    f"line {number}: asset identity hash does not match its fields"
                )
            if (
                identity.evidence_class == IDENTITY_EVIDENCE_LEGACY_UNCLASSIFIED
                and identity.asset_class != ASSET_CLASS_UNCLASSIFIED
            ):
                problems.append(
                    f"line {number}: legacy asset identity evidence requires UNCLASSIFIED"
                )
            if (
                entry.get("source_class") == SOURCE_VENUE_INSTRUMENT_METADATA
                and identity.evidence_class
                not in {
                    IDENTITY_EVIDENCE_VENUE_EXPLICIT_METADATA,
                    IDENTITY_EVIDENCE_LEGACY_UNCLASSIFIED,
                }
            ):
                problems.append(
                    f"line {number}: asset identity evidence does not match venue metadata"
                )
            if (
                entry.get("source_class") == SOURCE_OFFICIAL_ANNOUNCEMENT
                and identity.evidence_class
                not in {
                    IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
                    IDENTITY_EVIDENCE_LEGACY_UNCLASSIFIED,
                }
            ):
                problems.append(
                    f"line {number}: asset identity evidence does not match official attestation"
                )
        if (
            entry.get("source_class") == SOURCE_VENUE_INSTRUMENT_METADATA
            and timestamp_kind
            in {
                TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
                TIMESTAMP_CONTRACT_CREATED,
                TIMESTAMP_TRANSITION,
            }
            and episode_id
        ):
            metadata_episode_ids.add(episode_id)
            asset_class = str(entry.get("asset_class") or ASSET_CLASS_UNCLASSIFIED)
            if asset_class != ASSET_CLASS_UNCLASSIFIED:
                metadata_asset_identities.setdefault(episode_id, set()).add(
                    (
                        asset_class,
                        str(entry.get("issuer_namespace") or ""),
                        str(entry.get("issuer_id") or ""),
                    )
                )
        if (
            timestamp_kind == TIMESTAMP_OFFICIAL_SPOT_T0
            or entry.get("source_class") == SOURCE_OFFICIAL_ANNOUNCEMENT
        ):
            if not (
                timestamp_kind == TIMESTAMP_OFFICIAL_SPOT_T0
                and entry.get("source_class") == SOURCE_OFFICIAL_ANNOUNCEMENT
                and entry.get("instrument_role") == "spot"
            ):
                problems.append(f"line {number}: malformed official timestamp observation")
            if (entry.get("capture_eligible") is True) != (
                entry.get("evidence_use") == "ACCEPTANCE_ANCHOR"
            ):
                problems.append(
                    f"line {number}: official capture eligibility and evidence use disagree"
                )
            problems.extend(
                _official_record_problems(
                    entry,
                    number=number,
                    metadata_episode_ids=metadata_episode_ids,
                    metadata_asset_identities=metadata_asset_identities,
                )
            )

        stream_id = str(entry.get("stream_id") or "")
        try:
            expected_stream_id = _stream_id(
                episode_id=episode_id,
                timestamp_kind=timestamp_kind,
                instrument_role=str(entry.get("instrument_role") or ""),
                source_class=str(entry.get("source_class") or ""),
                source_identity=str(entry.get("source_identity") or ""),
            )
        except Exception:  # pragma: no cover - values are diagnosed individually
            expected_stream_id = ""
        if stream_id != expected_stream_id:
            problems.append(f"line {number}: stream_id does not match immutable fields")
        previous_stream = stream_heads.get(stream_id)
        expected_stream_revision = (
            int(previous_stream.get("stream_revision", -1)) + 1
            if previous_stream is not None
            else 0
        )
        if int(entry.get("stream_revision", -1)) != expected_stream_revision:
            problems.append(
                f"line {number}: stream_revision {entry.get('stream_revision')} "
                f"does not follow {expected_stream_revision - 1}"
            )
        expected_supersedes = (
            previous_stream.get("record_hash") if previous_stream is not None else None
        )
        if entry.get("supersedes_record_hash") != expected_supersedes:
            problems.append(
                f"line {number}: supersedes_record_hash does not match stream head"
            )
        if stream_id:
            stream_heads[stream_id] = entry
        previous_global_hash = claimed_hash
    return problems


def _verify_registry_snapshot(
    path: Path | None = None,
    *,
    verify_summary: bool = True,
    bootstrap_lock_owner: RegistryLockOwner | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load one registry snapshot and verify its lineage and production receipt."""
    path = path or REGISTRY_PATH
    production_registry = (
        path.resolve(strict=False) == REGISTRY_PATH.resolve(strict=False)
    )
    lock_path = REGISTRY_LOCK_PATH if production_registry else path.with_suffix(".lock")
    problems: list[str] = []
    if bootstrap_lock_owner is None:
        if lock_path.is_file():
            problems.append("registry mutation lock is active; stable snapshot unavailable")
    else:
        try:
            if (
                bootstrap_lock_owner.path.resolve(strict=False)
                != lock_path.resolve(strict=False)
            ):
                raise EventRegistryError("registry lock does not cover this snapshot")
            _assert_registry_lock_owner(bootstrap_lock_owner)
        except EventRegistryError as exc:
            problems.append(f"registry snapshot lock ownership is invalid: {exc}")

    registry_bytes = path.read_bytes() if path.is_file() else b""
    summary_path = path.with_suffix(".summary.json")
    summary_bytes = summary_path.read_bytes() if summary_path.is_file() else b""
    entries = _load_registry_bytes(registry_bytes, path=path)
    problems.extend(_verify_records(entries))
    summary_required = bool(entries) and production_registry
    summary_verified = False
    summary_pending_under_lock = False
    if summary_required and not verify_summary:
        if bootstrap_lock_owner is None:
            problems.append(
                "production registry summary verification cannot be disabled without "
                "the active registry lock"
            )
        else:
            expected_lock_path = REGISTRY_LOCK_PATH
            try:
                if (
                    bootstrap_lock_owner.path.resolve(strict=False)
                    != expected_lock_path.resolve(strict=False)
                ):
                    raise EventRegistryError(
                        "bootstrap lock does not cover the production registry"
                    )
                if bootstrap_lock_owner.plan_hash != trust_root.PLAN_HASH:
                    raise EventRegistryError(
                        "bootstrap lock is not bound to the active PlanOnly"
                    )
                _assert_registry_lock_owner(bootstrap_lock_owner)
                summary_pending_under_lock = True
            except EventRegistryError as exc:
                problems.append(f"production registry summary bootstrap is invalid: {exc}")
    if verify_summary:
        if summary_bytes:
            try:
                summary = json.loads(summary_bytes.decode("utf-8"))
                if not isinstance(summary, Mapping):
                    raise ValueError("summary root is not an object")
                summary_problem_count = len(problems)
                receipt = summary.get("registry")
                if not isinstance(receipt, Mapping):
                    raise ValueError("summary has no registry receipt")
                if production_registry:
                    if summary.get("schema") != REGISTRY_SCHEMA:
                        problems.append("summary schema does not match registry")
                    if summary.get("status") not in {
                        "REFRESH_COMPLETE",
                        "REGISTRY_MUTATION_COMPLETE",
                    }:
                        problems.append("summary status is not a complete registry mutation")
                    if summary.get("complete") is not True:
                        problems.append("summary complete flag is not true")
                    if summary.get("plan_hash") != trust_root.PLAN_HASH:
                        problems.append("summary plan_hash is not the active PlanOnly")
                    mutation_type = str(summary.get("mutation_type") or "metadata_refresh")
                    if mutation_type not in {"metadata_refresh", "official_attestation"}:
                        problems.append("summary mutation_type is invalid")
                    run_identity = (
                        summary.get("mutation_run_id")
                        if mutation_type == "official_attestation"
                        else summary.get("refresh_run_id") or summary.get("mutation_run_id")
                    )
                    if not str(run_identity or "").strip():
                        problems.append("summary mutation run id is missing")
                    resolved_paths_hash = str(summary.get("resolved_paths_hash") or "")
                    if len(resolved_paths_hash) != 64 or any(
                        character not in "0123456789abcdef"
                        for character in resolved_paths_hash
                    ):
                        problems.append("summary resolved_paths_hash is invalid")
                    active_state = summary.get(ACTIVE_CONTRACTS_FIELD)
                    if not isinstance(active_state, Mapping):
                        problems.append("summary active contract state is missing or invalid")
                    else:
                        for adapter in ADAPTERS:
                            values = active_state.get(adapter.venue)
                            if not isinstance(values, list) or not all(
                                isinstance(value, str)
                                and bool(value.strip())
                                and value == value.strip()
                                for value in values
                            ):
                                problems.append(
                                    f"summary active contract state is invalid for {adapter.venue}"
                                )
                    if receipt.get("status") != "REGISTRY_OK":
                        problems.append("summary registry status is not REGISTRY_OK")
                if int(receipt.get("entries", -1)) != len(entries):
                    problems.append("summary entry count does not match registry")
                actual_head = entries[-1].get("record_hash") if entries else None
                if receipt.get("head_record_hash") != actual_head:
                    problems.append("summary head_record_hash does not match registry")
                anchor_present = any(
                    field in summary for field in MUTATION_SUMMARY_LINK_FIELDS
                ) or _mutation_receipt_dir(path).exists()
                if (production_registry and summary_required) or anchor_present:
                    problems.extend(
                        _mutation_receipt_anchor_problems(
                            path,
                            summary=summary,
                            registry_bytes=registry_bytes,
                            entries=entries,
                        )
                    )
                summary_verified = len(problems) == summary_problem_count
            except (OSError, ValueError, TypeError) as exc:
                problems.append(f"summary receipt is unreadable: {type(exc).__name__}: {exc}")
        elif summary_required:
            problems.append("required summary receipt is missing for nonempty production registry")
    registry_bytes_after = path.read_bytes() if path.is_file() else b""
    summary_bytes_after = summary_path.read_bytes() if summary_path.is_file() else b""
    if registry_bytes_after != registry_bytes or summary_bytes_after != summary_bytes:
        problems.append("registry or summary changed during snapshot verification")
    if bootstrap_lock_owner is not None:
        try:
            _assert_registry_lock_owner(bootstrap_lock_owner)
        except EventRegistryError as exc:
            problems.append(f"registry snapshot lock changed during verification: {exc}")
    if bootstrap_lock_owner is None and lock_path.is_file():
        if "registry mutation lock is active; stable snapshot unavailable" not in problems:
            problems.append("registry mutation lock appeared during snapshot verification")
    episodes = materialize_episodes(entries)
    heads = [head[2] for head in _stream_heads(entries).values()]
    report = {
        "status": "REGISTRY_OK" if not problems else "REGISTRY_PROBLEMS",
        "entries": len(entries),
        "events": len(episodes),
        "episodes": len(episodes),
        "head_record_hash": entries[-1].get("record_hash") if entries else None,
        "registry_sha256": (
            hashlib.sha256(registry_bytes).hexdigest() if registry_bytes else None
        ),
        "registry_summary_sha256": (
            hashlib.sha256(summary_bytes).hexdigest()
            if verify_summary and summary_bytes
            else None
        ),
        "problems": problems,
        "summary_required": summary_required,
        "summary_verified": summary_verified,
        "recovery_action": (
            "RESTORE_MATCHING_SUMMARY_OR_QUARANTINE_AND_BOOTSTRAP_NEW_GENERATION"
            if summary_required and not summary_verified and not summary_pending_under_lock
            else None
        ),
        "bootstrap_state": (
            "EMPTY_REGISTRY_BOOTSTRAP"
            if production_registry and not entries
            else "SUMMARY_PENDING_UNDER_LOCK"
            if summary_pending_under_lock
            else "ESTABLISHED"
            if production_registry and summary_verified
            else "RECOVERY_REQUIRED"
            if production_registry
            else "NON_PRODUCTION_VALIDATION"
        ),
        "by_source_class": _count(heads, "source_class"),
        "by_venue": _count(episodes, "venue"),
    }
    return entries, report


def verify_registry(
    path: Path | None = None,
    *,
    verify_summary: bool = True,
    bootstrap_lock_owner: RegistryLockOwner | None = None,
) -> dict[str, Any]:
    """Fail closed on tampering, reorder, fork, or orphan source revisions."""
    _entries, report = _verify_registry_snapshot(
        path,
        verify_summary=verify_summary,
        bootstrap_lock_owner=bootstrap_lock_owner,
    )
    return report


def _count(entries: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        value = str(entry.get(key))
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def events_for_capture(
    registry_path: Path | None = None,
    *,
    now_ts: int,
    source_class: str,
    asset_class: str = ASSET_CLASS_CRYPTO_TOKEN,
    horizon_sec: int = 24 * 3600,
) -> list[dict[str, Any]]:
    """Events whose t0 is still ahead, within one source class only.

    The source class is a required argument rather than a filter applied afterwards:
    mixing an announcement-derived t0 with a metadata-derived one in the same capture
    set would reintroduce exactly the defect this registry exists to avoid."""
    if registry_path is not None and not isinstance(registry_path, (str, os.PathLike)):
        raise EventRegistryError(
            "VERIFIED_PRODUCTION_REGISTRY_REQUIRED: direct event sequences are not "
            "capture eligible"
        )
    path = Path(registry_path) if registry_path is not None else REGISTRY_PATH
    if path.resolve(strict=False) != REGISTRY_PATH.resolve(strict=False):
        raise EventRegistryError(
            "VERIFIED_PRODUCTION_REGISTRY_REQUIRED: capture selection only reads "
            "the production registry"
        )
    entries, report = _verify_registry_snapshot(path)
    if report["status"] != "REGISTRY_OK" or (
        report["summary_required"] and not report["summary_verified"]
    ):
        raise EventRegistryError(
            "VERIFIED_PRODUCTION_REGISTRY_REQUIRED: "
            + "; ".join(report["problems"] or ["registry summary is not verified"])
        )
    if source_class not in SOURCE_CLASSES:
        raise EventRegistryError(f"unknown source class: {source_class}")
    if source_class != SOURCE_OFFICIAL_ANNOUNCEMENT:
        raise EventRegistryError(
            "capture selection requires OFFICIAL_ANNOUNCEMENT; metadata proxies are descriptive-only"
        )
    if asset_class != ASSET_CLASS_CRYPTO_TOKEN:
        raise EventRegistryError(
            "this capture track requires the explicit CRYPTO_TOKEN asset class"
        )
    try:
        summary = json.loads(
            path.with_suffix(".summary.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise EventRegistryError(
            f"STALE_METADATA_REFRESH: summary freshness anchor is unreadable: {exc}"
        ) from exc
    metadata_refresh_received = _parse_explicit_utc(
        summary.get(LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD)
        if isinstance(summary, Mapping)
        else None
    )
    if metadata_refresh_received is None:
        raise EventRegistryError(
            "STALE_METADATA_REFRESH: complete metadata refresh anchor is missing"
        )
    metadata_age_sec = now_ts - int(metadata_refresh_received.timestamp())
    if not 0 <= metadata_age_sec <= MAX_COMPLETE_METADATA_REFRESH_AGE_SEC:
        raise EventRegistryError(
            "STALE_METADATA_REFRESH: latest complete metadata refresh is "
            f"{metadata_age_sec}s old; maximum is "
            f"{MAX_COMPLETE_METADATA_REFRESH_AGE_SEC}s"
        )
    active_generations, _high_water = _load_lifecycle_generation_state(
        path.with_suffix(".summary.json"),
        existing=entries,
    )
    mutation_receipts, mutation_receipt_problems = _load_mutation_receipt_chain(path)
    if mutation_receipt_problems or not mutation_receipts:
        raise EventRegistryError(
            "VERIFIED_PRODUCTION_REGISTRY_REQUIRED: latest mutation receipt is unavailable"
        )
    latest_mutation_receipt = mutation_receipts[-1]
    raw_surface_counts = latest_mutation_receipt.get(
        RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD
    )
    relevant_identity_hashes = latest_mutation_receipt.get(
        RELEVANT_IDENTITY_HASHES_BY_SURFACE_FIELD
    )
    explicit_terminal_ids = latest_mutation_receipt.get(
        EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD
    )
    expected_surfaces = {surface.surface_id for surface in SURFACES}
    if (
        not isinstance(raw_surface_counts, Mapping)
        or set(raw_surface_counts) != expected_surfaces
        or not isinstance(relevant_identity_hashes, Mapping)
        or set(relevant_identity_hashes) != expected_surfaces
        or not isinstance(explicit_terminal_ids, Mapping)
        or set(explicit_terminal_ids) != expected_surfaces
        or not all(
            _is_sha256_text(value) for value in relevant_identity_hashes.values()
        )
    ):
        raise EventRegistryError(
            "RELEVANT_IDENTITY_AUTHORITY_MISSING: latest mutation receipt lacks "
            "surface identity anchors"
        )
    authority_state_hash = registry_authority_state_hash(
        active_generations=latest_mutation_receipt.get(
            ACTIVE_LIFECYCLE_GENERATIONS_FIELD
        ),
        lifecycle_high_water=latest_mutation_receipt.get(
            LIFECYCLE_GENERATION_HIGH_WATER_FIELD
        ),
        metadata_refresh_received_at=str(
            latest_mutation_receipt.get(
                LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD
            )
            or ""
        ),
        raw_universe_rows_by_surface=raw_surface_counts,
        relevant_identity_hashes_by_surface=relevant_identity_hashes,
        explicit_terminal_ids_by_surface=explicit_terminal_ids,
    )
    episodes = materialize_episodes(entries)
    def is_due_t0(value: Any) -> bool:
        t0 = int(value or 0)
        target = t0 - config.CAPTURE_WINDOW_BEFORE_SEC
        return (
            target - config.CAPTURE_LAUNCH_EARLY_GRACE_SEC
            <= now_ts
            <= target + config.CAPTURE_LAUNCH_LATE_GRACE_SEC
            and t0 <= now_ts + horizon_sec
        )

    conflicts = [
        str(episode.get("episode_id"))
        for episode in episodes
        if episode.get("official_conflict") is True
        and any(
            is_due_t0(observation.get("timestamp_ts"))
            for observation in episode.get("timestamp_observations") or []
            if observation.get("timestamp_kind") == TIMESTAMP_OFFICIAL_SPOT_T0
        )
    ]
    if conflicts:
        raise EventRegistryError(
            "OFFICIAL_CONFLICT: conflicting official_spot_t0 heads for "
            + ", ".join(sorted(conflicts))
        )
    asset_conflicts = [
        str(episode.get("episode_id"))
        for episode in episodes
        if episode.get("asset_identity_conflict") is True
        and is_due_t0(episode.get("official_spot_t0"))
    ]
    if asset_conflicts:
        raise EventRegistryError(
            "ASSET_IDENTITY_CONFLICT: conflicting identity heads for "
            + ", ".join(sorted(asset_conflicts))
        )
    upcoming: list[dict[str, Any]] = []
    for entry in episodes:
        provenance = entry.get("official_t0_provenance")
        if not isinstance(provenance, Mapping):
            continue
        venue = str(entry.get("venue") or "")
        contract = str(entry.get("premarket_contract_id") or "")
        active_generation = active_generations.get(venue, {}).get(contract)
        received = _parse_explicit_utc(provenance.get("received_at_utc"))
        if (
            entry.get("t0_source_class") == source_class
            and entry.get("asset_class") == asset_class
            and entry.get("asset_identity_conflict") is False
            and entry.get("capture_eligible") is True
            and entry.get("evidence_use") == "ACCEPTANCE_ANCHOR"
            and active_generation is not None
            and int(entry.get("lifecycle_generation", -1)) == active_generation
            and is_due_t0(entry.get("official_spot_t0"))
            and received is not None
            and int(received.timestamp()) <= now_ts
        ):
            upcoming.append(entry)
    upcoming.sort(
        key=lambda item: (item["official_spot_t0"], item["venue"], item["symbol"])
    )
    for event in upcoming:
        provenance = dict(event.get("official_t0_provenance") or {})
        event.update({
            "official_record_hash": provenance.get("record_hash"),
            "official_source_url": provenance.get("source_url"),
            "official_source_identity": provenance.get("source_identity"),
            "registry_sha256": report.get("registry_sha256"),
            "registry_tail_record_hash": report.get("head_record_hash"),
            "mutation_receipt_seq": latest_mutation_receipt.get("mutation_seq"),
            "mutation_receipt_hash": latest_mutation_receipt.get("receipt_hash"),
            "summary_content_sha256": latest_mutation_receipt.get(
                "summary_content_hash"
            ),
            "registry_authority_state_hash": authority_state_hash,
            "plan_id": trust_root.PLAN_ID,
            "plan_hash": trust_root.PLAN_HASH,
        })
    return upcoming


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _mutation_receipt_dir(path: Path) -> Path:
    return path.with_name(path.name + MUTATION_RECEIPT_DIR_SUFFIX)


def _summary_content_hash(summary: Mapping[str, Any]) -> str:
    return canonical_hash(
        {
            key: value
            for key, value in summary.items()
            if key not in MUTATION_SUMMARY_LINK_FIELDS
        }
    )


def _load_mutation_receipt_chain(
    path: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    directory = _mutation_receipt_dir(path)
    if not directory.exists():
        return [], []
    if not directory.is_dir():
        return [], ["mutation receipt anchor path is not a directory"]
    receipts: list[dict[str, Any]] = []
    problems: list[str] = []
    files = sorted(directory.glob("*.json"), key=lambda item: item.name)
    unexpected = sorted(
        item.name for item in directory.iterdir() if not item.is_file() or item.suffix != ".json"
    )
    if unexpected:
        problems.append("mutation receipt anchor contains unexpected entries")
    previous_hash: str | None = None
    for expected_seq, receipt_path in enumerate(files):
        try:
            raw = receipt_path.read_bytes()
            receipt = json.loads(raw.decode("utf-8"))
            if not isinstance(receipt, Mapping):
                raise ValueError("receipt root is not an object")
            receipt = dict(receipt)
        except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
            problems.append(
                f"mutation receipt {receipt_path.name} is unreadable: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        claimed_hash = str(receipt.get("receipt_hash") or "")
        computed_hash = canonical_hash(
            {key: value for key, value in receipt.items() if key != "receipt_hash"}
        )
        expected_name = f"{expected_seq:020d}-{claimed_hash}.json"
        if receipt_path.name != expected_name:
            problems.append(
                f"mutation receipt {receipt_path.name} does not match sequence/hash identity"
            )
        if receipt.get("schema") != MUTATION_RECEIPT_SCHEMA:
            problems.append(f"mutation receipt {receipt_path.name} has the wrong schema")
        if receipt.get("mutation_seq") != expected_seq:
            problems.append(f"mutation receipt {receipt_path.name} breaks sequence order")
        if receipt.get("previous_mutation_receipt_hash") != previous_hash:
            problems.append(f"mutation receipt {receipt_path.name} breaks the hash chain")
        if claimed_hash != computed_hash:
            problems.append(f"mutation receipt {receipt_path.name} hash is invalid")
        if not _is_sha256_text(receipt.get("registry_sha256")):
            problems.append(f"mutation receipt {receipt_path.name} registry hash is invalid")
        if not _is_sha256_text(receipt.get("summary_content_hash")):
            problems.append(f"mutation receipt {receipt_path.name} summary hash is invalid")
        receipts.append(receipt)
        previous_hash = claimed_hash
    return receipts, problems


def _registry_prefix_bytes_through_record(
    raw: bytes,
    entries: Sequence[Mapping[str, Any]],
    record_hash: str,
) -> tuple[bytes, int]:
    """Return the exact historical JSONL prefix ending at one record hash."""
    positions = [
        index
        for index, entry in enumerate(entries)
        if entry.get("record_hash") == record_hash
    ]
    if len(positions) != 1:
        raise EventRegistryError(
            "registry_tail_record_hash does not identify exactly one registry record"
        )
    target_position = positions[0]
    seen_records = 0
    end_offset = 0
    for line in raw.splitlines(keepends=True):
        end_offset += len(line)
        if not line.strip():
            continue
        if seen_records == target_position:
            return raw[:end_offset], target_position
        seen_records += 1
    raise EventRegistryError("registry historical prefix cannot be reconstructed")


def verify_capture_lineage(
    evidence: Mapping[str, Any],
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    """Verify a capture against its exact historical registry prefix and receipt.

    The mutable current summary is not treated as historical evidence.  Its immutable
    mutation receipt and the exact JSONL prefix that receipt sealed are the authority.
    Later valid appends therefore do not invalidate an older capture.
    """
    if not isinstance(evidence, Mapping):
        raise EventRegistryError("capture lineage evidence must be an object")
    lineage = dict(evidence)
    required_sha_fields = (
        "official_record_hash",
        "registry_sha256",
        "registry_tail_record_hash",
        "mutation_receipt_hash",
        "summary_content_sha256",
        "registry_authority_state_hash",
        "plan_hash",
        "asset_identity_hash",
    )
    for field in required_sha_fields:
        if not _is_sha256_text(lineage.get(field)):
            raise EventRegistryError(f"capture lineage {field} is missing or invalid")
    required_text_fields = (
        "episode_id",
        "venue",
        "premarket_contract_id",
        "spot_symbol",
        "t0_source_class",
        "official_source_url",
        "official_source_identity",
        "plan_id",
        "asset_class",
        "issuer_namespace",
        "issuer_id",
    )
    for field in required_text_fields:
        value = lineage.get(field)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise EventRegistryError(f"capture lineage {field} is missing or invalid")
    mutation_seq = lineage.get("mutation_receipt_seq")
    if isinstance(mutation_seq, bool) or not isinstance(mutation_seq, int) or mutation_seq < 0:
        raise EventRegistryError(
            "capture lineage mutation_receipt_seq is missing or invalid"
        )
    t0 = lineage.get("official_spot_t0")
    if isinstance(t0, bool) or not isinstance(t0, int) or t0 <= 0:
        raise EventRegistryError("capture lineage official_spot_t0 is missing or invalid")
    precision = lineage.get("t0_precision_sec")
    if (
        isinstance(precision, bool)
        or not isinstance(precision, int)
        or precision <= 0
    ):
        raise EventRegistryError("capture lineage t0_precision_sec is missing or invalid")
    if lineage["asset_class"] != ASSET_CLASS_CRYPTO_TOKEN:
        raise EventRegistryError(
            "capture lineage is not the explicit crypto asset class"
        )
    if lineage["t0_source_class"] != SOURCE_OFFICIAL_ANNOUNCEMENT:
        raise EventRegistryError(
            "capture lineage t0 is not an official announcement timestamp"
        )

    registry_path = path or REGISTRY_PATH
    entries, current_report = _verify_registry_snapshot(registry_path)
    if current_report["status"] != "REGISTRY_OK":
        raise EventRegistryError(
            "current registry lineage is invalid: "
            + "; ".join(current_report["problems"])
        )
    raw_before = registry_path.read_bytes() if registry_path.is_file() else b""
    prefix_bytes, tail_position = _registry_prefix_bytes_through_record(
        raw_before,
        entries,
        str(lineage["registry_tail_record_hash"]),
    )
    prefix_entries = list(entries[: tail_position + 1])
    prefix_problems = _verify_records(prefix_entries)
    if prefix_problems:
        raise EventRegistryError(
            "historical registry prefix is invalid: " + "; ".join(prefix_problems)
        )
    if hashlib.sha256(prefix_bytes).hexdigest() != lineage["registry_sha256"]:
        raise EventRegistryError(
            "capture lineage registry_sha256 does not match the historical prefix"
        )

    receipts, receipt_problems = _load_mutation_receipt_chain(registry_path)
    if receipt_problems:
        raise EventRegistryError(
            "registry mutation receipt chain is invalid: " + "; ".join(receipt_problems)
        )
    if mutation_seq >= len(receipts):
        raise EventRegistryError("capture mutation receipt is missing")
    receipt = receipts[mutation_seq]
    if receipt.get("receipt_hash") != lineage["mutation_receipt_hash"]:
        raise EventRegistryError("capture mutation receipt hash does not match")
    receipt_expectations = {
        "registry_sha256": lineage["registry_sha256"],
        "registry_head_record_hash": lineage["registry_tail_record_hash"],
        "registry_entries": len(prefix_entries),
        "summary_content_hash": lineage["summary_content_sha256"],
        "plan_id": lineage["plan_id"],
        "plan_hash": lineage["plan_hash"],
    }
    for field, expected in receipt_expectations.items():
        if receipt.get(field) != expected:
            raise EventRegistryError(
                f"capture mutation receipt {field} does not match lineage"
            )
    authority_hash = registry_authority_state_hash(
        active_generations=receipt.get(ACTIVE_LIFECYCLE_GENERATIONS_FIELD),
        lifecycle_high_water=receipt.get(LIFECYCLE_GENERATION_HIGH_WATER_FIELD),
        metadata_refresh_received_at=str(
            receipt.get(LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD) or ""
        ),
        raw_universe_rows_by_surface=receipt.get(
            RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD
        ),
        relevant_identity_hashes_by_surface=receipt.get(
            RELEVANT_IDENTITY_HASHES_BY_SURFACE_FIELD
        ),
        explicit_terminal_ids_by_surface=receipt.get(
            EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD
        ),
    )
    if authority_hash != lineage["registry_authority_state_hash"]:
        raise EventRegistryError(
            "capture registry authority state hash does not match mutation receipt"
        )

    official_matches = [
        entry
        for entry in prefix_entries
        if entry.get("record_hash") == lineage["official_record_hash"]
    ]
    if len(official_matches) != 1:
        raise EventRegistryError(
            "official_record_hash does not identify one record in the historical prefix"
        )
    official = official_matches[0]
    official_expectations = {
        "episode_id": lineage["episode_id"],
        "venue": lineage["venue"],
        "premarket_contract_id": lineage["premarket_contract_id"],
        "spot_symbol": lineage["spot_symbol"],
        "timestamp_ts": t0,
        "t0_precision_sec": precision,
        "timestamp_kind": TIMESTAMP_OFFICIAL_SPOT_T0,
        "source_class": SOURCE_OFFICIAL_ANNOUNCEMENT,
        "source_url": lineage["official_source_url"],
        "source_identity": lineage["official_source_identity"],
        "asset_class": ASSET_CLASS_CRYPTO_TOKEN,
        "issuer_namespace": lineage["issuer_namespace"],
        "issuer_id": lineage["issuer_id"],
        "asset_identity_hash": lineage["asset_identity_hash"],
        "capture_eligible": True,
        "evidence_use": "ACCEPTANCE_ANCHOR",
    }
    for field, expected in official_expectations.items():
        if official.get(field) != expected:
            raise EventRegistryError(
                f"official capture asset/timestamp lineage {field} does not match"
            )
    official_stream = str(official.get("stream_id") or "")
    stream_head = _stream_heads(prefix_entries).get(official_stream)
    if stream_head is None or stream_head[2].get("record_hash") != official["record_hash"]:
        raise EventRegistryError(
            "official capture record was already superseded at the captured registry tail"
        )

    raw_after = registry_path.read_bytes() if registry_path.is_file() else b""
    lock_path = (
        REGISTRY_LOCK_PATH
        if registry_path.resolve(strict=False) == REGISTRY_PATH.resolve(strict=False)
        else registry_path.with_suffix(".lock")
    )
    if raw_after != raw_before or lock_path.is_file():
        raise EventRegistryError(
            "registry changed or became locked during capture lineage verification"
        )
    return {
        "schema": "premarket_perp_capture_lineage_verification_v1",
        "ok": True,
        "status": "CAPTURE_LINEAGE_OK",
        "registry_entries_at_capture": len(prefix_entries),
        "current_registry_entries": len(entries),
        "registry_sha256": lineage["registry_sha256"],
        "registry_tail_record_hash": lineage["registry_tail_record_hash"],
        "official_record_hash": lineage["official_record_hash"],
        "mutation_receipt_seq": mutation_seq,
        "mutation_receipt_hash": lineage["mutation_receipt_hash"],
        "asset_class": ASSET_CLASS_CRYPTO_TOKEN,
    }


def _mutation_compare_and_swap_token(path: Path) -> tuple[int, str | None]:
    """Identify the last fully committed mutation before a refresh starts staging."""
    receipts, problems = _load_mutation_receipt_chain(path)
    if problems:
        raise EventRegistryError(
            "refresh base mutation receipt chain is invalid: " + "; ".join(problems)
        )
    return len(receipts), receipts[-1]["receipt_hash"] if receipts else None


def _mutation_receipt_anchor_problems(
    path: Path,
    *,
    summary: Mapping[str, Any],
    registry_bytes: bytes,
    entries: Sequence[Mapping[str, Any]],
) -> list[str]:
    receipts, problems = _load_mutation_receipt_chain(path)
    if not receipts:
        return [*problems, "required immutable mutation receipt anchor is missing"]
    latest = receipts[-1]
    actual_registry_hash = hashlib.sha256(registry_bytes).hexdigest()
    actual_head = entries[-1].get("record_hash") if entries else None
    run_identity = (
        summary.get("mutation_run_id")
        if summary.get("mutation_type") == "official_attestation"
        else summary.get("refresh_run_id") or summary.get("mutation_run_id")
    )
    expected = {
        "registry_path_name": path.name,
        "registry_sha256": actual_registry_hash,
        "registry_entries": len(entries),
        "registry_head_record_hash": actual_head,
        "summary_content_hash": _summary_content_hash(summary),
        "mutation_type": summary.get("mutation_type"),
        "mutation_run_id": run_identity,
        "plan_id": summary.get("plan_id"),
        "plan_hash": summary.get("plan_hash"),
        ACTIVE_CONTRACTS_FIELD: summary.get(ACTIVE_CONTRACTS_FIELD),
        ACTIVE_LIFECYCLE_GENERATIONS_FIELD: summary.get(
            ACTIVE_LIFECYCLE_GENERATIONS_FIELD
        ),
        LIFECYCLE_GENERATION_HIGH_WATER_FIELD: summary.get(
            LIFECYCLE_GENERATION_HIGH_WATER_FIELD
        ),
        LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD: summary.get(
            LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD
        ),
        RAW_UNIVERSE_ROWS_FIELD: summary.get(RAW_UNIVERSE_ROWS_FIELD),
        RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD: summary.get(
            RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD
        ),
        RELEVANT_IDENTITY_IDS_BY_SURFACE_FIELD: summary.get(
            RELEVANT_IDENTITY_IDS_BY_SURFACE_FIELD
        ),
        RELEVANT_IDENTITY_HASHES_BY_SURFACE_FIELD: summary.get(
            RELEVANT_IDENTITY_HASHES_BY_SURFACE_FIELD
        ),
        EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD: summary.get(
            EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD
        ),
    }
    for field, value in expected.items():
        if latest.get(field) != value:
            problems.append(f"immutable mutation receipt does not match current {field}")
    summary_links = {
        "mutation_receipt_schema": MUTATION_RECEIPT_SCHEMA,
        "mutation_seq": latest.get("mutation_seq"),
        "previous_mutation_receipt_hash": latest.get(
            "previous_mutation_receipt_hash"
        ),
        "mutation_receipt_hash": latest.get("receipt_hash"),
    }
    for field, value in summary_links.items():
        if summary.get(field) != value:
            problems.append(f"summary {field} does not match immutable mutation receipt")
    return problems


def _summary_with_exact_lifecycle_state(
    summary: Mapping[str, Any],
    *,
    registry_entries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a summary with exact active-generation and high-water fields."""
    payload = dict(summary)
    if payload.get("mutation_type") == "metadata_refresh":
        metadata_refresh_received_at = payload.get(
            LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD
        ) or payload.get("refreshed_at_utc")
        if _parse_explicit_utc(metadata_refresh_received_at) is None:
            raise EventRegistryError(
                "complete metadata refresh requires an explicit UTC freshness anchor"
            )
        payload[LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD] = (
            metadata_refresh_received_at
        )
    elif _parse_explicit_utc(
        payload.get(LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD)
    ) is None:
        raise EventRegistryError(
            "non-metadata mutation must preserve the complete metadata refresh anchor"
        )
    if RAW_UNIVERSE_ROWS_FIELD in payload:
        payload[RAW_UNIVERSE_ROWS_FIELD] = _parse_raw_universe_counts(
            payload[RAW_UNIVERSE_ROWS_FIELD]
        )
    elif payload.get("mutation_type") == "metadata_refresh":
        # Compatibility-only bootstrap for historical test receipts. Runtime refresh
        # always supplies full raw API row counts before entering this function.
        counts = {adapter.venue: 0 for adapter in ADAPTERS}
        for entry in registry_entries:
            venue = str(entry.get("venue") or "")
            if venue in counts:
                counts[venue] += 1
        payload[RAW_UNIVERSE_ROWS_FIELD] = dict(sorted(counts.items()))
    else:
        raise EventRegistryError(
            "non-metadata mutation must preserve raw universe row counts"
        )
    surface_ids = {surface.surface_id for surface in SURFACES}
    if RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD not in payload:
        if payload.get("mutation_type") != "metadata_refresh":
            raise EventRegistryError(
                "non-metadata mutation must preserve raw universe rows by surface"
            )
        payload[RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD] = {
            surface_id: 0 for surface_id in sorted(surface_ids)
        }
    raw_surface_counts = payload[RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD]
    if not isinstance(raw_surface_counts, Mapping) or set(raw_surface_counts) != surface_ids:
        raise EventRegistryError("raw universe rows by surface are invalid")
    payload[RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD] = {
        surface_id: int(raw_surface_counts[surface_id])
        for surface_id in sorted(surface_ids)
        if not isinstance(raw_surface_counts[surface_id], bool)
        and isinstance(raw_surface_counts[surface_id], int)
        and raw_surface_counts[surface_id] >= 0
    }
    if set(payload[RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD]) != surface_ids:
        raise EventRegistryError("raw universe rows by surface are invalid")
    if RELEVANT_IDENTITY_IDS_BY_SURFACE_FIELD not in payload:
        if payload.get("mutation_type") != "metadata_refresh":
            raise EventRegistryError(
                "non-metadata mutation must preserve relevant identity ids"
            )
        payload[RELEVANT_IDENTITY_IDS_BY_SURFACE_FIELD] = {
            surface_id: [] for surface_id in sorted(surface_ids)
        }
    payload[RELEVANT_IDENTITY_IDS_BY_SURFACE_FIELD] = _parse_surface_string_lists(
        payload[RELEVANT_IDENTITY_IDS_BY_SURFACE_FIELD],
        field_name=RELEVANT_IDENTITY_IDS_BY_SURFACE_FIELD,
    )
    if RELEVANT_IDENTITY_HASHES_BY_SURFACE_FIELD not in payload:
        if payload.get("mutation_type") != "metadata_refresh":
            raise EventRegistryError(
                "non-metadata mutation must preserve relevant identity hashes"
            )
        payload[RELEVANT_IDENTITY_HASHES_BY_SURFACE_FIELD] = {
            surface_id: canonical_hash(
                {
                    "schema": "premarket_relevant_identity_set_v1",
                    "surface_id": surface_id,
                    "identities": [],
                }
            )
            for surface_id in sorted(surface_ids)
        }
    relevant_hashes = payload[RELEVANT_IDENTITY_HASHES_BY_SURFACE_FIELD]
    if (
        not isinstance(relevant_hashes, Mapping)
        or set(relevant_hashes) != surface_ids
        or not all(_is_sha256_text(value) for value in relevant_hashes.values())
    ):
        raise EventRegistryError("relevant identity hashes by surface are invalid")
    payload[RELEVANT_IDENTITY_HASHES_BY_SURFACE_FIELD] = dict(
        sorted((str(key), str(value)) for key, value in relevant_hashes.items())
    )
    if EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD not in payload:
        if payload.get("mutation_type") != "metadata_refresh":
            raise EventRegistryError(
                "non-metadata mutation must preserve explicit terminal identities"
            )
        payload[EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD] = {
            surface_id: [] for surface_id in sorted(surface_ids)
        }
    payload[EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD] = _parse_surface_string_lists(
        payload[EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD],
        field_name=EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD,
    )
    active_ids = _parse_active_contract_ids(payload.get(ACTIVE_CONTRACTS_FIELD))
    has_active = ACTIVE_LIFECYCLE_GENERATIONS_FIELD in payload
    has_high_water = LIFECYCLE_GENERATION_HIGH_WATER_FIELD in payload
    if has_active != has_high_water:
        raise EventRegistryError(
            "active lifecycle generation and high-water fields must be written together"
        )
    derived_high_water = _derived_lifecycle_high_water(registry_entries)
    if has_active:
        active = _parse_lifecycle_generation_state(
            payload[ACTIVE_LIFECYCLE_GENERATIONS_FIELD],
            field_name=ACTIVE_LIFECYCLE_GENERATIONS_FIELD,
        )
        high_water = _parse_lifecycle_generation_state(
            payload[LIFECYCLE_GENERATION_HIGH_WATER_FIELD],
            field_name=LIFECYCLE_GENERATION_HIGH_WATER_FIELD,
        )
    else:
        high_water = {
            venue: dict(values)
            for venue, values in derived_high_water.items()
        }
        active = {adapter.venue: {} for adapter in ADAPTERS}
        for venue, contracts in active_ids.items():
            for contract in contracts:
                generation = high_water[venue].get(contract, 0)
                active[venue][contract] = generation
                high_water[venue][contract] = max(
                    high_water[venue].get(contract, -1), generation
                )
    for venue in active:
        if sorted(active[venue]) != active_ids[venue]:
            raise EventRegistryError(
                "active contract ids do not match exact lifecycle generations"
            )
        for contract, generation in active[venue].items():
            if high_water[venue].get(contract, -1) != generation:
                raise EventRegistryError(
                    "active lifecycle generation must equal contract high-water"
                )
        for contract, generation in derived_high_water[venue].items():
            if high_water[venue].get(contract, -1) < generation:
                raise EventRegistryError("lifecycle generation high-water regressed")
    payload[ACTIVE_LIFECYCLE_GENERATIONS_FIELD] = {
        venue: dict(sorted(values.items()))
        for venue, values in sorted(active.items())
    }
    payload[LIFECYCLE_GENERATION_HIGH_WATER_FIELD] = {
        venue: dict(sorted(values.items()))
        for venue, values in sorted(high_water.items())
    }
    return payload


def _write_summary_with_mutation_receipt(
    path: Path,
    summary: Mapping[str, Any],
    *,
    lock_owner: RegistryLockOwner | None,
) -> dict[str, Any]:
    """Write the mutable summary, then seal it with one O_EXCL receipt.

    Production callers must hold the registry mutation lock.  Tests may bootstrap a
    non-production temporary path before rebinding it as their production fixture.
    """
    production_registry = path.resolve(strict=False) == REGISTRY_PATH.resolve(strict=False)
    if lock_owner is None:
        if production_registry:
            raise EventRegistryError("production mutation receipt requires registry lock")
    else:
        expected_lock_path = (
            REGISTRY_LOCK_PATH if production_registry else path.with_suffix(".lock")
        )
        if lock_owner.path.resolve(strict=False) != expected_lock_path.resolve(strict=False):
            raise EventRegistryError("registry lock does not cover mutation receipt")
        _assert_registry_lock_owner(lock_owner)

    receipts, receipt_problems = _load_mutation_receipt_chain(path)
    if receipt_problems:
        raise EventRegistryError(
            "existing mutation receipt chain is invalid: " + "; ".join(receipt_problems)
        )
    mutation_seq = len(receipts)
    previous_hash = receipts[-1]["receipt_hash"] if receipts else None
    summary_payload = {
        key: value for key, value in dict(summary).items() if key not in MUTATION_SUMMARY_LINK_FIELDS
    }
    registry_bytes = path.read_bytes() if path.is_file() else b""
    registry_entries = _load_registry_bytes(registry_bytes, path=path)
    summary_payload = _summary_with_exact_lifecycle_state(
        summary_payload,
        registry_entries=registry_entries,
    )
    record = {
        "schema": MUTATION_RECEIPT_SCHEMA,
        "mutation_seq": mutation_seq,
        "previous_mutation_receipt_hash": previous_hash,
        "registry_path_name": path.name,
        "registry_sha256": hashlib.sha256(registry_bytes).hexdigest(),
        "registry_entries": len(registry_entries),
        "registry_head_record_hash": (
            registry_entries[-1].get("record_hash") if registry_entries else None
        ),
        "summary_content_hash": _summary_content_hash(summary_payload),
        "mutation_type": summary_payload.get("mutation_type"),
        "mutation_run_id": (
            summary_payload.get("mutation_run_id")
            if summary_payload.get("mutation_type") == "official_attestation"
            else summary_payload.get("refresh_run_id")
            or summary_payload.get("mutation_run_id")
        ),
        "plan_id": summary_payload.get("plan_id"),
        "plan_hash": summary_payload.get("plan_hash"),
        ACTIVE_CONTRACTS_FIELD: summary_payload.get(ACTIVE_CONTRACTS_FIELD),
        ACTIVE_LIFECYCLE_GENERATIONS_FIELD: summary_payload.get(
            ACTIVE_LIFECYCLE_GENERATIONS_FIELD
        ),
        LIFECYCLE_GENERATION_HIGH_WATER_FIELD: summary_payload.get(
            LIFECYCLE_GENERATION_HIGH_WATER_FIELD
        ),
        LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD: summary_payload.get(
            LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD
        ),
        RAW_UNIVERSE_ROWS_FIELD: summary_payload.get(RAW_UNIVERSE_ROWS_FIELD),
        RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD: summary_payload.get(
            RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD
        ),
        RELEVANT_IDENTITY_IDS_BY_SURFACE_FIELD: summary_payload.get(
            RELEVANT_IDENTITY_IDS_BY_SURFACE_FIELD
        ),
        RELEVANT_IDENTITY_HASHES_BY_SURFACE_FIELD: summary_payload.get(
            RELEVANT_IDENTITY_HASHES_BY_SURFACE_FIELD
        ),
        EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD: summary_payload.get(
            EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD
        ),
    }
    record["receipt_hash"] = canonical_hash(record)
    linked_summary = dict(summary_payload)
    linked_summary.update(
        {
            "mutation_receipt_schema": MUTATION_RECEIPT_SCHEMA,
            "mutation_seq": mutation_seq,
            "previous_mutation_receipt_hash": previous_hash,
            "mutation_receipt_hash": record["receipt_hash"],
        }
    )
    _write_json_atomic(path.with_suffix(".summary.json"), linked_summary)

    receipt_dir = _mutation_receipt_dir(path)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / (
        f"{mutation_seq:020d}-{record['receipt_hash']}.json"
    )
    try:
        descriptor = os.open(receipt_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise EventRegistryError(f"mutation receipt already exists: {receipt_path}") from exc
    committed = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        committed = True
    finally:
        if not committed:
            receipt_path.unlink(missing_ok=True)
    return linked_summary


def _snapshot_mutation_files(path: Path) -> dict[str, Any]:
    summary_path = path.with_suffix(".summary.json")
    receipt_dir = _mutation_receipt_dir(path)
    return {
        "registry_exists": path.is_file(),
        "registry_bytes": path.read_bytes() if path.is_file() else b"",
        "summary_exists": summary_path.is_file(),
        "summary_bytes": summary_path.read_bytes() if summary_path.is_file() else b"",
        "receipt_dir_exists": receipt_dir.is_dir(),
        "receipt_names": frozenset(
            item.name for item in receipt_dir.glob("*.json")
        ) if receipt_dir.is_dir() else frozenset(),
    }


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.rollback.tmp"
    )
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _rollback_uncommitted_mutation(path: Path, snapshot: Mapping[str, Any]) -> bool:
    """Restore both mutable files iff no new immutable receipt was committed."""
    receipt_dir = _mutation_receipt_dir(path)
    current_receipts = frozenset(
        item.name for item in receipt_dir.glob("*.json")
    ) if receipt_dir.is_dir() else frozenset()
    if current_receipts - set(snapshot["receipt_names"]):
        return False
    if snapshot["registry_exists"]:
        _write_bytes_atomic(path, bytes(snapshot["registry_bytes"]))
    else:
        path.unlink(missing_ok=True)
    summary_path = path.with_suffix(".summary.json")
    if snapshot["summary_exists"]:
        _write_bytes_atomic(summary_path, bytes(snapshot["summary_bytes"]))
    else:
        summary_path.unlink(missing_ok=True)
    if not snapshot["receipt_dir_exists"] and receipt_dir.is_dir():
        try:
            receipt_dir.rmdir()
        except OSError:
            pass
    return True



@dataclass(frozen=True)
class RefreshSurfaceStage:
    rows_by_venue: Mapping[str, list[Mapping[str, Any]]]
    rows_by_surface: Mapping[str, list[Mapping[str, Any]]]
    pages_by_venue: Mapping[str, int]
    truncated_venues: tuple[str, ...]
    venue_errors: Mapping[str, str]


def _stage_refresh_surfaces(
    *,
    payloads: Mapping[str, Any] | None,
    timeout_sec: int,
) -> RefreshSurfaceStage:
    adapters = {adapter.venue: adapter for adapter in ADAPTERS}
    surfaces = {surface.surface_id: surface for surface in SURFACES}
    rows_by_surface: dict[str, list[Mapping[str, Any]]] = {}
    rows_by_venue: dict[str, list[Mapping[str, Any]]] = {}
    pages_by_venue: dict[str, int] = {}
    truncated: list[str] = []
    errors: dict[str, str] = {}

    # Bybit requires two separately paged surfaces.  PreLaunch discovers the
    # contract; Trading supplies the explicit post-transition observation after the
    # same native id disappears from PreLaunch.  Querying only PreLaunch makes a
    # legitimate transition indistinguishable from acquisition loss.
    try:
        adapter = adapters["bybit"]
        bybit_surfaces = (
            surfaces["bybit_linear_prelaunch"],
            surfaces["bybit_linear_trading"],
        )
        supplied_by_surface: dict[str, Any] = {}
        if payloads is not None:
            if "bybit" not in payloads:
                raise EventRegistryError("venue payload is missing")
            supplied = payloads["bybit"]
            structured = (
                supplied
                if isinstance(supplied, Mapping)
                and ("prelaunch" in supplied or "trading" in supplied)
                else None
            )
            if structured is not None:
                if not all(name in structured for name in ("prelaunch", "trading")):
                    raise EventRegistryError("missing required Bybit surface fixture")
                supplied_by_surface = {
                    "bybit_linear_prelaunch": structured["prelaunch"],
                    "bybit_linear_trading": structured["trading"],
                }
            else:
                # Compatibility for pre-v17 offline fixtures.  They describe only
                # discovery and therefore get an explicit empty Trading surface.
                supplied_by_surface = {
                    "bybit_linear_prelaunch": supplied,
                    "bybit_linear_trading": {
                        "retCode": 0,
                        "result": {
                            "category": "linear",
                            "list": [],
                            "nextPageCursor": "",
                        },
                    },
                }

        bybit_rows: list[Mapping[str, Any]] = []
        bybit_pages = 0
        bybit_truncated = False
        for surface in bybit_surfaces:
            surface_adapter = replace(adapter, params=dict(surface.params))
            if payloads is not None:
                supplied_surface = supplied_by_surface[surface.surface_id]
                pages = (
                    list(supplied_surface)
                    if isinstance(supplied_surface, list)
                    else [supplied_surface]
                )
                queue = list(pages)

                def bybit_fetch(_adapter, _params, *, _queue=queue):  # noqa: ANN001
                    if not _queue:
                        raise EventRegistryError(
                            "bybit pagination payload ended before its cursor"
                        )
                    payload = _queue.pop(0)
                    _validate_payload_shape(_adapter, payload)
                    return payload
            else:
                def bybit_fetch(adapter_, params):  # noqa: ANN001
                    payload = public_http.get_json(
                        adapter_.url, params=params, timeout_sec=timeout_sec
                    )
                    _validate_payload_shape(adapter_, payload)
                    return payload

            result = fetch_venue(surface_adapter, bybit_fetch)
            surface_payload = {
                "retCode": 0,
                "result": {"category": "linear", "list": list(result.rows)},
            }
            surface_rows = _surface_payload_rows(surface, surface_payload)
            rows_by_surface[surface.surface_id] = list(surface_rows)
            bybit_rows.extend(surface_rows)
            bybit_pages += result.pages
            bybit_truncated = bybit_truncated or result.truncated
        if bybit_truncated:
            truncated.append("bybit")
        rows_by_venue["bybit"] = bybit_rows
        pages_by_venue["bybit"] = bybit_pages
    except Exception as exc:  # noqa: BLE001 - one surface invalidates the stage
        errors["bybit"] = f"{type(exc).__name__}: {exc}"

    # OKX exposes two independent required surfaces. A legacy combined injected
    # payload is accepted only for offline fixtures; production always makes both
    # explicit requests below.
    try:
        okx_surfaces = (surfaces["okx_swap"], surfaces["okx_futures"])
        okx_rows: list[Mapping[str, Any]] = []
        if payloads is not None:
            if "okx" not in payloads:
                raise EventRegistryError("venue payload is missing")
            supplied = payloads["okx"]
            structured = (
                supplied
                if isinstance(supplied, Mapping)
                and ("swap" in supplied or "futures" in supplied)
                else None
            )
            if structured is not None:
                if not all(name in structured for name in ("swap", "futures")):
                    raise EventRegistryError("missing required OKX surface fixture")
                supplied_by_surface = {
                    "okx_swap": structured["swap"],
                    "okx_futures": structured["futures"],
                }
                for surface in okx_surfaces:
                    rows = _surface_payload_rows(
                        surface, supplied_by_surface[surface.surface_id]
                    )
                    rows_by_surface[surface.surface_id] = list(rows)
                    okx_rows.extend(rows)
            else:
                _validate_payload_shape(adapters["okx"], supplied)
                combined_rows = adapters["okx"].rows(supplied)
                for surface in okx_surfaces:
                    rows = [
                        row
                        for row in combined_rows
                        if str(row.get("instType") or "")
                        == surface.expected_instrument_type
                    ]
                    rows_by_surface[surface.surface_id] = rows
                    okx_rows.extend(rows)
        else:
            def surface_fetch(surface, params):  # noqa: ANN001
                return public_http.get_json(
                    surface.url, params=params, timeout_sec=timeout_sec
                )

            for surface in okx_surfaces:
                rows = fetch_surface(surface, surface_fetch)
                rows_by_surface[surface.surface_id] = list(rows)
                okx_rows.extend(rows)
        rows_by_venue["okx"] = okx_rows
        pages_by_venue["okx"] = 2
    except Exception as exc:  # noqa: BLE001
        errors["okx"] = f"{type(exc).__name__}: {exc}"

    try:
        gate_surface = surfaces["gate_usdt_contracts"]
        if payloads is not None:
            if "gate" not in payloads:
                raise EventRegistryError("venue payload is missing")
            rows = _surface_payload_rows(gate_surface, payloads["gate"])
        else:
            rows = fetch_surface(
                gate_surface,
                lambda surface, params: public_http.get_json(
                    surface.url, params=params, timeout_sec=timeout_sec
                ),
            )
        rows_by_surface[gate_surface.surface_id] = list(rows)
        rows_by_venue["gate"] = list(rows)
        pages_by_venue["gate"] = 1
    except Exception as exc:  # noqa: BLE001
        errors["gate"] = f"{type(exc).__name__}: {exc}"

    return RefreshSurfaceStage(
        rows_by_venue=dict(rows_by_venue),
        rows_by_surface=dict(rows_by_surface),
        pages_by_venue=dict(pages_by_venue),
        truncated_venues=tuple(sorted(truncated)),
        venue_errors=dict(sorted(errors.items())),
    )


def refresh(
    *,
    payloads: Mapping[str, Any] | None = None,
    path: Path | None = None,
    observed_at_utc: str | None = None,
    timeout_sec: int = 20,
    run_id: str,
) -> dict[str, Any]:
    """Stage every venue, then atomically append only a complete verified refresh."""
    if not str(run_id).strip():
        raise EventRegistryError("metadata refresh run_id is required")
    try:
        preflight = risk_gate.preflight(write_class="metadata_registry", run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - no failed preflight may reach network
        raise EventRegistryError(f"PREFLIGHT_BLOCKED: {type(exc).__name__}: {exc}") from exc
    if not _preflight_is_exact(
        preflight,
        write_class="metadata_registry",
        run_id=run_id,
        decision="ALLOW_METADATA_REGISTRY",
        action=risk_gate.METADATA_REGISTRY_ACTION,
    ):
        raise EventRegistryError("PREFLIGHT_BLOCKED: metadata preflight is not verified")

    registry_path = path or REGISTRY_PATH
    production_registry = (
        registry_path.resolve(strict=False) == REGISTRY_PATH.resolve(strict=False)
    )
    if payloads is not None and production_registry:
        raise EventRegistryError(
            "INJECTED_PAYLOADS_FORBIDDEN_FOR_PRODUCTION: supplied fixtures require "
            "an explicit non-production path"
        )
    if payloads is None and (path is not None or not production_registry):
        raise EventRegistryError(
            "LIVE_REFRESH_REQUIRES_CANONICAL_PRODUCTION_PATH: omit path for the "
            "gate-authorized public metadata refresh"
        )
    if payloads is None and observed_at_utc is not None:
        raise EventRegistryError(
            "LIVE_REFRESH_OBSERVED_AT_OVERRIDE_FORBIDDEN: completion time is "
            "writer-owned"
        )
    refresh_evidence_class = (
        "PUBLIC_VENUE_METADATA_LIVE"
        if payloads is None
        else "SYNTHETIC_OFFLINE_FIXTURE_ONLY"
    )
    production_eligible = payloads is None and production_registry
    staged_from_mutation = _mutation_compare_and_swap_token(registry_path)

    observed: list[dict[str, Any]] = []
    staged_surface_rows: dict[str, list[Mapping[str, Any]]] = {}
    staged_rows: dict[str, list[Mapping[str, Any]]] = {}
    per_venue: dict[str, int] = {}
    per_venue_pages: dict[str, int] = {}
    truncated: list[str] = []
    errors: dict[str, str] = {}

    stage = _stage_refresh_surfaces(payloads=payloads, timeout_sec=timeout_sec)
    staged_rows.update(stage.rows_by_venue)
    staged_surface_rows.update(stage.rows_by_surface)
    per_venue_pages.update(stage.pages_by_venue)
    truncated.extend(stage.truncated_venues)
    errors.update(stage.venue_errors)

    observed_at_utc = (
        _writer_refresh_completed_at_utc()
        if payloads is None
        else observed_at_utc or utc_now_iso()
    )
    raw_universe_rows = {
        adapter.venue: len(staged_rows.get(adapter.venue, ()))
        for adapter in ADAPTERS
    }
    raw_universe_rows_by_surface = {
        surface.surface_id: len(staged_surface_rows.get(surface.surface_id, ()))
        for surface in SURFACES
    }
    if production_eligible:
        surfaces_by_id = {surface.surface_id: surface for surface in SURFACES}
        for surface_id in FULL_UNIVERSE_SURFACE_IDS:
            if raw_universe_rows_by_surface.get(surface_id, 0) == 0:
                venue = surfaces_by_id[surface_id].venue
                errors[venue] = (
                    "EventRegistryError: empty required full-universe surface "
                    f"cannot be complete: {surface_id}"
                )
    else:
        # Legacy offline fixtures may not model every v17 surface, but retain the
        # older aggregate completeness boundary for their descriptive-only output.
        for venue in ("okx", "gate"):
            if venue in staged_rows and raw_universe_rows[venue] == 0:
                errors[venue] = (
                    "EventRegistryError: empty full-universe response cannot be complete"
                )
    adapters_by_venue = {adapter.venue: adapter for adapter in ADAPTERS}
    for venue, rows in staged_rows.items():
        events = normalise_rows(
            adapters_by_venue[venue], rows, observed_at_utc=observed_at_utc
        )
        per_venue[venue] = len(events)
        observed.extend(events)

    if errors or truncated or set(per_venue) != {adapter.venue for adapter in ADAPTERS}:
        return {
            "schema": REGISTRY_SCHEMA,
            "status": "INCOMPLETE_NO_REGISTRY_WRITE",
            "complete": False,
            "refreshed_at_utc": observed_at_utc,
            "observed_events": len(observed),
            "observed_by_venue": per_venue,
            "pages_by_venue": per_venue_pages,
            "truncated_venues": sorted(truncated),
            "venue_errors": dict(sorted(errors.items())),
            "appended_entries": 0,
            "refresh_evidence_class": refresh_evidence_class,
            "production_eligible": production_eligible,
            RAW_UNIVERSE_ROWS_FIELD: dict(sorted(raw_universe_rows.items())),
            RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD: dict(
                sorted(raw_universe_rows_by_surface.items())
            ),
        }

    current_active = _active_contract_ids_from_rows(staged_rows)
    lifecycle_contracts = _lifecycle_contract_ids_from_rows(staged_rows)
    lock_path = (
        REGISTRY_LOCK_PATH
        if registry_path.resolve(strict=False) == REGISTRY_PATH.resolve(strict=False)
        else registry_path.with_suffix(".lock")
    )
    with registry_lock(
        lock_path,
        run_id=run_id,
        plan_hash=str(preflight["plan_hash"]),
    ) as lock_owner:
        committed_mutation = _mutation_compare_and_swap_token(registry_path)
        if committed_mutation != staged_from_mutation:
            raise EventRegistryError(
                "STALE_REFRESH_STAGE: compare-and-swap base changed while venue "
                "payloads were staged"
            )
        existing, existing_report = _verify_registry_snapshot(
            registry_path,
            bootstrap_lock_owner=lock_owner,
        )
        if existing_report["status"] != "REGISTRY_OK":
            raise EventRegistryError(
                "existing registry lineage is invalid: "
                + "; ".join(existing_report["problems"])
            )
        previous_relevant_ids = _summary_relevant_identity_ids(
            registry_path.with_suffix(".summary.json")
        ) or {surface.surface_id: [] for surface in SURFACES}
        previous_terminal_ids = _summary_explicit_terminal_identity_ids(
            registry_path.with_suffix(".summary.json")
        ) or {surface.surface_id: [] for surface in SURFACES}
        previous_tracked_by_venue: dict[str, set[str]] = {
            adapter.venue: set() for adapter in ADAPTERS
        }
        previous_classification_known_by_venue: dict[str, set[str]] = {
            adapter.venue: set() for adapter in ADAPTERS
        }
        for surface in SURFACES:
            relevant_ids = previous_relevant_ids.get(surface.surface_id, ())
            previous_tracked_by_venue[surface.venue].update(relevant_ids)
            previous_classification_known_by_venue[surface.venue].update(relevant_ids)
            previous_classification_known_by_venue[surface.venue].update(
                previous_terminal_ids.get(surface.surface_id, ())
            )
        observed_moment = _parse_explicit_utc(observed_at_utc)
        if observed_moment is None:
            raise EventRegistryError("refresh completion timestamp is invalid")
        identity_snapshots = {
            surface.surface_id: build_relevant_identity_snapshot(
                surface,
                staged_surface_rows.get(surface.surface_id, ()),
                now_ts=int(observed_moment.timestamp()),
                # OKX can move one native id from the SWAP surface to the FUTURES
                # surface during xperp conversion.  Classification therefore needs
                # the venue-wide known set. Immediately previous terminal ids are
                # classification-only: they are never required to remain present and
                # never keep a generation active.
                tracked_ids=previous_tracked_by_venue[surface.venue],
                classification_ids=previous_classification_known_by_venue[
                    surface.venue
                ],
            )
            for surface in SURFACES
        }
        identity_problems = [
            problem
            for snapshot in identity_snapshots.values()
            for problem in snapshot.problems
            if not problem.startswith("MISSING_TRACKED_IDENTITIES:")
        ]
        for venue, tracked_ids in sorted(previous_tracked_by_venue.items()):
            seen_ids = {
                native_id
                for surface in SURFACES
                if surface.venue == venue
                for native_id in identity_snapshots[
                    surface.surface_id
                ].observations_by_id
            }
            missing_ids = sorted(tracked_ids - seen_ids)
            if missing_ids:
                identity_problems.append(
                    f"MISSING_TRACKED_IDENTITIES:{venue}:" + ",".join(missing_ids)
                )
        if identity_problems:
            raise EventRegistryError(
                "INCOMPLETE_RELEVANT_IDENTITY_SET: " + "; ".join(identity_problems)
            )
        relevant_identity_ids_by_surface = {
            surface_id: list(snapshot.relevant_ids)
            for surface_id, snapshot in sorted(identity_snapshots.items())
        }
        relevant_identity_hashes_by_surface = {
            surface_id: snapshot.relevant_identity_set_sha256
            for surface_id, snapshot in sorted(identity_snapshots.items())
        }
        explicit_terminal_ids_by_surface = {
            surface_id: list(snapshot.explicit_terminal_ids)
            for surface_id, snapshot in sorted(identity_snapshots.items())
        }
        current_active, relevant_identity_ids_by_surface = (
            apply_cross_surface_terminal_precedence(
                identity_snapshots=identity_snapshots,
                current_active=current_active,
                relevant_identity_ids_by_surface=relevant_identity_ids_by_surface,
            )
        )
        current_terminal_ids_by_venue: dict[str, set[str]] = {
            adapter.venue: set() for adapter in ADAPTERS
        }
        surfaces_by_id = {surface.surface_id: surface for surface in SURFACES}
        for surface_id, terminal_ids in explicit_terminal_ids_by_surface.items():
            current_terminal_ids_by_venue[
                surfaces_by_id[surface_id].venue
            ].update(terminal_ids)
        # A later surface can prove that an earlier active row is stale. Do not append
        # another launch/transition revision from that stale row after terminal
        # precedence has removed it from the current lifecycle state.
        observed = [
            item
            for item in observed
            if str(item.get("premarket_contract_id") or item.get("symbol") or "")
            not in current_terminal_ids_by_venue.get(str(item.get("venue") or ""), set())
        ]
        per_venue = {
            adapter.venue: sum(
                1 for item in observed if item.get("venue") == adapter.venue
            )
            for adapter in ADAPTERS
        }
        previous_universe_rows = _summary_raw_universe_counts(
            registry_path.with_suffix(".summary.json")
        )
        if previous_universe_rows is not None:
            for venue in ("okx", "gate"):
                previous_count = previous_universe_rows[venue]
                current_count = raw_universe_rows[venue]
                if (
                    previous_count > 0
                    and current_count / previous_count
                    < MIN_FULL_UNIVERSE_RETENTION_RATIO
                ):
                    raise EventRegistryError(
                        "INCOMPLETE_UNIVERSE_DROP: "
                        f"{venue} raw rows fell from {previous_count} to "
                        f"{current_count}"
                    )
        if production_eligible:
            previous_surface_rows = _summary_raw_universe_counts_by_surface(
                registry_path.with_suffix(".summary.json")
            )
            if previous_surface_rows is not None:
                for surface_id in FULL_UNIVERSE_SURFACE_IDS:
                    previous_count = previous_surface_rows[surface_id]
                    current_count = raw_universe_rows_by_surface[surface_id]
                    if (
                        previous_count > 0
                        and current_count / previous_count
                        < MIN_FULL_UNIVERSE_RETENTION_RATIO
                    ):
                        raise EventRegistryError(
                            "INCOMPLETE_UNIVERSE_DROP: "
                            f"{surface_id} raw rows fell from {previous_count} to "
                            f"{current_count}"
                        )
        previous_active, previous_high_water = _load_lifecycle_generation_state(
            registry_path.with_suffix(".summary.json"),
            existing=existing,
            current_active_for_legacy=current_active,
        )
        active_generations, lifecycle_high_water = _allocate_current_lifecycle_state(
            current_active,
            previous_active=previous_active,
            previous_high_water=previous_high_water,
        )
        observation_generations, lifecycle_high_water = (
            _bind_relevant_lifecycle_generations(
                lifecycle_contracts,
                active_generations=active_generations,
                previous_active=previous_active,
                lifecycle_high_water=lifecycle_high_water,
            )
        )
        bound_observed = _bind_lifecycle_generations(
            observed,
            active_generations=observation_generations,
        )
        terminal_observed = build_terminal_lifecycle_observations(
            identity_snapshots=identity_snapshots,
            rows_by_surface=staged_surface_rows,
            previous_active=previous_active,
            received_at_utc=observed_at_utc,
        )
        bound_observed.extend(terminal_observed)
        observed.extend(terminal_observed)
        for item in terminal_observed:
            venue = str(item["venue"])
            per_venue[venue] = per_venue.get(venue, 0) + 1
        appended = build_stream_revisions(existing, bound_observed)
        candidate_problems = _verify_records([*existing, *appended])
        if candidate_problems:
            raise EventRegistryError(
                "candidate registry failed verification before append: "
                + "; ".join(candidate_problems)
            )
        try:
            commit_preflight = risk_gate.preflight(
                write_class="metadata_registry", run_id=run_id
            )
        except Exception as exc:  # noqa: BLE001 - commit must fail closed
            raise EventRegistryError(
                f"PREFLIGHT_BLOCKED_AT_COMMIT: {type(exc).__name__}: {exc}"
            ) from exc
        if not _preflight_is_exact(
            commit_preflight,
            write_class="metadata_registry",
            run_id=run_id,
            decision="ALLOW_METADATA_REGISTRY",
            action=risk_gate.METADATA_REGISTRY_ACTION,
        ) or any(
            commit_preflight.get(field) != preflight.get(field)
            for field in ("plan_id", "plan_hash", "resolved_paths_hash")
        ):
            raise EventRegistryError(
                "PREFLIGHT_BLOCKED_AT_COMMIT: write authority changed during staging"
            )
        mutation_snapshot = _snapshot_mutation_files(registry_path)
        try:
            written = append_entries(appended, registry_path, lock_owner=lock_owner)
            _entries_after, report = _verify_registry_snapshot(
                registry_path,
                verify_summary=False,
                bootstrap_lock_owner=lock_owner,
            )
            if report["status"] != "REGISTRY_OK":
                raise EventRegistryError(
                    "registry failed verification after append: "
                    + "; ".join(report["problems"])
                )
            summary = {
                "schema": REGISTRY_SCHEMA,
                "status": "REFRESH_COMPLETE",
                "mutation_type": "metadata_refresh",
                "mutation_run_id": run_id,
                "refresh_run_id": run_id,
                "plan_id": preflight["plan_id"],
                "plan_hash": preflight["plan_hash"],
                "resolved_paths_hash": preflight["resolved_paths_hash"],
                "refreshed_at_utc": observed_at_utc,
                LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD: observed_at_utc,
                "observed_events": len(observed),
                "observed_by_venue": per_venue,
                "pages_by_venue": per_venue_pages,
                "truncated_venues": [],
                "venue_errors": {},
                "complete": True,
                ACTIVE_CONTRACTS_FIELD: current_active,
                ACTIVE_LIFECYCLE_GENERATIONS_FIELD: active_generations,
                LIFECYCLE_GENERATION_HIGH_WATER_FIELD: lifecycle_high_water,
                "refresh_evidence_class": refresh_evidence_class,
                "production_eligible": production_eligible,
                RAW_UNIVERSE_ROWS_FIELD: dict(sorted(raw_universe_rows.items())),
                RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD: dict(
                    sorted(raw_universe_rows_by_surface.items())
                ),
                RELEVANT_IDENTITY_IDS_BY_SURFACE_FIELD: (
                    relevant_identity_ids_by_surface
                ),
                RELEVANT_IDENTITY_HASHES_BY_SURFACE_FIELD: (
                    relevant_identity_hashes_by_surface
                ),
                EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD: (
                    explicit_terminal_ids_by_surface
                ),
                "appended_entries": written,
                "new_streams": sum(
                    1 for entry in appended if int(entry["stream_revision"]) == 0
                ),
                "revisions": sum(
                    1 for entry in appended if int(entry["stream_revision"]) > 0
                ),
                "registry": report,
            }
            summary = _write_summary_with_mutation_receipt(
                registry_path,
                summary,
                lock_owner=lock_owner,
            )
            _final_entries, final_report = _verify_registry_snapshot(
                registry_path,
                bootstrap_lock_owner=lock_owner,
            )
            if final_report["status"] != "REGISTRY_OK":
                raise EventRegistryError(
                    "registry transaction failed final verification: "
                    + "; ".join(final_report["problems"])
                )
        except BaseException:
            _rollback_uncommitted_mutation(registry_path, mutation_snapshot)
            raise
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Listing-event registry for pre-market perpetuals.")
    parser.add_argument("--refresh", action="store_true",
                        help="read public instrument metadata and append what changed")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--upcoming", action="store_true")
    parser.add_argument("--horizon-hours", type=int, default=24)
    parser.add_argument("--source-class", choices=SOURCE_CLASSES)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--payloads", default="",
                        help="JSON file of {venue: payload}; refreshes offline")
    args = parser.parse_args(argv)

    if args.verify:
        report = verify_registry()
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["status"] == "REGISTRY_OK" else 1
    if args.upcoming:
        if args.source_class is None:
            parser.error("--source-class is required with --upcoming")
        report = verify_registry()
        if report["status"] != "REGISTRY_OK":
            raise EventRegistryError(
                "registry verification failed: " + "; ".join(report["problems"])
            )
        upcoming = events_for_capture(
            now_ts=int(time.time()),
            source_class=args.source_class,
            asset_class=ASSET_CLASS_CRYPTO_TOKEN,
            horizon_sec=args.horizon_hours * 3600,
        )
        print(json.dumps({
            "status": "UPCOMING",
            "horizon_hours": args.horizon_hours,
            "count": len(upcoming),
            "events": upcoming,
        }, ensure_ascii=False))
        return 0
    if args.refresh:
        payloads = None
        if args.payloads:
            payloads = json.loads(Path(args.payloads).read_text(encoding="utf-8"))
        run_id = args.run_id or (
            "metadata_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )
        print(json.dumps(refresh(payloads=payloads, run_id=run_id), ensure_ascii=False))
        return 0
    raise SystemExit("no action requested")


if __name__ == "__main__":
    raise SystemExit(main())
