"""Generates the immutable PlanOnly for this project.

The plan records what the project may reach, what it may do, and the SHA-256 of every
runtime file that implements those limits. Reissuing it is the deliberate act that
accompanies any runtime change - the risk gate refuses to run against a plan that no
longer describes the files on disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import project_config as config
from canonical_hash import canonical_hash


SCHEMA = "premarket_perp_capture_planonly_v5"
PLAN_ID = "premarket_perp_capture_20260822_v5"
SUPERSEDES_PLAN_ID = "premarket_perp_capture_20260822_v4"
SUPERSEDES_PLAN_HASH = "fae208baf126163e2041fccffe4c1b656848a80647b1a15b0bc0af5901dd3314"
SUPERSEDES_PLAN_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v4.json"
HASH_METHOD = "sha256_canonical_json_excluding_plan_hash"


class PlanBuildError(ValueError):
    pass


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        raise PlanBuildError(f"bound file missing: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_plan(generated_at_utc: str) -> dict[str, Any]:
    files = [
        {
            "role": role,
            "repo_path": relative,
            "sha256": _sha256_file(config.PROJECT_ROOT / relative),
        }
        for role, relative in config.BOUND_RUNTIME_FILES
    ]
    plan: dict[str, Any] = {
        "schema": SCHEMA,
        "plan_id": PLAN_ID,
        "supersedes_plan_id": SUPERSEDES_PLAN_ID,
        "supersedes_plan_hash": SUPERSEDES_PLAN_HASH,
        "supersedes_plan_path": SUPERSEDES_PLAN_PATH,
        "project": "ZolotyayLopata-premarket-perp-capture",
        "strategy_branch": "premarket_perpetual_listing_impulse",
        "mode": "PlanOnly",
        "status": "AWAIT_CAPTURE_IMPLEMENTATION_AUDIT_NO_CAPTURE",
        "generated_at_utc": generated_at_utc,
        "implementation_path_semantics": "repo_path values are relative to the runtime Git root",
        "objective": (
            "Capture public pre-market and early perpetual market data around a "
            "listing event t0 on Bybit, OKX and Gate, densely enough to replay the "
            "hypothesis 'enter before the listing, exit at t0/+5/+15/+60s' offline. "
            "Capture only: this plan authorises no execution of any kind, and the "
            "replay is a simulation over data already on disk."
        ),
        "hypothesis_under_study": (
            "LONG before the listing, exit at t0, +5s, +15s or +60s - stated here so "
            "the capture design can be judged against it, not so it can be traded"
        ),
        # What the project does, as opposed to the instrument class it observes. The
        # distinction is the entire reason this repository is separate: the spot
        # monitor forbids leverage because it never goes near it, while this one looks
        # at a leveraged market and must still never take leverage.
        "risk_contract": dict(config.RISK_CONTRACT),
        "allowed_endpoints": [list(item) for item in config.ALLOWED_ENDPOINTS],
        "resolved_path_bindings": {
            "shared_gate_path": str(config.SHARED_GATE_PATH.resolve(strict=False)),
            "shared_writer_claim_path": str(
                config.SHARED_WRITER_CLAIM_PATH.resolve(strict=False)
            ),
            "capture_root": str(config.CAPTURE_ROOT.resolve(strict=False)),
        },
        "enforcement": {
            "capability_scan": (
                "src/ and tools/ are scanned against docs/risk/forbidden-capabilities.txt "
                "and against allowed_endpoints; any order, signing, credential, "
                "leverage-changing or off-list URL marker blocks the gate"
            ),
            "plan_bindings": (
                "every bound file's SHA-256 is verified against this plan, and this "
                "plan against the external trust root src/frozen_plan_bindings.py, "
                "which sits outside the plan-binds-runtime cycle on purpose"
            ),
            "shared_single_writer": (
                "the workspace-wide active-run gate must be open and the shared "
                "market-data writer claim unheld; a stale claim is reported, never "
                "cleared automatically"
            ),
            "capture_token": (
                "a capture requires a one-shot token minted only by a passing "
                "--preflight; there is no honour-system flag"
            ),
            "runtime_http_allow_list": (
                "GET requests require HTTPS, an exact declared host/path and declared "
                "query keys; userinfo, redirects, dot/encoded paths, non-public DNS "
                "answers and caller-supplied allow-lists fail closed; TCP connects to "
                "the already validated IP while TLS SNI and Host retain the venue name"
            ),
            "resolved_path_bindings": (
                "shared gate, shared writer claim and capture root are compared to "
                "their canonical absolute values before either write class is allowed"
            ),
        },
        "write_classes": {
            key: dict(value) for key, value in config.WRITE_CLASSES.items()
        },
        "event_registry": {
            "schema": "premarket_perp_event_registry_v2",
            "path": "docs/registry/listing-events-v2.jsonl",
            "legacy_v1_path": "docs/registry/listing-events.jsonl",
            "timestamp_kinds": [
                "premarket_contract_launch_ts",
                "official_spot_t0",
                "first_trade_ts",
                "transition_ts",
                "contract_created_ts",
            ],
            "source_classes": [
                "OFFICIAL_ANNOUNCEMENT",
                "VENUE_INSTRUMENT_METADATA",
                "OBSERVED_PUBLIC_TRADE",
                "OBSERVED_LIFECYCLE",
            ],
            "acceptance_anchor": {
                "timestamp_kind": "official_spot_t0",
                "source_class": "OFFICIAL_ANNOUNCEMENT",
            },
            "proxy_policy": (
                "venue metadata, first-trade observations and observed lifecycle "
                "timestamps are DESCRIPTIVE_ONLY and cannot enter capture selection"
            ),
            "episode_identity": (
                "sha256(venue,native_premarket_contract_id,lifecycle_generation); "
                "schedule revisions do not change episode identity"
            ),
            "stream_identity": (
                "sha256(episode_id,timestamp_kind,instrument_role,source_class,source_identity)"
            ),
            "lineage": (
                "strict global record_seq/previous_record_hash chain plus independent "
                "stream_revision/supersedes_record_hash chains; forks and orphan "
                "revisions fail closed; a nonempty production registry requires its "
                "tail-anchoring summary receipt before capture selection"
            ),
            "locking": (
                "metadata refresh uses an atomic O_EXCL registry lock from load and "
                "lineage verification through append, fsync and summary receipt"
            ),
            "venue_metadata_semantics": {
                "bybit": (
                    "status=PreLaunch,isPreListing=true LinearPerpetual launchTime; "
                    "descriptive contract launch only"
                ),
                "okx": (
                    "instType=FUTURES,ruleType=pre_market listTime; descriptive "
                    "contract launch only"
                ),
                "gate": (
                    "status=prelaunch create_time; contract_created_ts only, not "
                    "trading start"
                ),
            },
        },
        "capture_bounds": {
            "window_before_t0_sec": config.CAPTURE_WINDOW_BEFORE_SEC,
            "window_after_t0_sec": config.CAPTURE_WINDOW_AFTER_SEC,
            "max_runtime_sec": config.MAX_CAPTURE_RUNTIME_SEC,
            "max_requests_per_capture": config.MAX_REQUESTS_PER_CAPTURE,
            "max_events_per_capture": config.MAX_EVENTS_PER_CAPTURE,
            "capture_root": str(config.CAPTURE_ROOT),
            "one_capture_at_a_time": True,
            "visible_terminal_required": True,
        },
        "implementation": {"files": files},
        "forbidden": [
            "orders of any kind, on any venue, in any mode",
            "paper or live execution",
            "private API surfaces, credentials, request signing",
            "taking leverage or margin, or changing either",
            "withdrawals or transfers",
            "acceptance or rejection decisions from captured data",
            "a second concurrent market-data writer in this workspace",
            "background or hidden capture runs",
        ],
        "authorized_after_gate_green": [
            "refresh the public metadata event registry after metadata preflight",
            "verify and materialize descriptive proxy observations offline",
        ],
        "acceptance_policy": {
            "evidence_class": "PUBLIC_PREMARKET_PERP_CAPTURE",
            "acceptance_decision": "NONE_CAPTURE_ONLY",
            "note": (
                "no metric computed from this capture supports ACCEPT or REJECT of "
                "any strategy; a separate user-checkpointed plan is required for that"
            ),
        },
        "plan_hash_method": HASH_METHOD,
    }
    plan["plan_hash"] = canonical_hash(plan)
    validate_plan(plan)
    return plan


def validate_plan(plan: dict[str, Any]) -> None:
    def require(value: bool, message: str) -> None:
        if not value:
            raise PlanBuildError(message)

    require(plan.get("schema") == SCHEMA, "schema mismatch")
    require(plan.get("plan_id") == PLAN_ID, "plan id mismatch")
    require(plan.get("mode") == "PlanOnly", "mode mismatch")
    require(plan.get("supersedes_plan_id") == SUPERSEDES_PLAN_ID, "supersedes id mismatch")
    require(
        plan.get("supersedes_plan_hash") == SUPERSEDES_PLAN_HASH,
        "supersedes hash mismatch",
    )
    require(
        plan.get("status") == "AWAIT_CAPTURE_IMPLEMENTATION_AUDIT_NO_CAPTURE",
        "v3 must remain capture-disabled",
    )
    contract = plan.get("risk_contract") or {}
    for key in (
        "private_api", "api_keys", "request_signing", "orders", "paper_execution",  # risk-scan: allow api_key
        "live_execution", "uses_leverage", "uses_margin", "real_capital",
        "withdrawals_or_transfers",
    ):
        require(contract.get(key) is False, f"risk contract must forbid {key}")
    require(contract.get("research_only") is True, "risk contract must be research-only")
    require(contract.get("public_data_only") is True, "risk contract must be public-data-only")
    require(
        plan.get("acceptance_policy", {}).get("acceptance_decision") == "NONE_CAPTURE_ONLY",
        "plan must not carry an acceptance decision",
    )
    require(bool(plan.get("allowed_endpoints")), "plan must declare its endpoint allow-list")
    require(
        set((plan.get("resolved_path_bindings") or {}))
        == {"shared_gate_path", "shared_writer_claim_path", "capture_root"},
        "resolved path bindings are incomplete",
    )
    without_hash = {k: v for k, v in plan.items() if k != "plan_hash"}
    require(plan.get("plan_hash") == canonical_hash(without_hash), "plan hash mismatch")


def write_plan(generated_at_utc: str) -> Path:
    plan = build_plan(generated_at_utc)
    content = json.dumps(plan, indent=2, ensure_ascii=False) + "\n"
    path = config.PLAN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o444)
    except FileExistsError as exc:
        if path.read_text(encoding="utf-8") == content:
            return path
        raise PlanBuildError(
            f"immutable artifact mismatch: {path}. Issue a new versioned PlanOnly "
            f"path and supersede this identity; never remove or overwrite it."
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-plan", action="store_true")
    args = parser.parse_args(argv)
    if not args.write_plan:
        raise SystemExit("no action requested")
    generated = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    path = write_plan(generated)
    plan = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps({
        "status": "PLAN_WRITTEN",
        "path": str(path),
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "plan_file_sha256": _sha256_file(path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
