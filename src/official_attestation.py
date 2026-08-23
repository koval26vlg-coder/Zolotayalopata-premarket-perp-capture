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
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import event_registry as registry
import project_config as config
import risk_gate


ATTESTATION_SCHEMA = "premarket_perp_official_attestation_v1"

# Hosts whose pages count as an official announcement from the venue itself. This is a
# provenance list, not a network allow-list - nothing here is ever fetched - but it is
# the same idea: an "official" t0 sourced from a forum post or an aggregator is a
# different datum wearing the same word.
OFFICIAL_ANNOUNCEMENT_HOSTS: dict[str, tuple[str, ...]] = {
    "bybit": ("announcements.bybit.com", "www.bybit.com"),
    "okx": ("www.okx.com",),
    "gate": ("www.gate.com", "www.gate.io", "gate.io"),
}

# An announcement says "10:00AM UTC", not "10:00:00.000". Recording second precision
# would claim an accuracy the source never offered.
ANNOUNCED_PRECISION_SEC = 60

# A capture opens its window before t0, so an anchor must still be far enough ahead to
# be usable. Attesting a moment that has already passed cannot anchor a live capture,
# and silently accepting one invites a capture that can never cover its own window.
MIN_LEAD_SEC = config.CAPTURE_WINDOW_BEFORE_SEC


class AttestationError(RuntimeError):
    pass


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


def require_official_source(venue: str, url: str) -> str:
    hosts = OFFICIAL_ANNOUNCEMENT_HOSTS.get(venue)
    if not hosts:
        raise AttestationError(f"no official announcement host declared for {venue}")
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.lower() != "https":
        raise AttestationError("the announcement URL must be https")
    host = (parsed.hostname or "").lower()
    if host not in hosts:
        raise AttestationError(
            f"{host or url} is not an official announcement host for {venue}; "
            f"expected one of {', '.join(hosts)}"
        )
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), host, parsed.path, parsed.query, "")
    )


def require_quotation(quote: str, announced_utc: str) -> str:
    """The sentence must be present, substantial, and mention a time.

    Not a proof that the right line was chosen - nothing in software can be - but it
    forces the attestation to carry the evidence a reviewer needs to disagree."""
    text = " ".join(quote.split())
    if len(text) < 24:
        raise AttestationError(
            "the quoted announcement sentence is required and must be the real sentence"
        )
    if not re.search(r"\d", text):
        raise AttestationError("the quoted sentence carries no time at all")
    return text


def build_attestation(
    *,
    venue: str,
    spot_symbol: str,
    premarket_contract_id: str,
    announced_utc: str,
    announcement_url: str,
    quoted_sentence: str,
    attested_by: str,
    now_ts: int,
) -> dict[str, Any]:
    if venue not in OFFICIAL_ANNOUNCEMENT_HOSTS:
        raise AttestationError(f"unknown venue: {venue}")
    if not str(attested_by).strip():
        raise AttestationError("attested_by is required: an attestation has an author")
    if not str(spot_symbol).strip():
        raise AttestationError("spot_symbol is required for an official spot t0")

    t0_ts = parse_announced_utc(announced_utc)
    url = require_official_source(venue, announcement_url)
    quote = require_quotation(quoted_sentence, announced_utc)

    lead = t0_ts - now_ts
    if lead < MIN_LEAD_SEC:
        raise AttestationError(
            f"the announced t0 is {lead}s away; a capture opens its window "
            f"{MIN_LEAD_SEC}s before t0 and could not cover it"
        )

    episode_id = registry.event_id(venue, premarket_contract_id)
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
        received_at_utc=registry.utc_now_iso(),
        precision_sec=ANNOUNCED_PRECISION_SEC,
        caveats=("OFFICIAL_T0_READ_BY_A_PERSON_FROM_ANNOUNCEMENT_PROSE",),
    )
    # The evidence rides with the record, not in a commit message someone has to find.
    observation["attestation"] = {
        "schema": ATTESTATION_SCHEMA,
        "attested_by": attested_by,
        "announced_utc": announced_utc.strip(),
        "quoted_sentence": quote,
        "announcement_url": url,
        "lead_sec_at_attestation": lead,
    }
    return observation


def attest(
    *,
    run_id: str,
    venue: str,
    spot_symbol: str,
    premarket_contract_id: str,
    announced_utc: str,
    announcement_url: str,
    quoted_sentence: str,
    attested_by: str,
    path: Path | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Preflight, lock, append. Recording an official t0 is a registry write."""
    import time

    if not str(run_id).strip():
        raise AttestationError("run_id is required")
    try:
        preflight = risk_gate.preflight(write_class="metadata_registry", run_id=run_id)
    except Exception as exc:  # noqa: BLE001
        raise AttestationError(f"PREFLIGHT_BLOCKED: {type(exc).__name__}: {exc}") from exc
    if not (
        preflight.get("ok") is True
        and preflight.get("verified") is True
        and preflight.get("decision") == "ALLOW_METADATA_REGISTRY"
        and preflight.get("run_id") == run_id
        and len(str(preflight.get("plan_hash") or "")) == 64
    ):
        raise AttestationError("PREFLIGHT_BLOCKED: metadata preflight is not verified")

    observation = build_attestation(
        venue=venue,
        spot_symbol=spot_symbol,
        premarket_contract_id=premarket_contract_id,
        announced_utc=announced_utc,
        announcement_url=announcement_url,
        quoted_sentence=quoted_sentence,
        attested_by=attested_by,
        now_ts=int(now_ts if now_ts is not None else time.time()),
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
        existing = registry.load_registry(target)
        appended = registry.merge_observations(existing, [observation])
        written = registry.append_entries(appended, target, lock_owner=lock_owner)

    return {
        "schema": ATTESTATION_SCHEMA,
        "status": "ATTESTED" if written else "ALREADY_RECORDED",
        "episode_id": observation["episode_id"],
        "official_spot_t0": observation["timestamp_ts"],
        "precision_sec": ANNOUNCED_PRECISION_SEC,
        "appended_records": written,
        "attestation": observation["attestation"],
        "plan_hash": preflight["plan_hash"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record an official spot t0 read from a venue announcement.",
    )
    parser.add_argument("--attest", action="store_true")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--venue", choices=sorted(OFFICIAL_ANNOUNCEMENT_HOSTS))
    parser.add_argument("--spot-symbol", default="")
    parser.add_argument("--premarket-contract-id", default="")
    parser.add_argument("--announced-utc", default="",
                        help="ISO-8601 UTC, e.g. 2026-09-09T04:00:00Z")
    parser.add_argument("--announcement-url", default="")
    parser.add_argument("--quote", default="",
                        help="the sentence in the announcement that states the time")
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
        "--announced-utc": args.announced_utc,
        "--announcement-url": args.announcement_url,
        "--quote": args.quote,
        "--attested-by": args.attested_by,
    }
    missing = [flag for flag, value in required.items() if not str(value or "").strip()]
    if missing:
        raise SystemExit("--attest requires " + ", ".join(missing))

    result = attest(
        run_id=args.run_id,
        venue=args.venue,
        spot_symbol=args.spot_symbol,
        premarket_contract_id=args.premarket_contract_id,
        announced_utc=args.announced_utc,
        announcement_url=args.announcement_url,
        quoted_sentence=args.quote,
        attested_by=args.attested_by,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
