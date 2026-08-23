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
import re
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


CAPTURE_TOKEN_SCHEMA = "premarket_capture_token_v2"
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
OFFICIAL_ATTESTATION_ACTION = (
    "append one human-verified official spot t0 after attestation preflight"
)
REGISTRY_QUARANTINE_ACTION = (
    "quarantine one failed registry generation after exact recovery preflight"
)
CAPTURE_ACTION = "capture one official event in a bounded visible terminal"
WRITE_CLASS_ACTION = {
    "metadata_registry": METADATA_REGISTRY_ACTION,
    "official_attestation": OFFICIAL_ATTESTATION_ACTION,
    "registry_quarantine": REGISTRY_QUARANTINE_ACTION,
    "market_data_capture": CAPTURE_ACTION,
}
REGISTRY_QUARANTINE_PLAN_STATUS = "REGISTRY_QUARANTINE_HARDENED_NO_CAPTURE"
PLAN_WRITE_AUTHORIZATION: dict[str, dict[str, frozenset[str]]] = {
    "AWAIT_CAPTURE_IMPLEMENTATION_AUDIT_NO_CAPTURE": {
        "authorized_actions": frozenset({
            METADATA_REGISTRY_ACTION,
            OFFLINE_DESCRIPTIVE_ACTION,
            OFFICIAL_ATTESTATION_ACTION,
        }),
        "write_classes": frozenset({"metadata_registry", "official_attestation"}),
    },
    "CAPTURE_IMPLEMENTATION_AUDIT_GREEN_NO_CAPTURE": {
        "authorized_actions": frozenset({
            METADATA_REGISTRY_ACTION,
            OFFLINE_DESCRIPTIVE_ACTION,
            OFFICIAL_ATTESTATION_ACTION,
        }),
        "write_classes": frozenset({"metadata_registry", "official_attestation"}),
    },
    REGISTRY_QUARANTINE_PLAN_STATUS: {
        "authorized_actions": frozenset({
            METADATA_REGISTRY_ACTION,
            OFFLINE_DESCRIPTIVE_ACTION,
            OFFICIAL_ATTESTATION_ACTION,
            REGISTRY_QUARANTINE_ACTION,
        }),
        "write_classes": frozenset({
            "metadata_registry",
            "official_attestation",
            "registry_quarantine",
        }),
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
        "registry_quarantine_root": str(
            config.REGISTRY_QUARANTINE_ROOT.resolve(strict=False)
        ),
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
    action = WRITE_CLASS_ACTION.get(write_class)
    _require(action is not None and action in actual_actions, "write class has no exact action")
    return {
        "status": status,
        "authorized_actions": sorted(actual_actions),
        "authorized_action": action,
        "write_class": write_class,
    }


# ------------------------------------------------------------------ plan identity


def _verify_retired_plan_lineage() -> None:
    for retired in trust_root.RETIRED_PLANS:
        relative = str(retired.get("path") or "")
        path = config.PROJECT_ROOT / relative
        _require(path.is_file(), f"retired plan missing from lineage: {relative}")
        _require(
            sha256_file(path) == retired.get("plan_file_sha256"),
            f"retired plan sha256 mismatch: {relative}",
        )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RiskGateError(f"retired plan unreadable: {relative}: {exc}") from exc
        _require(payload.get("schema") == retired.get("schema"), f"retired schema mismatch: {relative}")
        _require(payload.get("plan_id") == retired.get("plan_id"), f"retired plan_id mismatch: {relative}")
        _require(payload.get("plan_hash") == retired.get("plan_hash"), f"retired plan_hash mismatch: {relative}")
        _require(
            payload.get("plan_hash")
            == canonical_hash({key: value for key, value in payload.items() if key != "plan_hash"}),
            f"retired plan canonical hash mismatch: {relative}",
        )


def load_and_verify_plan(plan_path: Path | None = None) -> dict[str, Any]:
    """The plan must be internally consistent, approved by the trust root, and an
    accurate description of the files on disk.

    The trust root is a separate small module precisely because the plan pins the
    runtime and the runtime verifies the plan: without an outside anchor, editing both
    together closes the loop and proves nothing."""
    plan_path = plan_path or config.PLAN_PATH
    _verify_retired_plan_lineage()
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
    if trust_root.RETIRED_PLANS:
        previous = trust_root.RETIRED_PLANS[-1]
        _require(plan.get("supersedes_plan_id") == previous["plan_id"], "active plan lineage id mismatch")
        _require(plan.get("supersedes_plan_hash") == previous["plan_hash"], "active plan lineage hash mismatch")
        _require(plan.get("supersedes_plan_path") == previous["path"], "active plan lineage path mismatch")

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
    # NOT verified here. These are absolute paths to a shared gate, a claim file and
    # a capture root on one machine; on another host - CI, another OS - they resolve to
    # something else entirely, and asserting equality there fails a plan that is
    # perfectly valid. The binding is an operational precondition of a WRITE, so it is
    # checked in the write preflight below, where those paths are about to be used.
    return plan


def verify_plan_identity(
    *,
    plan_id: str,
    plan_hash: str,
    implementation: Mapping[str, Any],
    required_write_class: str | None = None,
) -> dict[str, Any]:
    """Verify evidence against one exact active or immutable retired PlanOnly.

    Identity alone is not evidence-origin authority. A capture can be production
    evidence only if the selected historical Plan itself authorized the capture write
    class when the bytes were created. Current write permission is deliberately not
    consulted for retired evidence.
    """
    _require(bool(str(plan_id).strip()), "plan identity carries no plan_id")
    _require(
        bool(re.fullmatch(r"[0-9a-f]{64}", str(plan_hash or ""))),
        "plan identity carries an invalid plan_hash",
    )
    _require(
        isinstance(implementation, Mapping),
        "plan identity implementation binding is not an object",
    )

    # This also verifies every retired file and the current runtime bindings. Replay
    # evidence may be historical, but it must not run under an unbound current runtime.
    active_plan = load_and_verify_plan()
    active = (
        active_plan.get("plan_id") == plan_id
        and active_plan.get("plan_hash") == plan_hash
    )
    selected_plan: Mapping[str, Any] | None = active_plan if active else None
    selected_path: str | None = str(config.PLAN_PATH)
    if not active:
        selected_path = None
        for retired in trust_root.RETIRED_PLANS:
            if (
                retired.get("plan_id") == plan_id
                and retired.get("plan_hash") == plan_hash
            ):
                relative = str(retired.get("path") or "")
                path = config.PROJECT_ROOT / relative
                try:
                    candidate = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise RiskGateError(
                        f"retired plan identity is unreadable: {relative}: {exc}"
                    ) from exc
                _require(isinstance(candidate, Mapping), "retired plan root is not an object")
                selected_plan = candidate
                selected_path = relative
                break
    _require(selected_plan is not None, "plan identity is neither active nor immutable retired")
    _require(
        selected_plan.get("implementation") == dict(implementation),
        "plan identity implementation binding mismatch",
    )
    evidence_origin: Mapping[str, Any] | None = None
    evidence_origin_capture_root: str | None = None
    if required_write_class is not None:
        evidence_origin = verify_plan_write_authorization(
            selected_plan, required_write_class
        )
        bindings = selected_plan.get("resolved_path_bindings")
        _require(
            isinstance(bindings, Mapping),
            "selected PlanOnly has no resolved path bindings",
        )
        capture_root = Path(str(bindings.get("capture_root") or ""))
        _require(
            capture_root.is_absolute(),
            "selected PlanOnly capture root is missing or not absolute",
        )
        evidence_origin_capture_root = str(capture_root.resolve(strict=False))
    return {
        "schema": "premarket_perp_plan_identity_verification_v1",
        "ok": True,
        "status": "PLAN_IDENTITY_OK",
        "plan_id": plan_id,
        "plan_hash": plan_hash,
        "active": active,
        "plan_path": selected_path,
        "implementation_hash": canonical_hash(dict(implementation)),
        "evidence_origin_capture_authorized": (
            evidence_origin is not None
            and required_write_class == "market_data_capture"
        ),
        "evidence_origin_write_class": required_write_class,
        "evidence_origin_plan_status": selected_plan.get("status"),
        "evidence_origin_capture_root": evidence_origin_capture_root,
    }


def run_capability_scan() -> dict[str, Any]:
    result = assert_runtime_is_clean(
        config.PROJECT_ROOT,
        markers_path=config.PROJECT_ROOT / "docs/risk/forbidden-capabilities.txt",
        allowed_endpoints=config.ALLOWED_ENDPOINTS,
    )
    result["report_hash"] = canonical_hash(result)
    return result


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
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            detail = f"gate process exit code {completed.returncode}"
            if stderr:
                detail = f"{detail}: {stderr[:500]}"
            return {"open": False, "status": "UNAVAILABLE", "detail": detail}
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


def mint_capture_token(
    run_id: str,
    *,
    event_id: str,
    source_class: str,
    verified_preflight: Mapping[str, Any],
    ttl_sec: int = CAPTURE_TOKEN_TTL_SEC,
) -> dict[str, Any]:
    """Mint only from a complete capture preflight bound to one event and source."""
    _require(bool(str(run_id).strip()), "capture token run_id is required")
    _require(bool(str(event_id).strip()), "capture token event_id is required")
    _require(source_class == "OFFICIAL_ANNOUNCEMENT", "capture token requires official source")
    _require(0 < int(ttl_sec) <= CAPTURE_TOKEN_TTL_SEC, "capture token TTL exceeds policy")

    plan = load_and_verify_plan()
    authorization = verify_plan_write_authorization(plan, "market_data_capture")
    verify_resolved_path_bindings(plan)
    receipt = dict(verified_preflight)
    _require(receipt.get("schema") == PREFLIGHT_RESULT_SCHEMA, "capture preflight schema mismatch")
    _require(receipt.get("ok") is True and receipt.get("verified") is True, "capture preflight is not verified")
    _require(receipt.get("decision") == "ALLOW_VISIBLE_CAPTURE", "capture preflight did not allow capture")
    _require(receipt.get("write_class") == "market_data_capture", "capture preflight write class mismatch")
    _require(receipt.get("action") == CAPTURE_ACTION, "capture preflight action mismatch")
    _require(receipt.get("run_id") == run_id, "capture preflight run_id mismatch")
    _require(receipt.get("event_id") == event_id, "capture preflight event mismatch")
    _require(receipt.get("source_class") == source_class, "capture preflight source mismatch")
    _require(receipt.get("plan_id") == plan.get("plan_id"), "capture preflight plan_id mismatch")
    _require(receipt.get("plan_hash") == plan.get("plan_hash"), "capture preflight plan hash mismatch")
    current_paths_hash = canonical_hash(resolved_config())
    _require(
        receipt.get("resolved_paths_hash") == current_paths_hash,
        "capture preflight resolved paths do not match the current runtime",
    )
    capability = receipt.get("capability_scan")
    _require(isinstance(capability, Mapping), "capture preflight capability receipt missing")
    _require(capability.get("status") == "CAPABILITY_SCAN_CLEAN", "capture preflight capability scan not clean")
    _require(len(str(capability.get("report_hash") or "")) == 64, "capture capability hash invalid")
    _require(receipt.get("gate_status") in GATE_OPEN_STATUSES, "capture preflight gate not open")

    payload = {
        "schema": CAPTURE_TOKEN_SCHEMA,
        "token": secrets.token_hex(16),
        "run_id": run_id,
        "write_class": "market_data_capture",
        "action": CAPTURE_ACTION,
        "event_id": event_id,
        "source_class": source_class,
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "resolved_paths_hash": receipt["resolved_paths_hash"],
        "gate_status": receipt["gate_status"],
        "capability_scan_status": capability["status"],
        "capability_scan_hash": capability["report_hash"],
        "minted_at_utc": utc_now_iso(),
        "minted_by_pid": os.getpid(),
        "expires_at_ts": int(time.time()) + int(ttl_sec),
    }
    payload["binding_hash"] = canonical_hash(
        {key: value for key, value in payload.items() if key != "token"}
    )
    path = config.CAPTURE_TOKEN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise RiskGateError(
            "an outstanding capture token already exists; consume or explicitly "
            "resolve it before minting another"
        ) from exc
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            path.unlink(missing_ok=True)
        raise
    return payload


def _read_capture_token(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise RiskGateError("no capture token: run --preflight first") from exc
    except OSError as exc:
        raise RiskGateError(f"capture token is unreadable: {exc}") from exc
    if not raw or len(raw) > 64 * 1024:
        raise RiskGateError("capture token bytes are empty or exceed the policy bound")
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RiskGateError(f"capture token JSON is unreadable: {exc}") from exc
    _require(isinstance(payload, dict), "capture token JSON must be an object")
    return raw, payload


def _validate_capture_token_caller(
    payload: Mapping[str, Any],
    *,
    token: str,
    run_id: str,
    event_id: str,
    source_class: str,
) -> None:
    """Validate immutable bytes and caller authority before touching the token path."""
    _require(payload.get("schema") == CAPTURE_TOKEN_SCHEMA, "capture token schema mismatch")
    expected_binding_hash = canonical_hash(
        {key: value for key, value in payload.items() if key not in {"token", "binding_hash"}}
    )
    _require(payload.get("binding_hash") == expected_binding_hash, "capture token binding hash mismatch")
    _require(
        secrets.compare_digest(str(payload.get("token") or ""), str(token)),
        "capture token mismatch",
    )
    _require(
        str(payload.get("run_id") or "") == str(run_id),
        "capture token belongs to a different run_id",
    )
    _require(payload.get("event_id") == event_id, "capture token belongs to a different event")
    _require(
        payload.get("source_class") == source_class,
        "capture token belongs to a different source class",
    )
    _require(payload.get("write_class") == "market_data_capture", "capture token write class mismatch")
    _require(payload.get("action") == CAPTURE_ACTION, "capture token action mismatch")
    try:
        expires_at_ts = int(payload.get("expires_at_ts"))
    except (TypeError, ValueError) as exc:
        raise RiskGateError("capture token expiry is missing or malformed") from exc
    _require(expires_at_ts >= int(time.time()),
             "capture token expired: run --preflight again")
    _require(bool(str(payload.get("plan_id") or "").strip()), "capture token plan_id is missing")
    _require(len(str(payload.get("plan_hash") or "")) == 64, "capture token plan hash is invalid")
    _require(
        len(str(payload.get("resolved_paths_hash") or "")) == 64,
        "capture token resolved paths hash is invalid",
    )
    _require(
        payload.get("capability_scan_status") == "CAPABILITY_SCAN_CLEAN",
        "capture token capability status is not clean",
    )
    _require(
        len(str(payload.get("capability_scan_hash") or "")) == 64,
        "capture token capability hash is invalid",
    )


def consume_capture_token(
    *,
    token: str,
    run_id: str,
    event_id: str,
    source_class: str,
) -> dict[str, Any]:
    """Validate the caller without mutation, then atomically take and recheck live state."""
    path = config.CAPTURE_TOKEN_PATH
    raw, payload = _read_capture_token(path)
    _validate_capture_token_caller(
        payload,
        token=token,
        run_id=run_id,
        event_id=event_id,
        source_class=source_class,
    )

    consumed = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.consumed")
    try:
        os.replace(path, consumed)
    except OSError as exc:
        raise RiskGateError(f"capture token could not be taken: {exc}") from exc
    try:
        try:
            claimed_raw = consumed.read_bytes()
        except OSError as exc:
            raise RiskGateError(f"consumed capture token is unreadable: {exc}") from exc
        _require(
            claimed_raw == raw,
            "capture token changed between validation and atomic consume",
        )

        plan = load_and_verify_plan()
        _require(payload.get("plan_id") == plan.get("plan_id"), "capture token plan_id is stale")
        _require(payload.get("plan_hash") == plan.get("plan_hash"), "capture token plan hash is stale")
        verify_plan_write_authorization(plan, "market_data_capture")
        verify_resolved_path_bindings(plan)
        _require(
            payload.get("resolved_paths_hash") == canonical_hash(resolved_config()),
            "current resolved paths differ from the capture token",
        )
        capability = run_capability_scan()
        _require(capability.get("status") == "CAPABILITY_SCAN_CLEAN", "current capability scan is not clean")
        _require(
            capability.get("report_hash") == payload.get("capability_scan_hash"),
            "current capability scan differs from token",
        )
        gate = read_shared_gate()
        _require(
            gate.get("open") is True and gate.get("status") in GATE_OPEN_STATUSES,
            f"current shared gate is not open: {gate.get('status')}",
        )
        claim = inspect_claim()
        _require(not claim.get("blocks"), str(claim.get("detail") or "shared writer claim blocks"))
        run_record = inspect_run_record()
        _require(not run_record.get("blocks"), str(run_record.get("detail") or "capture run record blocks"))
        return payload
    finally:
        consumed.unlink(missing_ok=True)


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


def preflight(
    *,
    write_class: str,
    run_id: str,
    event_id: str = "",
    source_class: str = "",
) -> dict[str, Any]:
    _require(bool(str(run_id).strip()), "run_id is required")
    policy = config.WRITE_CLASSES.get(write_class)
    _require(policy is not None, f"unknown write class: {write_class}")
    if write_class == "market_data_capture":
        _require(bool(str(event_id).strip()), "event_id is required for capture preflight")
        _require(
            source_class == "OFFICIAL_ANNOUNCEMENT",
            "capture preflight requires OFFICIAL_ANNOUNCEMENT",
        )

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
        "action": (
            plan_authorization.get("authorized_action")
            if plan_authorization is not None
            else WRITE_CLASS_ACTION.get(write_class)
        ),
        "event_id": event_id or None,
        "source_class": source_class or None,
        "capability_scan": capability_result,
    })
    resolved_paths = resolved_config()
    decision["resolved_paths"] = resolved_paths
    decision["resolved_paths_hash"] = canonical_hash(resolved_paths)
    if decision["ok"] and write_class == "metadata_registry":
        decision["decision"] = "ALLOW_METADATA_REGISTRY"
    if decision["ok"] and write_class == "official_attestation":
        decision["decision"] = "ALLOW_OFFICIAL_ATTESTATION"
    if decision["ok"] and write_class == "registry_quarantine":
        decision["decision"] = "ALLOW_REGISTRY_QUARANTINE"
    if decision["ok"] and policy.get("capture_token"):
        token = mint_capture_token(
            run_id,
            event_id=event_id,
            source_class=source_class,
            verified_preflight=decision,
        )
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
        "registry_quarantine_root": config.REGISTRY_QUARANTINE_ROOT,
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
    parser.add_argument("--event-id", default="")
    parser.add_argument("--source-class", default="")
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
        preflight_args: dict[str, str] = {
            "write_class": args.write_class,
            "run_id": run_id,
        }
        if args.write_class == "market_data_capture":
            preflight_args.update({
                "event_id": args.event_id,
                "source_class": args.source_class,
            })
        decision = preflight(**preflight_args)
        print(json.dumps(decision, ensure_ascii=False))
        return 0 if decision["ok"] else 1
    raise SystemExit("no action requested")


if __name__ == "__main__":
    raise SystemExit(main())
