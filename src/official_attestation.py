"""Recording an official spot t0 that a person actually read, with what they read.

Why this exists, measured rather than assumed. On 2026-08-23 none of the three venues
published the official spot listing moment as a machine-readable field:

* Bybit's announcement API carries startDateTimestamp, but for listing announcements it
  equals the publication instant - start and end are the same value on every row, and
  not one announcement across four types had it set in the future. The real moment sits
  in the title text ("...to Delist 1 Token(s) on Aug 21, 2026, 11:00AM UTC").
* OKX's announcement API exposes pTime only. There is no listing-time field at all.
* Gate publishes no announcement API.

And the moment is not merely unstructured, it is ambiguous: one Bybit spot-listing
article contained twenty-four time expressions - deposits opening, trading starting,
campaign windows, withdrawals - so a regular expression over the page would take the
first one and call it t0. That is not extraction, it is invention with a timestamp.

So the official class is populated by a person reading the announcement, and the record
carries the sentence they read. A later reviewer can check the quotation against the
source instead of trusting that whoever ran this picked the right line out of
twenty-four. The attestation is a write like any other: same preflight, same lock, same
append-only revision chain.

This module never fetches anything. It records what was read.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import event_registry as registry
import frozen_plan_bindings as trust_root
import project_config as config
import risk_gate


ATTESTATION_SCHEMA = config.OFFICIAL_ATTESTATION_SCHEMA

# Hosts whose pages count as an official announcement from the venue itself. This is a
# provenance list, not a network allow-list - nothing here is ever fetched - but it is
# the same idea: an "official" t0 sourced from a forum post or an aggregator is a
# different datum wearing the same word.
OFFICIAL_ANNOUNCEMENT_HOSTS = config.OFFICIAL_ANNOUNCEMENT_HOSTS

# Most announcements say "10:00AM UTC", not "10:00:00".  Sixty seconds remains the
# compatibility default, but v29 can preserve one-second precision when the verbatim
# source fragment itself explicitly carries seconds.  Precision is derived; callers
# never get a flag with which to upgrade a minute-only source.
ANNOUNCED_PRECISION_SEC = 60
SECONDS_GRADE_PRECISION_SEC = 1

_QUOTED_SECOND_FORMS = (
    "%b %d, %Y, %I:%M:%S%p UTC",
    "%B %d, %Y, %I:%M:%S%p UTC",
    "%b %d, %Y, %H:%M:%S UTC",
    "%B %d, %Y, %H:%M:%S UTC",
    "%Y-%m-%d %H:%M:%S UTC",
)
_QUOTED_MINUTE_FORMS = (
    "%b %d, %Y, %I:%M%p UTC",
    "%B %d, %Y, %I:%M%p UTC",
    "%b %d, %Y, %H:%M UTC",
    "%B %d, %Y, %H:%M UTC",
    "%Y-%m-%d %H:%M UTC",
)
_QUOTED_ISO_UTC = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?P<second>:\d{2})?"
    r"(?:Z|\+00:00)$"
)

# A capture opens its window before t0, so an anchor must still be far enough ahead to
# be usable. Attesting a moment that has already passed cannot anchor a live capture,
# and silently accepting one invites a capture that can never cover its own window.
MIN_LEAD_SEC = config.CAPTURE_WINDOW_BEFORE_SEC


class AttestationError(RuntimeError):
    pass


def _has_forbidden_unicode_controls(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)


def _canonical_text(value: Any, *, field: str, allow_internal_space: bool) -> str:
    if not isinstance(value, str) or not value:
        raise AttestationError(f"{field} is required")
    if value != value.strip() or _has_forbidden_unicode_controls(value):
        raise AttestationError(f"{field} must be canonical without surrounding whitespace")
    if not allow_internal_space and any(character.isspace() for character in value):
        raise AttestationError(f"{field} must not contain whitespace")
    return value


def parse_announced_utc(value: str) -> int:
    """Accept an explicit UTC instant and nothing looser.

    No natural-language parsing on purpose: the ambiguity this module exists to handle
    is exactly the kind a lenient parser hides."""
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AttestationError(
            f"announced time must be ISO-8601 UTC, e.g. 2026-09-09T04:00:00Z: {value}"
        ) from exc
    if moment.tzinfo is None:
        raise AttestationError("announced time must carry an explicit UTC offset")
    if moment.utcoffset() != timezone.utc.utcoffset(None):
        raise AttestationError("announced time must be expressed in UTC")
    return int(moment.timestamp())


def require_official_source(listing_venue: str, url: str) -> str:
    hosts = OFFICIAL_ANNOUNCEMENT_HOSTS.get(listing_venue)
    if not hosts:
        raise AttestationError(
            f"no official announcement host declared for {listing_venue}"
        )
    canonical_url = _canonical_text(
        url, field="announcement URL", allow_internal_space=False
    )
    if "\\" in canonical_url:
        raise AttestationError("the announcement URL must not contain backslashes")
    parsed = urllib.parse.urlsplit(canonical_url)
    try:
        explicit_port = parsed.port
    except ValueError as exc:
        raise AttestationError("the announcement URL carries an invalid port") from exc
    if parsed.scheme.lower() != "https":
        raise AttestationError("the announcement URL must be https")
    host = (parsed.hostname or "").lower()
    if (
        host not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or explicit_port is not None
    ):
        raise AttestationError(
            f"{host or url} is not an official announcement host for "
            f"{listing_venue}; "
            f"expected one of {', '.join(hosts)}"
        )
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), host, parsed.path, parsed.query, "")
    )


def _normalise_symbol(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def parse_quoted_utc(value: str) -> int:
    """Parse only the exact UTC fragment selected by the attestor."""
    text = " ".join(value.split())
    for form in (*_QUOTED_SECOND_FORMS, *_QUOTED_MINUTE_FORMS):
        try:
            return int(datetime.strptime(text, form).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    if _QUOTED_ISO_UTC.fullmatch(text) is not None:
        try:
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    raise AttestationError(
        "quoted time fragment must be an explicit, unambiguous UTC timestamp"
    )


def quoted_time_precision_sec(value: str) -> int:
    """Derive source precision from the exact quoted fragment.

    An ISO value normalised by the operator is not evidence of seconds.  The quoted
    fragment must visibly contain an ``HH:MM:SS`` component; otherwise the honest
    precision is one minute even when the represented instant happens to end in
    ``:00``.
    """
    text = " ".join(str(value or "").split())
    parse_quoted_utc(text)
    iso_match = _QUOTED_ISO_UTC.fullmatch(text)
    if iso_match is not None:
        return (
            SECONDS_GRADE_PRECISION_SEC
            if iso_match.group("second") is not None
            else ANNOUNCED_PRECISION_SEC
        )
    if any(_matches_quoted_form(text, form) for form in _QUOTED_SECOND_FORMS):
        return SECONDS_GRADE_PRECISION_SEC
    return ANNOUNCED_PRECISION_SEC


def _matches_quoted_form(value: str, form: str) -> bool:
    try:
        datetime.strptime(value, form)
    except ValueError:
        return False
    return True


def require_quotation(
    quote: str,
    announced_utc: str,
    *,
    quoted_time_text: str,
    quoted_symbol_text: str,
    spot_symbol: str,
) -> str:
    """Bind the selected sentence to both the declared timestamp and market."""
    evidence = {
        "quoted sentence": quote,
        "quoted time fragment": quoted_time_text,
        "quoted symbol fragment": quoted_symbol_text,
    }
    for field, value in evidence.items():
        if not isinstance(value, str) or not value:
            raise AttestationError(f"the {field} is required")
        if value != value.strip() or _has_forbidden_unicode_controls(value):
            raise AttestationError(
                f"the {field} must be copied verbatim without control or format characters"
            )
    text = quote
    if len(text) < 24:
        raise AttestationError(
            "the quoted announcement sentence is required and must be the real sentence"
        )
    if not re.search(r"\d", text):
        raise AttestationError("the quoted sentence carries no time at all")
    time_fragment = quoted_time_text
    symbol_fragment = quoted_symbol_text
    if not time_fragment or time_fragment not in text:
        raise AttestationError("the quoted time fragment is not present in the sentence")
    if not symbol_fragment or symbol_fragment not in text:
        raise AttestationError("the quoted symbol fragment is not present in the sentence")
    if parse_quoted_utc(time_fragment) != parse_announced_utc(announced_utc):
        raise AttestationError("the quoted time does not equal announced_utc")
    if _normalise_symbol(symbol_fragment) != _normalise_symbol(spot_symbol):
        raise AttestationError("the quoted symbol does not equal spot_symbol")
    return text


def _build_attestation(
    *,
    venue: str,
    listing_venue: str | None = None,
    spot_symbol: str,
    premarket_contract_id: str,
    lifecycle_generation: int,
    announced_utc: str,
    announcement_url: str,
    quoted_sentence: str,
    quoted_time_text: str,
    quoted_symbol_text: str,
    attested_by: str,
    now_ts: int,
    enforce_min_lead: bool,
    asset_identity: registry.AssetIdentity | None = None,
) -> dict[str, Any]:
    venue = _canonical_text(venue, field="venue", allow_internal_space=False)
    spot_symbol = _canonical_text(
        spot_symbol, field="spot_symbol", allow_internal_space=False
    )
    premarket_contract_id = _canonical_text(
        premarket_contract_id,
        field="premarket_contract_id",
        allow_internal_space=False,
    )
    attested_by = _canonical_text(
        attested_by, field="attested_by author", allow_internal_space=True
    )
    announced_utc = _canonical_text(
        announced_utc, field="announced_utc", allow_internal_space=False
    )
    announcement_url = _canonical_text(
        announcement_url, field="announcement_url", allow_internal_space=False
    )
    if (
        isinstance(lifecycle_generation, bool)
        or not isinstance(lifecycle_generation, int)
        or lifecycle_generation < 0
    ):
        raise AttestationError("lifecycle_generation must be an explicit non-negative integer")
    if venue not in config.PERP_VENUES:
        raise AttestationError(f"unknown perpetual venue: {venue}")
    listing_venue = str(listing_venue or venue)
    if listing_venue not in OFFICIAL_ANNOUNCEMENT_HOSTS:
        raise AttestationError(f"unknown listing venue: {listing_venue}")

    t0_ts = parse_announced_utc(announced_utc)
    url = require_official_source(listing_venue, announcement_url)
    quote = require_quotation(
        quoted_sentence,
        announced_utc,
        quoted_time_text=quoted_time_text,
        quoted_symbol_text=quoted_symbol_text,
        spot_symbol=spot_symbol,
    )
    precision_sec = quoted_time_precision_sec(quoted_time_text)

    lead = t0_ts - now_ts
    if enforce_min_lead and lead < MIN_LEAD_SEC:
        raise AttestationError(
            f"the announced t0 is {lead}s away; a capture opens its window "
            f"{MIN_LEAD_SEC}s before t0 and could not cover it"
        )

    episode_id = registry.make_episode_id(
        venue, premarket_contract_id, lifecycle_generation
    )
    received_at_utc = datetime.fromtimestamp(now_ts, timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    observation = registry.make_timestamp_observation(
        episode_id=episode_id,
        venue=venue,
        premarket_contract_id=premarket_contract_id,
        spot_symbol=spot_symbol,
        timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
        timestamp_ts=t0_ts,
        instrument_role="spot",
        source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
        source_identity=f"human_attestation:{attested_by}",
        source_url=url,
        received_at_utc=received_at_utc,
        precision_sec=precision_sec,
        caveats=("OFFICIAL_T0_READ_BY_A_PERSON_FROM_ANNOUNCEMENT_PROSE",),
        lifecycle_generation=lifecycle_generation,
        asset_identity=asset_identity,
    )
    # The evidence rides with the record, not in a commit message someone has to find.
    # Which exchange listed the underlying is provenance in its own right: the
    # catalyst may come from a venue this project never trades on.
    observation["listing_venue"] = listing_venue
    observation["attestation"] = {
        "schema": ATTESTATION_SCHEMA,
        "attested_by": attested_by,
        "listing_venue": listing_venue,
        "perpetual_venue": venue,
        "announced_utc": datetime.fromtimestamp(t0_ts, timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
        "quoted_sentence": quote,
        "quoted_time_text": quoted_time_text,
        "quoted_symbol_text": quoted_symbol_text,
        "quoted_time_precision_sec": precision_sec,
        "announcement_url": url,
        "lead_sec_at_attestation": lead,
    }
    return observation


def build_attestation(
    *,
    venue: str,
    listing_venue: str | None = None,
    spot_symbol: str,
    premarket_contract_id: str,
    lifecycle_generation: int,
    announced_utc: str,
    announcement_url: str,
    quoted_sentence: str,
    quoted_time_text: str,
    quoted_symbol_text: str,
    attested_by: str,
    now_ts: int,
    asset_identity: registry.AssetIdentity | None = None,
) -> dict[str, Any]:
    """Build a new acceptance anchor and enforce usable causal lead."""
    return _build_attestation(
        venue=venue,
        listing_venue=listing_venue,
        spot_symbol=spot_symbol,
        premarket_contract_id=premarket_contract_id,
        lifecycle_generation=lifecycle_generation,
        announced_utc=announced_utc,
        announcement_url=announcement_url,
        quoted_sentence=quoted_sentence,
        quoted_time_text=quoted_time_text,
        quoted_symbol_text=quoted_symbol_text,
        attested_by=attested_by,
        now_ts=now_ts,
        enforce_min_lead=True,
        asset_identity=asset_identity,
    )


def attest(
    *,
    run_id: str,
    venue: str,
    listing_venue: str | None = None,
    spot_symbol: str,
    premarket_contract_id: str,
    lifecycle_generation: int,
    announced_utc: str,
    announcement_url: str,
    quoted_sentence: str,
    quoted_time_text: str,
    quoted_symbol_text: str,
    attested_by: str,
    path: Path | None = None,
) -> dict[str, Any]:
    """Preflight, lock, append. Recording an official t0 is a registry write."""
    run_id = _canonical_text(run_id, field="run_id", allow_internal_space=False)
    try:
        preflight = risk_gate.preflight(write_class="official_attestation", run_id=run_id)
    except Exception as exc:  # noqa: BLE001
        raise AttestationError(f"PREFLIGHT_BLOCKED: {type(exc).__name__}: {exc}") from exc
    if not registry._preflight_is_exact(
        preflight,
        write_class="official_attestation",
        run_id=run_id,
        decision="ALLOW_OFFICIAL_ATTESTATION",
        action=risk_gate.OFFICIAL_ATTESTATION_ACTION,
    ):
        raise AttestationError("PREFLIGHT_BLOCKED: attestation preflight is not verified")

    prelock_now_ts = int(time.time())
    observation = _build_attestation(
        venue=venue,
        listing_venue=listing_venue,
        spot_symbol=spot_symbol,
        premarket_contract_id=premarket_contract_id,
        lifecycle_generation=lifecycle_generation,
        announced_utc=announced_utc,
        announcement_url=announcement_url,
        quoted_sentence=quoted_sentence,
        quoted_time_text=quoted_time_text,
        quoted_symbol_text=quoted_symbol_text,
        attested_by=attested_by,
        now_ts=prelock_now_ts,
        enforce_min_lead=False,
    )

    target = path or registry.REGISTRY_PATH
    lock_path = (
        registry.REGISTRY_LOCK_PATH
        if target.resolve(strict=False) == registry.REGISTRY_PATH.resolve(strict=False)
        else target.with_suffix(".lock")
    )
    with registry.registry_lock(
        lock_path, run_id=run_id, plan_hash=str(preflight["plan_hash"])
    ) as lock_owner:
        existing, existing_report = registry._verify_registry_snapshot(
            target,
            bootstrap_lock_owner=lock_owner,
        )
        if existing_report["status"] != "REGISTRY_OK":
            raise AttestationError(
                "existing registry lineage is invalid: "
                + "; ".join(existing_report["problems"])
            )
        active_generations, lifecycle_high_water = (
            registry._load_lifecycle_generation_state(
                target.with_suffix(".summary.json"),
                existing=existing,
            )
        )
        active_generation = active_generations.get(venue, {}).get(
            premarket_contract_id
        )
        if active_generation is None or lifecycle_generation != active_generation:
            raise AttestationError(
                "new official attestation must target the current active lifecycle generation"
            )
        matching_metadata = [
            entry
            for entry in existing
            if entry.get("episode_id") == observation["episode_id"]
            and entry.get("venue") == venue
            and entry.get("premarket_contract_id") == premarket_contract_id
            and int(entry.get("lifecycle_generation", 0) or 0)
            == lifecycle_generation
            and entry.get("source_class")
            == registry.SOURCE_VENUE_INSTRUMENT_METADATA
            and entry.get("timestamp_kind")
            in {
                registry.TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
                registry.TIMESTAMP_CONTRACT_CREATED,
                registry.TIMESTAMP_TRANSITION,
            }
        ]
        if not matching_metadata:
            raise AttestationError(
                "official t0 has no matching metadata lifecycle episode"
            )
        known_metadata_identities = {
            (
                str(entry.get("asset_class") or registry.ASSET_CLASS_UNCLASSIFIED),
                str(entry.get("issuer_namespace") or ""),
                str(entry.get("issuer_id") or ""),
            )
            for entry in matching_metadata
            if str(entry.get("asset_class") or registry.ASSET_CLASS_UNCLASSIFIED)
            != registry.ASSET_CLASS_UNCLASSIFIED
        }
        if len(known_metadata_identities) != 1:
            raise AttestationError(
                "official t0 requires one known metadata asset identity; legacy or "
                "conflicting ticker-only identity is descriptive-only"
            )
        metadata_asset_class, metadata_namespace, metadata_issuer_id = next(
            iter(known_metadata_identities)
        )
        if metadata_asset_class != registry.ASSET_CLASS_CRYPTO_TOKEN:
            raise AttestationError(
                "official spot t0 for the crypto listing track cannot bind a pre-IPO "
                "equity or other tradfi perpetual"
            )
        asset_identity = registry.AssetIdentity(
            asset_class=metadata_asset_class,
            issuer_namespace=metadata_namespace,
            issuer_id=metadata_issuer_id,
            evidence_class=registry.IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
        )
        observation = _build_attestation(
            venue=venue,
            listing_venue=listing_venue,
            spot_symbol=spot_symbol,
            premarket_contract_id=premarket_contract_id,
            lifecycle_generation=lifecycle_generation,
            announced_utc=announced_utc,
            announcement_url=announcement_url,
            quoted_sentence=quoted_sentence,
            quoted_time_text=quoted_time_text,
            quoted_symbol_text=quoted_symbol_text,
            attested_by=attested_by,
            now_ts=prelock_now_ts,
            enforce_min_lead=False,
            asset_identity=asset_identity,
        )
        mapped_spot_symbols = {
            str(entry.get("spot_symbol") or "").strip()
            for entry in existing
            if entry.get("episode_id") == observation["episode_id"]
            and str(entry.get("spot_symbol") or "").strip()
        }
        if mapped_spot_symbols and any(
            registry._normalise_symbol(mapped) != registry._normalise_symbol(spot_symbol)
            for mapped in mapped_spot_symbols
        ):
            raise AttestationError(
                "spot symbol conflicts with the existing episode mapping"
            )
        last_complete_metadata_refresh_received_at = (
            registry._summary_complete_metadata_refresh_received_at(
                target.with_suffix(".summary.json")
            )
        )
        raw_universe_rows = registry._summary_raw_universe_counts(
            target.with_suffix(".summary.json")
        )
        if raw_universe_rows is None:
            raise AttestationError("raw universe row-count anchor is missing")
        try:
            previous_summary = json.loads(
                target.with_suffix(".summary.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise AttestationError(f"registry summary is unreadable: {exc}") from exc
        surface_authority_fields = {
            field: previous_summary.get(field)
            for field in (
                registry.RAW_UNIVERSE_ROWS_BY_SURFACE_FIELD,
                registry.RELEVANT_IDENTITY_IDS_BY_SURFACE_FIELD,
                registry.RELEVANT_IDENTITY_HASHES_BY_SURFACE_FIELD,
                registry.EXPLICIT_TERMINAL_IDS_BY_SURFACE_FIELD,
            )
        }
        if any(value is None for value in surface_authority_fields.values()):
            raise AttestationError(
                "relevant identity surface authority is missing from registry summary"
            )
        active_contracts = {
            venue_name: sorted(generations)
            for venue_name, generations in sorted(active_generations.items())
        }
        appended = registry.merge_observations(existing, [observation])
        if not appended:
            official_entry = next(
                (
                    entry
                    for entry in reversed(existing)
                    if entry.get("stream_id") == observation["stream_id"]
                ),
                None,
            )
            if official_entry is None:
                raise AttestationError(
                    "duplicate attestation stream head is missing from registry"
                )
            return {
                "schema": ATTESTATION_SCHEMA,
                "status": "ALREADY_RECORDED",
                "episode_id": observation["episode_id"],
                "official_spot_t0": observation["timestamp_ts"],
                "precision_sec": int(official_entry.get("t0_precision_sec") or 0),
                "appended_records": 0,
                "official_record_hash": official_entry["record_hash"],
                "attestation": official_entry["attestation"],
                "plan_hash": preflight["plan_hash"],
            }

        # The writer timestamp is authority for causality.  Pre-lock validation may
        # block on another mutation, so rebuild the observation after lock acquisition
        # and enforce lead again immediately before the append candidate is verified.
        writer_now_ts = int(time.time())
        metadata_refresh_moment = registry._parse_explicit_utc(
            last_complete_metadata_refresh_received_at
        )
        if metadata_refresh_moment is None:
            raise AttestationError("complete metadata refresh anchor is invalid")
        metadata_age_sec = writer_now_ts - int(metadata_refresh_moment.timestamp())
        if not 0 <= metadata_age_sec <= registry.MAX_COMPLETE_METADATA_REFRESH_AGE_SEC:
            raise AttestationError(
                "STALE_METADATA_REFRESH: latest complete metadata refresh is "
                f"{metadata_age_sec}s old; maximum is "
                f"{registry.MAX_COMPLETE_METADATA_REFRESH_AGE_SEC}s"
            )
        observation = _build_attestation(
            venue=venue,
            listing_venue=listing_venue,
            spot_symbol=spot_symbol,
            premarket_contract_id=premarket_contract_id,
            lifecycle_generation=lifecycle_generation,
            announced_utc=announced_utc,
            announcement_url=announcement_url,
            quoted_sentence=quoted_sentence,
            quoted_time_text=quoted_time_text,
            quoted_symbol_text=quoted_symbol_text,
            attested_by=attested_by,
            now_ts=writer_now_ts,
            enforce_min_lead=True,
            asset_identity=asset_identity,
        )
        appended = registry.merge_observations(existing, [observation])
        if not appended:
            raise AttestationError(
                "attestation became an unexpected duplicate during locked rebuild"
            )
        candidate_problems = registry._verify_records([*existing, *appended])
        if candidate_problems:
            raise AttestationError(
                "candidate attestation is semantically invalid before append: "
                + "; ".join(candidate_problems)
            )
        try:
            commit_preflight = risk_gate.preflight(
                write_class="official_attestation", run_id=run_id
            )
        except Exception as exc:  # noqa: BLE001 - commit must fail closed
            raise AttestationError(
                f"PREFLIGHT_BLOCKED_AT_COMMIT: {type(exc).__name__}: {exc}"
            ) from exc
        if not registry._preflight_is_exact(
            commit_preflight,
            write_class="official_attestation",
            run_id=run_id,
            decision="ALLOW_OFFICIAL_ATTESTATION",
            action=risk_gate.OFFICIAL_ATTESTATION_ACTION,
        ) or any(
            commit_preflight.get(field) != preflight.get(field)
            for field in ("plan_id", "plan_hash", "resolved_paths_hash")
        ):
            raise AttestationError(
                "PREFLIGHT_BLOCKED_AT_COMMIT: write authority changed after lock acquisition"
            )
        mutation_snapshot = registry._snapshot_mutation_files(target)
        try:
            written = registry.append_entries(appended, target, lock_owner=lock_owner)
            _entries_after, report = registry._verify_registry_snapshot(
                target,
                verify_summary=False,
                bootstrap_lock_owner=lock_owner,
            )
            if report["status"] != "REGISTRY_OK":
                raise AttestationError(
                    "registry failed verification after attestation: "
                    + "; ".join(report["problems"])
                )
            summary = {
                "schema": registry.REGISTRY_SCHEMA,
                "status": "REGISTRY_MUTATION_COMPLETE",
                "mutation_type": "official_attestation",
                "mutation_run_id": run_id,
                "plan_id": preflight["plan_id"],
                "plan_hash": preflight["plan_hash"],
                # An attestation writes a summary like any other mutation, so it binds
                # to the registry contract like any other - otherwise the very record
                # that cannot be re-derived would be the one left unverifiable.
                "registry_contract_hash": registry.active_registry_contract_hash(),
                "resolved_paths_hash": preflight["resolved_paths_hash"],
                "mutated_at_utc": observation["received_at_utc"],
                "complete": True,
                registry.ACTIVE_CONTRACTS_FIELD: active_contracts,
                registry.ACTIVE_LIFECYCLE_GENERATIONS_FIELD: active_generations,
                registry.LIFECYCLE_GENERATION_HIGH_WATER_FIELD: lifecycle_high_water,
                registry.LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD: (
                    last_complete_metadata_refresh_received_at
                ),
                registry.RAW_UNIVERSE_ROWS_FIELD: raw_universe_rows,
                **surface_authority_fields,
                "appended_entries": written,
                "registry": report,
            }
            registry._write_summary_with_mutation_receipt(
                target,
                summary,
                lock_owner=lock_owner,
            )
            final_entries, final_report = registry._verify_registry_snapshot(
                target,
                bootstrap_lock_owner=lock_owner,
            )
            if final_report["status"] != "REGISTRY_OK":
                raise AttestationError(
                    "registry transaction failed final verification: "
                    + "; ".join(final_report["problems"])
                )
            official_entry = next(
                (
                    entry
                    for entry in reversed(final_entries)
                    if entry.get("stream_id") == observation["stream_id"]
                ),
                None,
            )
            if official_entry is None:
                raise AttestationError(
                    "official attestation stream head is missing after commit"
                )
        except BaseException:
            registry._rollback_uncommitted_mutation(target, mutation_snapshot)
            raise

    return {
        "schema": ATTESTATION_SCHEMA,
        "status": "ATTESTED" if written else "ALREADY_RECORDED",
        "episode_id": observation["episode_id"],
        "official_spot_t0": observation["timestamp_ts"],
        "precision_sec": int(official_entry.get("t0_precision_sec") or 0),
        "appended_records": written,
        "official_record_hash": official_entry["record_hash"],
        "attestation": official_entry["attestation"],
        "plan_hash": preflight["plan_hash"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record an official spot t0 read from a venue announcement.",
    )
    parser.add_argument("--attest", action="store_true")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--venue", choices=sorted(config.PERP_VENUES),
                        help="the venue trading the pre-market perpetual")
    parser.add_argument("--listing-venue", choices=sorted(OFFICIAL_ANNOUNCEMENT_HOSTS),
                        help="the venue whose announcement schedules the spot listing; "
                             "defaults to --venue when the same exchange does both")
    parser.add_argument("--spot-symbol", default="")
    parser.add_argument("--premarket-contract-id", default="")
    parser.add_argument("--lifecycle-generation", type=int, default=None)
    parser.add_argument("--announced-utc", default="",
                        help="ISO-8601 UTC, e.g. 2026-09-09T04:00:00Z")
    parser.add_argument("--announcement-url", default="")
    parser.add_argument("--quote", default="",
                        help="the sentence in the announcement that states the time")
    parser.add_argument("--quoted-time", default="",
                        help="exact UTC time fragment copied from --quote")
    parser.add_argument("--quoted-symbol", default="",
                        help="exact market-symbol fragment copied from --quote")
    parser.add_argument("--attested-by", default="")
    parser.add_argument("--why", action="store_true",
                        help="explain why an official t0 cannot be fetched")
    args = parser.parse_args(argv)

    if args.why:
        print(json.dumps({
            "measured_on": "2026-08-23",
            "machine_readable_official_spot_t0_available": False,
            "bybit": "announcement startDateTimestamp equals the publication instant; "
                     "no listing announcement had it set in the future",
            "okx": "announcement API exposes pTime only; no listing-time field",
            "gate": "no public announcement API",
            "ambiguity": "one Bybit spot-listing article carried 24 time expressions "
                         "(deposits, trading start, campaign window, withdrawals)",
            "consequence": "the official class is attested by a person, and the record "
                           "carries the sentence they read",
        }, ensure_ascii=False, indent=2))
        return 0

    if not args.attest:
        raise SystemExit("no action requested")
    required = {
        "--run-id": args.run_id,
        "--venue": args.venue,
        "--spot-symbol": args.spot_symbol,
        "--premarket-contract-id": args.premarket_contract_id,
        "--lifecycle-generation": args.lifecycle_generation,
        "--announced-utc": args.announced_utc,
        "--announcement-url": args.announcement_url,
        "--quote": args.quote,
        "--quoted-time": args.quoted_time,
        "--quoted-symbol": args.quoted_symbol,
        "--attested-by": args.attested_by,
    }
    missing = [
        flag
        for flag, value in required.items()
        if value is None or (isinstance(value, str) and not value.strip())
    ]
    if missing:
        raise SystemExit("--attest requires " + ", ".join(missing))

    result = attest(
        run_id=args.run_id,
        venue=args.venue,
        listing_venue=args.listing_venue or args.venue,
        spot_symbol=args.spot_symbol,
        premarket_contract_id=args.premarket_contract_id,
        lifecycle_generation=args.lifecycle_generation,
        announced_utc=args.announced_utc,
        announcement_url=args.announcement_url,
        quoted_sentence=args.quote,
        quoted_time_text=args.quoted_time,
        quoted_symbol_text=args.quoted_symbol,
        attested_by=args.attested_by,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
