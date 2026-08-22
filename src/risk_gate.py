"""May a bounded metadata write or a capture start with the approved runtime?

The spot monitor's audit ended with one lesson worth carrying over verbatim: a rule
that lives only in a document is not a rule. Everything below either verifies
something mechanically or reports that it could not.

Plan identity, capability scan, and the shared active-run gate are mandatory for every
declared write class.  Sustained market-data capture additionally requires the global
writer claim to be free, the prior capture record to be terminal, and a one-shot token.
The short metadata registry refresh intentionally does not take those capture-only
controls, so it cannot claim capture authority by accident or block an active capture.

The common checks answer four questions, and any of them failing blocks:

* does the checked-out runtime match the immutable PlanOnly, and is that plan the one
  the external trust root approves;
* can the runtime still only do what the risk contract permits (capability scan);
* is the shared active-run gate open;
* are the resolved control paths recorded in the verification receipt.

Only a passing ``market_data_capture`` preflight mints a token. A capture that starts
without one has not been through the stricter capture path.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import frozen_plan_bindings as trust_root
import project_config as config
from canonical_hash import canonical_hash
from capability_scan import assert_runtime_is_clean


CAPTURE_TOKEN_SCHEMA = "premarket_capture_token_v1"
PREFLIGHT_RESULT_SCHEMA = "premarket_write_preflight_v2"
GATE_OPEN_STATUSES = frozenset({"READY_FOR_POSTPROCESS"})
RUN_RECORD_ACTIVE_STATUSES = frozenset({"LAUNCHING", "RUNNING"})
LAUNCH_GRACE_SEC = 120
CAPTURE_TOKEN_TTL_SEC = 900
GATE_TIMEOUT_SEC = 120

METADATA_REGISTRY_ACTION = (
    "refresh the public metadata event registry after metadata preflight"
)
OFFLINE_DESCRIPTIVE_ACTION = (
    "verify and materialize descriptive proxy observations offline"
)
PLAN_WRITE_AUTHORIZATION: dict[str, dict[str, frozenset[str]]] = {
    "AWAIT_CAPTURE_IMPLEMENTATION_AUDIT_NO_CAPTURE": {
        "authorized_actions": frozenset({
            METADATA_REGISTRY_ACTION,
            OFFLINE_DESCRIPTIVE_ACTION,
        }),
        "write_classes": frozenset({"metadata_registry"}),
    },
}


class RiskGateError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(value: bool, message: str) -> None:
    if not value:
        raise RiskGateError(message)


def resolved_path_bindings() -> dict[str, str]:
    return {
        "shared_gate_path": str(config.SHARED_GATE_PATH.resolve(strict=False)),
        "shared_writer_claim_path": str(
            config.SHARED_WRITER_CLAIM_PATH.resolve(strict=False)
        ),
        "capture_root": str(config.CAPTURE_ROOT.resolve(strict=False)),
    }


def verify_resolved_path_bindings(plan: Mapping[str, Any]) -> dict[str, str]:
    expected = plan.get("resolved_path_bindings")
    actual = resolved_path_bindings()
    _require(isinstance(expected, dict), "plan carries no resolved path bindings")
    _require(expected == actual, "runtime resolved path bindings differ from the plan")
    return actual


def verify_plan_write_authorization(
    plan: Mapping[str, Any], write_class: str
) -> dict[str, Any]:
    """Bind a write preflight to the plan's exact status/action matrix.

    Adding a new status, action, or write class must update this PlanOnly-bound
    runtime and therefore requires a newly issued immutable plan. Unknown values
    never inherit authority from a nearby human-readable phrase.
    """
    status = str(plan.get("status") or "").strip()
    authorization = PLAN_WRITE_AUTHORIZATION.get(status)
    _require(authorization is not None, f"unknown or unauthorized plan status: {status!r}")

    actions = plan.get("authorized_after_gate_green")
    _require(
        isinstance(actions, list)
        and all(isinstance(action, str) and action.strip() for action in actions),
        "plan authorized actions are missing or malformed",
    )
    actual_actions = frozenset(actions)
    _require(
        len(actual_actions) == len(actions),
        "plan authorized actions contain duplicates",
    )
    _require(
        actual_actions == authorization["authorized_actions"],
        "plan authorized actions are unknown, missing, or inconsistent with its status",
    )
    _require(
        write_class in authorization["write_classes"],
        f"plan status {status!r} does not authorize write class {write_class!r}",
    )
    return {
        "status": status,
        "authorized_actions": sorted(actual_actions),
        "write_class": write_class,
    }


# ------------------------------------------------------------------ plan identity


def load_and_verify_plan(plan_path: Path | None = None) -> dict[str, Any]:
    """The plan must be internally consistent, approved by the trust root, and an
    accurate description of the files on disk.

    The trust root is a separate small module precisely because the plan pins the
    runtime and the runtime verifies the plan: without an outside anchor, editing both
    together closes the loop and proves nothing."""
    plan_path = plan_path or config.PLAN_PATH
    _require(plan_path.is_file(), f"PlanOnly missing: {plan_path}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    _require(plan.get("schema") == trust_root.PLAN_SCHEMA, "plan schema mismatch")
    _require(plan.get("plan_id") == trust_root.PLAN_ID, "plan id mismatch")
    without_hash = {k: v for k, v in plan.items() if k != "plan_hash"}
    _require(plan.get("plan_hash") == canonical_hash(without_hash), "plan hash mismatch")
    _require(plan.get("plan_hash") == trust_root.PLAN_HASH, "plan hash is not the approved one")
    _require(
        sha256_file(plan_path) == trust_root.PLAN_FILE_SHA256,
        "plan file sha256 is not the approved one",
    )

    declared = {
        str(item.get("role")): str(item.get("sha256") or "")
        for item in (plan.get("implementation") or {}).get("files") or []
    }
    for role, relative in config.BOUND_RUNTIME_FILES:
        expected = declared.get(role, "")
        _require(expected != "", f"plan does not bind: {role}")
        path = config.PROJECT_ROOT / relative
        _require(path.is_file(), f"bound file missing: {relative}")
        actual = sha256_file(path)
        _require(
            actual == expected,
            f"file sha256 mismatch for {role} ({relative}): plan {expected}, file {actual}",
        )
    unknown = sorted(set(declared) - {role for role, _ in config.BOUND_RUNTIME_FILES})
    _require(not unknown, f"plan binds unknown roles: {', '.join(unknown)}")

    # The plan is also the authority on what may be contacted and what the project is
    # allowed to do. If config drifted from it, reach or capability widened without
    # review, which is the whole thing this gate exists to prevent.
    _require(
        plan.get("risk_contract") == dict(config.RISK_CONTRACT),
        "runtime risk contract differs from the plan",
    )
    _require(
        [list(item) for item in plan.get("allowed_endpoints") or []]
        == [list(item) for item in config.ALLOWED_ENDPOINTS],
        "runtime endpoint allow-list differs from the plan",
    )
    verify_resolved_path_bindings(plan)
    return plan


def run_capability_scan() -> dict[str, Any]:
    return assert_runtime_is_clean(
        config.PROJECT_ROOT,
        markers_path=config.PROJECT_ROOT / "docs/risk/forbidden-capabilities.txt",
        allowed_endpoints=config.ALLOWED_ENDPOINTS,
    )


# --------------------------------------------------------------- shared workspace


def process_is_alive(pid: Any) -> bool | None:
    """True / False / None when the platform cannot answer.

    Never os.kill on Windows for this: os.kill there does not implement signal 0 and
    would terminate the process being asked about."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return kernel32.GetLastError() == 5  # access denied means it exists
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - liveness must never crash a preflight
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def read_shared_gate(gate_path: Path | None = None) -> dict[str, Any]:
    """Open only on an explicit allow-listed status. Every other outcome is closed."""
    gate_path = gate_path or config.SHARED_GATE_PATH
    if not gate_path.is_file():
        return {"open": False, "status": "UNAVAILABLE", "detail": f"gate missing: {gate_path}"}
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        return {"open": False, "status": "UNAVAILABLE", "detail": "no pwsh/powershell on PATH"}
    try:
        completed = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(gate_path), "-Json"],
            capture_output=True, text=True, timeout=GATE_TIMEOUT_SEC,
        )
        payload = json.loads((completed.stdout or "").strip())
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"open": False, "status": "UNAVAILABLE", "detail": f"{type(exc).__name__}: {exc}"}
    if not isinstance(payload, dict):
        return {"open": False, "status": "UNAVAILABLE", "detail": "gate payload is not an object"}
    status = str(payload.get("status") or payload.get("gate_status") or "").strip()
    if not status:
        return {"open": False, "status": "UNAVAILABLE", "detail": "gate payload carries no status"}
    return {
        "open": status in GATE_OPEN_STATUSES,
        "status": status,
        "detail": None if status in GATE_OPEN_STATUSES else f"shared active-run gate: {status}",
    }


