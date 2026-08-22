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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import project_config as config
from canonical_hash import canonical_hash


SCHEMA = "premarket_perp_capture_planonly_v1"
PLAN_ID = "premarket_perp_capture_20260822"
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
            "path": str(config.PROJECT_ROOT / relative),
            "repo_path": relative,
            "sha256": _sha256_file(config.PROJECT_ROOT / relative),
        }
        for role, relative in config.BOUND_RUNTIME_FILES
    ]
    plan: dict[str, Any] = {
        "schema": SCHEMA,
        "plan_id": PLAN_ID,
        "project": "ZolotyayLopata-premarket-perp-capture",
        "strategy_branch": "premarket_perpetual_listing_impulse",
        "mode": "PlanOnly",
        "status": "AWAIT_RISK_GATE_GREEN_NO_CAPTURE_YET",
        "generated_at_utc": generated_at_utc,
        "generated_at_project_root": str(config.PROJECT_ROOT),
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
        },
        "write_classes": {
            key: dict(value) for key, value in config.WRITE_CLASSES.items()
        },
        "event_registry": {
            "t0_source_classes": ["OFFICIAL_ANNOUNCEMENT", "VENUE_INSTRUMENT_METADATA"],
            "populated_today": ["VENUE_INSTRUMENT_METADATA"],
            "never_mixed": (
                "a capture set is drawn from one t0 source class only; mixing an "
                "announcement-derived t0 with a metadata-derived one is the defect "
                "the spot monitor's audit found in its listed_ts column"
            ),
            "revisions": (
                "venues move launch times; a change is appended as a new revision "
                "carrying the previous value, never written over the old one"
            ),
            "venue_t0_semantics": {
                adapter_venue: semantics
                for adapter_venue, semantics in (
                    ("bybit", "launchTime: venue-declared contract launch time"),
                    ("okx", "listTime: venue-declared instrument listing time"),
                    ("gate", "create_time: contract creation, not necessarily trading start"),
                )
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
        # The cadence is a bound, not a tuning knob: it sets both the request volume
        # and the time resolution of the only data the hypothesis will ever be tested
        # on. Loosening it silently would change what a capture means while leaving
        # every file name and status the same.
        "sampling": {
            "method": "rest_polling",
            "is_continuous_tape": False,
            "background_cadence_sec": dict(config.PROBE_CADENCE_SEC),
            "burst_cadence_sec": dict(config.BURST_CADENCE_SEC),
            "burst_half_width_sec": config.BURST_HALF_WIDTH_SEC,
            "orderbook_depth": config.ORDERBOOK_DEPTH,
            "probes": ["trades", "orderbook", "ticker"],
            "achieved_cadence_is_measured_and_published": True,
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
            "run one visible bounded public-data capture around a single event",
            "write per-capture manifests and evidence receipts",
            "replay captured data offline",
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
    without_hash = {k: v for k, v in plan.items() if k != "plan_hash"}
    require(plan.get("plan_hash") == canonical_hash(without_hash), "plan hash mismatch")


def write_plan(generated_at_utc: str) -> Path:
    plan = build_plan(generated_at_utc)
    content = json.dumps(plan, indent=2, ensure_ascii=False) + "\n"
    path = config.PLAN_PATH
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise PlanBuildError(
                f"immutable artifact mismatch: {path}. Reissuing means removing it "
                f"deliberately and recording the supersession in docs/decisions/."
            )
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
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
