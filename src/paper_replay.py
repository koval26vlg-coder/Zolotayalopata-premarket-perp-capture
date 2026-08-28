"""Offline paper simulation over sealed public capture evidence only."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import project_config as config
from canonical_hash import canonical_hash


SCHEMA_VERSION = "premarket_perp_paper_replay_v1"

FIXED_MODEL: dict[str, Any] = dict(config.OFFLINE_PAPER_MODEL)


def canonical_result_hash(material: Mapping[str, Any]) -> str:
    """Return the deterministic content hash of a paper result."""

    return canonical_hash(material)


def _result(material: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(material)
    result["result_hash"] = canonical_result_hash(result)
    return result


def _registry_summary(candidate_report: Mapping[str, Any]) -> dict[str, Any]:
    registry = candidate_report.get("registry")
    if not isinstance(registry, Mapping):
        return {
            "status": "REGISTRY_REPORT_MISSING",
            "registry_sha256": None,
            "entries": None,
        }
    return {
        "status": registry.get("status"),
        "registry_sha256": registry.get("registry_sha256"),
        "entries": registry.get("entries"),
    }


def _candidate_report_is_valid(candidate_report: object) -> bool:
    if not isinstance(candidate_report, Mapping):
        return False
    status = candidate_report.get("status")
    registry = candidate_report.get("registry")
    candidates = candidate_report.get("candidates")
    rejections = candidate_report.get("rejections")
    if (
        not isinstance(status, str)
        or not status.strip()
        or candidate_report.get("capture_authorized") is not False
        or not isinstance(registry, Mapping)
        or registry.get("status") != "REGISTRY_OK"
        or not isinstance(registry.get("registry_sha256"), str)
        or len(str(registry.get("registry_sha256"))) != 64
        or not isinstance(registry.get("entries"), int)
        or isinstance(registry.get("entries"), bool)
        or int(registry.get("entries")) < 0
        or not isinstance(candidates, list)
        or not isinstance(rejections, list)
    ):
        return False
    if status == "NO_SECONDS_GRADE_CANDIDATE" and candidates:
        return False
    if status not in {
        "NO_SECONDS_GRADE_CANDIDATE",
        "SECONDS_GRADE_CANDIDATE_FOUND_NO_CAPTURE_AUTHORITY",
        "CANDIDATE_READY",
    }:
        return False
    if status != "NO_SECONDS_GRADE_CANDIDATE" and not candidates:
        return False
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            return False
        if (
            candidate.get("asset_class") != "CRYPTO_TOKEN"
            or candidate.get("t0_source_class") != "OFFICIAL_ANNOUNCEMENT"
            or candidate.get("t0_precision_sec") != 1
            or not str(candidate.get("episode_id") or "").strip()
            or not str(candidate.get("venue") or "").strip()
        ):
            return False
    return True


def paper_tick_from_candidate_report(
    candidate_report: object,
    *,
    output_dir: Path,
    sealed_capture_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Evaluate whether an offline paper replay may run.

    The v30 boundary is deliberately fail-closed.  It can report that no
    eligible event or sealed capture exists, but it cannot create a virtual
    position from metadata alone.  ``output_dir`` is accepted for the later
    sealed-replay stage and is intentionally untouched by these no-run paths.
    """

    del output_dir  # No filesystem mutation is allowed on a no-run tick.

    valid = _candidate_report_is_valid(candidate_report)
    report = candidate_report if isinstance(candidate_report, Mapping) else {}
    candidates = report.get("candidates", []) if valid else []
    registry = _registry_summary(report)
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_candidate_status": report.get("status"),
        "registry": registry,
        "fixed_model": copy.deepcopy(FIXED_MODEL),
        "virtual_positions_created": 0,
        "capture_started": False,
        "acceptance_capable": False,
        "paper_broker_execution": False,
        "cost_model_ready": False,
        "net_pnl_usdt": None,
    }

    if not valid:
        base["status"] = "PAPER_NOT_RUN_INVALID_CANDIDATE_REPORT"
        return _result(base)

    if not candidates:
        base["status"] = "NO_ELIGIBLE_EVENT"
        return _result(base)

    if not sealed_capture_ids:
        base["status"] = "PAPER_NOT_RUN_NO_CAPTURE_EVIDENCE"
        return _result(base)

    base["status"] = "PAPER_NOT_RUN_COST_MODEL_MISSING"
    return _result(base)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed offline paper simulation readiness check."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--candidate-report", type=Path)
    source.add_argument("--candidate-stdin", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("unused-paper-output"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        raw = (
            args.candidate_report.read_text(encoding="utf-8")
            if args.candidate_report is not None
            else sys.stdin.read()
        )
        candidate_report = json.loads(raw)
    except (OSError, ValueError) as exc:
        candidate_report = {
            "status": "INVALID_INPUT",
            "capture_authorized": False,
            "registry": {},
            "candidates": [],
            "rejections": [f"{type(exc).__name__}: invalid candidate report"],
        }

    result = paper_tick_from_candidate_report(
        candidate_report,
        output_dir=args.output_dir,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0 if result["status"] in {
        "NO_ELIGIBLE_EVENT",
        "PAPER_NOT_RUN_NO_CAPTURE_EVIDENCE",
        "PAPER_NOT_RUN_COST_MODEL_MISSING",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