def inspect_claim(claim_path: Path | None = None) -> dict[str, Any]:
    """A stale claim is reported, never cleared: silently reclaiming a single-writer
    lock is exactly the accident it exists to prevent."""
    claim_path = claim_path or config.SHARED_WRITER_CLAIM_PATH
    if not claim_path.is_file():
        return {"present": False, "blocks": False, "stale": False}
    try:
        claim = json.loads(claim_path.read_text(encoding="utf-8-sig"))
        if not isinstance(claim, dict):
            raise ValueError("claim is not an object")
    except (OSError, ValueError) as exc:
        return {"present": True, "blocks": True, "stale": False,
                "detail": f"claim unreadable: {type(exc).__name__}: {exc}"}
    owner_host = claim.get("owner_host")
    same_host = owner_host in (None, "", socket.gethostname())
    stale = (process_is_alive(claim.get("owner_pid")) is False) if same_host else False
    return {
        "present": True, "blocks": True, "stale": stale,
        "run_id": claim.get("run_id"), "owner_pid": claim.get("owner_pid"),
        "detail": (
            f"shared writer claim is stale: owner pid {claim.get('owner_pid')} is gone"
            if stale else
            f"shared writer claim held by run_id={claim.get('run_id')}"
        ),
    }


def _within_launch_grace(record: Mapping[str, Any]) -> bool:
    if str(record.get("status") or "") != "LAUNCHING":
        return False
    try:
        moment = datetime.fromisoformat(str(record.get("started_at_utc") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return 0 <= (datetime.now(timezone.utc) - moment).total_seconds() < LAUNCH_GRACE_SEC


def inspect_run_record(run_record_path: Path | None = None) -> dict[str, Any]:
    run_record_path = run_record_path or config.RUN_RECORD_PATH
    if not run_record_path.is_file():
        return {"present": False, "blocks": False, "stale": False}
    try:
        record = json.loads(run_record_path.read_text(encoding="utf-8-sig"))
        if not isinstance(record, dict):
            raise ValueError("run record is not an object")
    except (OSError, ValueError):
        return {"present": True, "blocks": True, "stale": False, "status": "UNREADABLE"}
    status = str(record.get("status") or "")
    if status not in RUN_RECORD_ACTIVE_STATUSES:
        return {"present": True, "blocks": False, "stale": False, "status": status}
    pid = record.get("worker_pid") or record.get("terminal_pid")
    stale = process_is_alive(pid) is False and not _within_launch_grace(record)
    return {
        "present": True, "blocks": True, "stale": stale, "status": status,
        "run_id": record.get("run_id"),
        "detail": f"own capture {record.get('run_id')} is {status}",
    }


# ------------------------------------------------------------------ capture token


def mint_capture_token(run_id: str, *, ttl_sec: int = CAPTURE_TOKEN_TTL_SEC) -> dict[str, Any]:
    payload = {
        "schema": CAPTURE_TOKEN_SCHEMA,
        "token": secrets.token_hex(16),
        "run_id": run_id,
        "minted_at_utc": utc_now_iso(),
        "minted_by_pid": os.getpid(),
        "expires_at_ts": int(time.time()) + int(ttl_sec),
    }
    path = config.CAPTURE_TOKEN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return payload


def consume_capture_token(*, token: str, run_id: str) -> dict[str, Any]:
    """Taken exactly once: the rename is the claim, so a second caller finds nothing."""
    path = config.CAPTURE_TOKEN_PATH
    if not path.is_file():
        raise RiskGateError("no capture token: run --preflight first")
    consumed = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.consumed")
    try:
        os.replace(path, consumed)
    except OSError as exc:
        raise RiskGateError(f"capture token could not be taken: {exc}") from exc
    try:
        payload = json.loads(consumed.read_text(encoding="utf-8"))
    finally:
        consumed.unlink(missing_ok=True)
    _require(payload.get("schema") == CAPTURE_TOKEN_SCHEMA, "capture token schema mismatch")
    _require(secrets.compare_digest(str(payload.get("token") or ""), str(token)),
             "capture token mismatch")
    _require(str(payload.get("run_id") or "") == str(run_id),
             "capture token belongs to a different run_id")
    _require(int(payload.get("expires_at_ts") or 0) >= int(time.time()),
             "capture token expired: run --preflight again")
    return payload


# --------------------------------------------------------------------- preflight


def evaluate_risk_preflight(
    *,
    plan_error: str | None,
    capability_error: str | None,
    gate: Mapping[str, Any],
    claim: Mapping[str, Any],
    run_record: Mapping[str, Any],
) -> dict[str, Any]:
    """Pure decision step, so the whole matrix is testable without a shell."""
    blockers: list[dict[str, Any]] = []
    if plan_error:
        blockers.append({"source": "plan", "detail": plan_error})
    if capability_error:
        blockers.append({"source": "capability_scan", "detail": capability_error})
    if not gate.get("open"):
        blockers.append({"source": "shared_gate",
                         "detail": gate.get("detail") or f"gate: {gate.get('status')}"})
    if claim.get("blocks"):
        blockers.append({"source": "shared_writer_claim", "detail": claim.get("detail"),
                         "stale": bool(claim.get("stale"))})
    if run_record.get("blocks"):
        blockers.append({"source": "own_run_record", "detail": run_record.get("detail"),
                         "stale": bool(run_record.get("stale"))})
    return {
        "ok": not blockers,
        "decision": "ALLOW_VISIBLE_CAPTURE" if not blockers else "BLOCK",
        "blockers": blockers,
        "gate_status": gate.get("status"),
        "checked_at_utc": utc_now_iso(),
    }


def preflight(*, write_class: str, run_id: str) -> dict[str, Any]:
    _require(bool(str(run_id).strip()), "run_id is required")
    policy = config.WRITE_CLASSES.get(write_class)
    _require(policy is not None, f"unknown write class: {write_class}")

    plan_error: str | None = None
    capability_error: str | None = None
    plan: dict[str, Any] | None = None
    plan_authorization: dict[str, Any] | None = None
    capability_result: dict[str, Any] | None = None
    try:
        plan = load_and_verify_plan()
        _require(
            bool(str(plan.get("plan_id") or "").strip()),
            "verified plan carries no plan_id",
        )
        _require(
            bool(str(plan.get("plan_hash") or "").strip()),
            "verified plan carries no plan_hash",
        )
        verify_resolved_path_bindings(plan)
        plan_authorization = verify_plan_write_authorization(plan, write_class)
    except (RiskGateError, OSError, ValueError) as exc:
        plan_error = f"{type(exc).__name__}: {exc}"
    try:
        capability_result = run_capability_scan()
        _require(
            capability_result.get("status") == "CAPABILITY_SCAN_CLEAN",
            "capability scan did not return CAPABILITY_SCAN_CLEAN",
        )
    except Exception as exc:  # noqa: BLE001 - a failed scan must block, never crash
        capability_error = f"{type(exc).__name__}: {exc}"

    free = {"present": False, "blocks": False, "stale": False}
    needs_capture_controls = bool(
        policy.get("exclusive_writer_claim") or policy.get("capture_token")
    )

    decision = evaluate_risk_preflight(
        plan_error=plan_error,
        capability_error=capability_error,
        gate=read_shared_gate(),
        claim=inspect_claim() if needs_capture_controls else free,
        run_record=inspect_run_record() if needs_capture_controls else free,
    )
    decision.update({
        "schema": PREFLIGHT_RESULT_SCHEMA,
        "verified": bool(decision["ok"]),
        "write_class": write_class,
        "run_id": run_id,
        "write_policy": dict(policy),
        "plan_id": plan.get("plan_id") if plan else None,
        "plan_hash": plan.get("plan_hash") if plan else None,
        "plan_authorization": plan_authorization,
        "capability_scan": capability_result,
    })
    resolved_paths = resolved_config()
    decision["resolved_paths"] = resolved_paths
    decision["resolved_paths_hash"] = canonical_hash(resolved_paths)
    if decision["ok"] and write_class == "metadata_registry":
        decision["decision"] = "ALLOW_METADATA_REGISTRY"
    if decision["ok"] and policy.get("capture_token"):
        token = mint_capture_token(run_id)
        decision["capture_token"] = token["token"]
        decision["capture_token_expires_at_ts"] = token["expires_at_ts"]
    return decision


def resolved_config() -> dict[str, Any]:
    paths = {
        "project_root": config.PROJECT_ROOT,
        "plan_path": config.PLAN_PATH,
        "shared_gate_path": config.SHARED_GATE_PATH,
        "shared_writer_claim_path": config.SHARED_WRITER_CLAIM_PATH,
        "capture_root": config.CAPTURE_ROOT,
        "run_record_path": config.RUN_RECORD_PATH,
        "capture_token_path": config.CAPTURE_TOKEN_PATH,
        "stop_request_path": config.STOP_REQUEST_PATH,
    }
    return {name: str(path.resolve(strict=False)) for name, path in paths.items()}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Risk gate for the pre-market perpetual capture project.")
    parser.add_argument("--print-config", action="store_true")
    parser.add_argument("--plan-check", action="store_true")
    parser.add_argument("--capability-scan", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--write-class", choices=sorted(config.WRITE_CLASSES))
    parser.add_argument("--run-id", default="")
    args = parser.parse_args(argv)

    if args.print_config:
        print(json.dumps(resolved_config(), ensure_ascii=False))
        return 0
    if args.capability_scan:
        print(json.dumps(run_capability_scan(), ensure_ascii=False))
        return 0
    if args.plan_check:
        plan = load_and_verify_plan()
        run_capability_scan()
        print(json.dumps({
            "status": "PLAN_OK",
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "risk_contract": plan["risk_contract"],
            "allowed_endpoints": len(plan["allowed_endpoints"]),
        }, ensure_ascii=False))
        return 0
    if args.preflight:
        if not args.write_class:
            parser.error("--write-class is required with --preflight")
        run_id = args.run_id or "capture_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        decision = preflight(write_class=args.write_class, run_id=run_id)
        print(json.dumps(decision, ensure_ascii=False))
        return 0 if decision["ok"] else 1
    raise SystemExit("no action requested")


if __name__ == "__main__":
    raise SystemExit(main())
