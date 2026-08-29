"""At-most-once local alerts for unverified announcement candidates.

The module never promotes a discovery hint into official evidence and never
authorizes capture.  It reads the verified candidate store, links a notification
to the first candidate revision, and writes an append-only intent/terminal ledger
around the one external side effect: submitting a local Windows toast.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

import announcement_candidate_store as candidate_store
from announcement_watch_state import process_is_alive
from canonical_hash import canonical_json_bytes
import frozen_plan_bindings as trust_root
import project_config as config


ALERT_SCHEMA = "premarket_candidate_alert_ledger_v1"
ALERT_PAYLOAD_SCHEMA = "premarket_candidate_alert_toast_v1"
ALERT_RESULT_SCHEMA = "premarket_candidate_alert_toast_result_v1"
ALERT_PREFLIGHT_SCHEMA = "premarket_candidate_alert_toast_preflight_v1"
ALERT_KIND = "UNVERIFIED_ANNOUNCEMENT_CANDIDATE"
ALERT_GROUP = "ZLP-PREMARKET"
ALERT_ACTION = "submit one local candidate notification after alert preflight"

_INTENT_PHASE = "SUBMISSION_INTENT"
_TERMINAL_PHASES = frozenset(
    {
        "WINDOWS_HISTORY_CONFIRMED",
        "WINDOWS_SUBMITTED_UNCONFIRMED",
        "DELIVERY_UNCERTAIN_NO_AUTO_RETRY",
    }
)
_TARGET_IDENTITY_FIELDS = (
    "episode_id",
    "perpetual_venue",
    "premarket_contract_id",
    "lifecycle_generation",
    "asset_class",
    "issuer_namespace",
    "issuer_id",
    "asset_identity_hash",
)
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_HEX_16 = re.compile(r"^[0-9a-f]{16}$")


class CandidateAlertError(RuntimeError):
    """Candidate, ledger, preflight, or notifier evidence is invalid."""


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _record_hash(record: Mapping[str, Any]) -> str:
    return _sha256(
        {key: value for key, value in record.items() if key != "record_hash"}
    )


def _utc(timestamp: int) -> str:
    return datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _canonical_text(value: object, *, field: str, max_length: int = 2048) -> str:
    if not isinstance(value, str):
        raise CandidateAlertError(f"{field} must be text")
    if not value or value != value.strip() or len(value) > max_length:
        raise CandidateAlertError(f"{field} is missing or non-canonical")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise CandidateAlertError(f"{field} contains a control character")
    return value


def _known_plan_identities() -> frozenset[tuple[str, str]]:
    entries = [trust_root.ACTIVE_PLAN, *trust_root.RETIRED_PLANS]
    return frozenset(
        (str(entry.get("plan_id") or ""), str(entry.get("plan_hash") or ""))
        for entry in entries
        if isinstance(entry, Mapping)
    )


def _load_alert_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if not path.is_file():
        raise CandidateAlertError("alert ledger path is not a regular file")
    raw = path.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        raise CandidateAlertError("alert ledger is empty or truncated")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise CandidateAlertError("alert ledger is not UTF-8") from exc

    records: list[dict[str, Any]] = []
    previous_hash: str | None = None
    open_intent: dict[str, Any] | None = None
    terminal_candidates: set[str] = set()
    known_plans = _known_plan_identities()
    for sequence, line in enumerate(lines):
        if not line:
            raise CandidateAlertError("alert ledger contains a blank record")
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise CandidateAlertError("alert ledger contains invalid JSON") from exc
        if not isinstance(record, dict):
            raise CandidateAlertError("alert ledger record is not an object")
        if record.get("schema") != ALERT_SCHEMA:
            raise CandidateAlertError("alert ledger schema mismatch")
        if record.get("record_seq") != sequence:
            raise CandidateAlertError("alert ledger sequence mismatch")
        if record.get("previous_record_hash") != previous_hash:
            raise CandidateAlertError("alert ledger previous hash mismatch")
        claimed_hash = record.get("record_hash")
        if (
            not isinstance(claimed_hash, str)
            or _HEX_64.fullmatch(claimed_hash) is None
            or claimed_hash != _record_hash(record)
        ):
            raise CandidateAlertError("alert ledger record hash mismatch")
        if record.get("capture_authorized") is not False:
            raise CandidateAlertError("alert ledger attempts to confer capture authority")
        if record.get("alert_kind") != ALERT_KIND:
            raise CandidateAlertError("alert ledger kind mismatch")
        if (
            str(record.get("plan_id") or ""),
            str(record.get("plan_hash") or ""),
        ) not in known_plans:
            raise CandidateAlertError("alert ledger plan identity is not trusted")
        candidate_id = record.get("candidate_id")
        notification_id = record.get("notification_id")
        candidate_record_hash = record.get("candidate_record_hash")
        tag = record.get("tag")
        if any(
            not isinstance(value, str) or _HEX_64.fullmatch(value) is None
            for value in (candidate_id, notification_id, candidate_record_hash)
        ):
            raise CandidateAlertError("alert ledger identity hash is malformed")
        if not isinstance(tag, str) or _HEX_16.fullmatch(tag) is None:
            raise CandidateAlertError("alert ledger tag is malformed")
        if record.get("group") != ALERT_GROUP:
            raise CandidateAlertError("alert ledger group mismatch")

        phase = record.get("phase")
        if phase == _INTENT_PHASE:
            if open_intent is not None:
                raise CandidateAlertError("alert ledger has overlapping intents")
            if candidate_id in terminal_candidates:
                raise CandidateAlertError("alert ledger repeats a terminal candidate")
            open_intent = record
        elif phase in _TERMINAL_PHASES:
            if open_intent is None:
                raise CandidateAlertError("alert terminal has no submission intent")
            for field in (
                "candidate_id",
                "candidate_record_hash",
                "notification_id",
                "tag",
                "group",
            ):
                if record.get(field) != open_intent.get(field):
                    raise CandidateAlertError(
                        f"alert terminal does not match its intent: {field}"
                    )
            terminal_candidates.add(str(candidate_id))
            open_intent = None
        else:
            raise CandidateAlertError("alert ledger phase is invalid")
        previous_hash = claimed_hash
        records.append(record)
    return records


def _append_alert_record(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    existing = _load_alert_ledger(path)
    record = dict(payload)
    record["schema"] = ALERT_SCHEMA
    record["record_seq"] = len(existing)
    record["previous_record_hash"] = (
        existing[-1]["record_hash"] if existing else None
    )
    record["record_hash"] = _record_hash(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND
    if existing:
        descriptor = os.open(path, flags)
    else:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "ab", buffering=0) as handle:
            handle.write(
                json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
                + b"\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise CandidateAlertError("alert ledger append or fsync failed") from exc
    return record


@contextmanager
def _alert_ledger_lock(path: Path, *, run_id: str) -> Iterator[None]:
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "premarket_candidate_alert_lock_v1",
        "owner_pid": os.getpid(),
        "owner_host": socket.gethostname(),
        "run_id": run_id,
        "nonce": secrets.token_hex(16),
    }
    descriptor: int | None = None
    for _attempt in range(2):
        try:
            descriptor = os.open(
                lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            break
        except FileExistsError:
            try:
                existing = json.loads(lock_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise CandidateAlertError(
                    "candidate alert lock is unreadable"
                ) from exc
            expected_fields = {
                "schema", "owner_pid", "owner_host", "run_id", "nonce"
            }
            if not isinstance(existing, dict) or set(existing) != expected_fields:
                raise CandidateAlertError("candidate alert lock schema is invalid")
            if existing.get("schema") != "premarket_candidate_alert_lock_v1":
                raise CandidateAlertError("candidate alert lock schema is invalid")
            owner_pid = existing.get("owner_pid")
            owner_host = existing.get("owner_host")
            nonce = existing.get("nonce")
            if (
                isinstance(owner_pid, bool)
                or not isinstance(owner_pid, int)
                or owner_pid <= 0
                or not isinstance(owner_host, str)
                or not owner_host
                or existing.get("run_id") is None
                or not isinstance(nonce, str)
                or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
            ):
                raise CandidateAlertError("candidate alert lock identity is invalid")
            if owner_host != socket.gethostname():
                raise CandidateAlertError("candidate alert ledger is remotely locked")
            if process_is_alive(owner_pid) is not False:
                raise CandidateAlertError("candidate alert ledger is locked")
            archive_dir = Path(str(lock_path) + ".archive")
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive = archive_dir / f"{owner_pid}-{nonce}.json"
            if archive.exists():
                raise CandidateAlertError(
                    "candidate alert stale-lock archive already exists"
                )
            try:
                lock_path.rename(archive)
            except OSError as exc:
                raise CandidateAlertError(
                    "candidate alert stale lock could not be archived"
                ) from exc
    if descriptor is None:
        raise CandidateAlertError("candidate alert ledger could not be locked")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        try:
            current = json.loads(lock_path.read_text(encoding="utf-8"))
            if current != payload:
                raise CandidateAlertError("candidate alert lock ownership was lost")
            lock_path.unlink()
        except FileNotFoundError as exc:
            raise CandidateAlertError("candidate alert lock disappeared") from exc


def _validate_preflight(receipt: object, *, run_id: str) -> Mapping[str, Any]:
    if not isinstance(receipt, Mapping):
        raise CandidateAlertError("candidate alert preflight returned a non-object")
    expected = {
        "schema": "premarket_write_preflight_v2",
        "ok": True,
        "verified": True,
        "decision": "ALLOW_CANDIDATE_ALERT",
        "write_class": "candidate_alert",
        "run_id": run_id,
        "action": ALERT_ACTION,
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise CandidateAlertError(
                f"candidate alert preflight mismatch: {field}"
            )
    resolved_paths_hash = receipt.get("resolved_paths_hash")
    if (
        not isinstance(resolved_paths_hash, str)
        or _HEX_64.fullmatch(resolved_paths_hash) is None
    ):
        raise CandidateAlertError(
            "candidate alert preflight has no exact resolved paths hash"
        )
    return receipt


def _target_snapshot(
    selector: Callable[..., Mapping[str, Any]], *, now_ts: int
) -> tuple[Mapping[str, Any], ...]:
    report = selector(now_ts=int(now_ts))
    if not isinstance(report, Mapping):
        raise CandidateAlertError("candidate target selector returned a non-object")
    status = report.get("status")
    if status == "NO_ANNOUNCEMENT_TARGETS":
        return ()
    if status != "TARGETS_READY":
        raise CandidateAlertError(
            f"candidate target authority is unavailable: {status or 'UNKNOWN'}"
        )
    rows = report.get("targets")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise CandidateAlertError("candidate target selector returned malformed targets")
    targets: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise CandidateAlertError("candidate target row is not an object")
        targets.append(row)
    return tuple(targets)


def _candidate_matches_target(
    candidate: Mapping[str, Any], target: Mapping[str, Any]
) -> bool:
    return all(candidate.get(field) == target.get(field) for field in _TARGET_IDENTITY_FIELDS)


def _eligible_first_revisions(
    records: Sequence[Mapping[str, Any]], targets: Sequence[Mapping[str, Any]]
) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        record
        for record in records
        if record.get("content_revision") == 0
        and record.get("asset_class") == "CRYPTO_TOKEN"
        and record.get("review_state") == "HUMAN_ATTESTATION_REQUIRED"
        and record.get("evidence_class") == "UNVERIFIED_ANNOUNCEMENT_DISCOVERY"
        and any(_candidate_matches_target(record, target) for target in targets)
    )


def inspect_candidate_review_queue(
    *,
    now_ts: int,
    candidate_store_path: str | os.PathLike[str],
    alert_ledger_path: str | os.PathLike[str],
    target_selector: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a copyable, verified operator queue without network or mutation."""
    if isinstance(now_ts, bool) or not isinstance(now_ts, int) or now_ts < 0:
        raise CandidateAlertError("now_ts is invalid")
    candidate_records = candidate_store.load_verified_candidate_records(
        Path(candidate_store_path)
    )
    targets = _target_snapshot(target_selector, now_ts=now_ts)
    alert_records = _load_alert_ledger(Path(alert_ledger_path))
    latest_phase = {
        str(record["candidate_id"]): str(record["phase"])
        for record in alert_records
    }
    rows: list[dict[str, Any]] = []
    for candidate in _eligible_first_revisions(candidate_records, targets):
        candidate_id = str(candidate["candidate_id"])
        rows.append(
            {
                "candidate_id": candidate_id,
                "episode_id": candidate["episode_id"],
                "lifecycle_generation": candidate["lifecycle_generation"],
                "perpetual_venue": candidate["perpetual_venue"],
                "premarket_contract_id": candidate["premarket_contract_id"],
                "listing_venue": candidate["listing_venue"],
                "ticker": candidate["issuer_id"],
                "article_title": candidate["article_title"],
                "article_url": candidate["article_url"],
                "article_published_at_ms": candidate["article_published_at_ms"],
                "detected_at_utc": candidate["detected_at_utc"],
                "review_state": candidate["review_state"],
                "alert_phase": latest_phase.get(candidate_id),
            }
        )
    return {
        "schema": "premarket_candidate_review_queue_v1",
        "status": (
            "CANDIDATES_READY_FOR_HUMAN_REVIEW"
            if rows
            else "NO_CANDIDATES_FOR_REVIEW"
        ),
        "as_of_utc": _utc(now_ts),
        "count": len(rows),
        "candidates": rows,
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
        "network_used": False,
        "writes_performed": False,
        "capture_authorized": False,
    }


def _ledger_state(
    records: Sequence[Mapping[str, Any]],
) -> tuple[frozenset[str], tuple[Mapping[str, Any], ...]]:
    terminal = frozenset(
        str(record["candidate_id"])
        for record in records
        if record.get("phase") in _TERMINAL_PHASES
    )
    open_intents: list[Mapping[str, Any]] = []
    open_record: Mapping[str, Any] | None = None
    for record in records:
        if record.get("phase") == _INTENT_PHASE:
            open_record = record
        elif record.get("phase") in _TERMINAL_PHASES:
            open_record = None
    if open_record is not None:
        open_intents.append(open_record)
    return terminal, tuple(open_intents)


def _notification_id(candidate: Mapping[str, Any]) -> str:
    return _sha256(
        {
            "alert_kind": ALERT_KIND,
            "candidate_id": candidate["candidate_id"],
            "candidate_record_hash": candidate["record_hash"],
        }
    )


def _notification_payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    notification_id = _notification_id(candidate)
    return {
        "schema": ALERT_PAYLOAD_SCHEMA,
        "notification_id": notification_id,
        "alert_kind": ALERT_KIND,
        "candidate_id": candidate["candidate_id"],
        "candidate_record_hash": candidate["record_hash"],
        "episode_id": candidate["episode_id"],
        "ticker": candidate["issuer_id"],
        "perpetual_venue": candidate["perpetual_venue"],
        "premarket_contract_id": candidate["premarket_contract_id"],
        "listing_venue": candidate["listing_venue"],
        "article_title": candidate["article_title"],
        "article_url": candidate["article_url"],
        "review_state": "HUMAN_ATTESTATION_REQUIRED",
        "capture_authorized": False,
        "tag": notification_id[:16],
        "group": ALERT_GROUP,
    }


def _base_ledger_record(
    *,
    payload: Mapping[str, Any],
    phase: str,
    now_ts: int,
    run_id: str,
) -> dict[str, Any]:
    return {
        "phase": phase,
        "alert_kind": ALERT_KIND,
        "notification_id": payload["notification_id"],
        "candidate_id": payload["candidate_id"],
        "candidate_record_hash": payload["candidate_record_hash"],
        "tag": payload["tag"],
        "group": ALERT_GROUP,
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
        "write_run_id": run_id,
        "recorded_at_utc": _utc(now_ts),
        "capture_authorized": False,
    }


def _terminal_from_intent(
    intent: Mapping[str, Any],
    *,
    phase: str,
    now_ts: int,
    run_id: str,
    notifier_result: Mapping[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    payload = {
        key: intent[key]
        for key in (
            "alert_kind",
            "notification_id",
            "candidate_id",
            "candidate_record_hash",
            "tag",
            "group",
        )
    }
    terminal = _base_ledger_record(
        payload=payload,
        phase=phase,
        now_ts=now_ts,
        run_id=run_id,
    )
    terminal["intent_record_hash"] = intent["record_hash"]
    terminal["notifier_result_hash"] = (
        _sha256(dict(notifier_result)) if notifier_result is not None else None
    )
    terminal["reason"] = reason
    return terminal


def _validate_notifier_result(
    result: object, *, payload: Mapping[str, Any]
) -> Mapping[str, Any]:
    if not isinstance(result, Mapping):
        raise CandidateAlertError("toast sidecar returned a non-object")
    if result.get("schema") != ALERT_RESULT_SCHEMA:
        raise CandidateAlertError("toast sidecar result schema mismatch")
    if result.get("status") not in {
        "WINDOWS_HISTORY_CONFIRMED",
        "WINDOWS_SUBMITTED_UNCONFIRMED",
    }:
        raise CandidateAlertError("toast sidecar returned an unknown status")
    for field in ("notification_id", "tag", "group"):
        if result.get(field) != payload.get(field):
            raise CandidateAlertError(f"toast sidecar result mismatch: {field}")
    if result.get("show_invoked") is not True:
        raise CandidateAlertError("toast sidecar did not attest a Show invocation")
    return result


def _result(
    status: str,
    *,
    submitted: int = 0,
    history_confirmed: int = 0,
    uncertain: int = 0,
    pending_retry: bool = False,
    reason: str | None = None,
    ledger_head: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "submitted_alerts": int(submitted),
        "history_confirmed_alerts": int(history_confirmed),
        "uncertain_candidates": int(uncertain),
        "pending_retry": bool(pending_retry),
        "reason": reason,
        "alert_ledger_head_hash": ledger_head,
        "capture_authorized": False,
    }


class WindowsToastNotifier:
    """Exact hidden Windows PowerShell 5.1 adapter for the toast sidecar."""

    def __init__(
        self,
        *,
        executable: str | os.PathLike[str] | None = None,
        script: str | os.PathLike[str] | None = None,
        timeout_sec: int = 10,
    ) -> None:
        configured_executable = getattr(
            config,
            "WINDOWS_POWERSHELL_EXECUTABLE",
            "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        )
        configured_script = getattr(
            config,
            "CANDIDATE_ALERT_SIDECAR_PATH",
            config.PROJECT_ROOT / "tools/show_premarket_candidate_alert.ps1",
        )
        self.executable = Path(executable or configured_executable)
        self.script = Path(script or configured_script)
        self.timeout_sec = int(timeout_sec)
        if self.timeout_sec <= 0 or self.timeout_sec > 30:
            raise CandidateAlertError("toast sidecar timeout is outside bounds")

    def _run(self, mode: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if mode not in {"Preflight", "Show"}:
            raise CandidateAlertError("toast sidecar mode is invalid")
        if not self.executable.is_file():
            raise CandidateAlertError(
                f"Windows PowerShell executable is unavailable: {self.executable}"
            )
        if not self.script.is_file():
            raise CandidateAlertError(f"toast sidecar is unavailable: {self.script}")
        command = [
            str(self.executable),
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script),
            f"-{mode}",
            "-Json",
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                command,
                input=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                text=True,
                capture_output=True,
                timeout=self.timeout_sec,
                check=False,
                shell=False,
                creationflags=creationflags,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CandidateAlertError(
                f"toast sidecar process failed: {type(exc).__name__}: {exc}"
            ) from exc
        output = completed.stdout.strip()
        if completed.returncode != 0:
            detail = (completed.stderr.strip() or output or "no diagnostic")[:1024]
            raise CandidateAlertError(
                f"toast sidecar exited {completed.returncode}: {detail}"
            )
        try:
            result = json.loads(output)
        except ValueError as exc:
            raise CandidateAlertError("toast sidecar did not return JSON") from exc
        if not isinstance(result, dict):
            raise CandidateAlertError("toast sidecar JSON is not an object")
        return result

    def preflight(self, _payload: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self._run("Preflight", {})
        if (
            result.get("schema") != ALERT_PREFLIGHT_SCHEMA
            or result.get("status") != "READY"
        ):
            raise CandidateAlertError("toast sidecar preflight is not ready")
        return result

    def __call__(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._run("Show", payload)


def process_candidate_alerts(
    *,
    now_ts: int,
    run_id: str,
    candidate_store_path: str | os.PathLike[str],
    alert_ledger_path: str | os.PathLike[str],
    target_selector: Callable[..., Mapping[str, Any]],
    preflight: Callable[..., Mapping[str, Any]],
    notifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Submit at most one local notification per current first-revision candidate."""

    try:
        timestamp = int(now_ts)
        if isinstance(now_ts, bool) or timestamp < 0:
            raise CandidateAlertError("now_ts is invalid")
        run_id = _canonical_text(run_id, field="run_id", max_length=256)
        candidate_path = Path(candidate_store_path)
        alert_path = Path(alert_ledger_path)
        candidate_records = candidate_store.load_verified_candidate_records(
            candidate_path
        )
        targets = _target_snapshot(target_selector, now_ts=timestamp)
        alert_records = _load_alert_ledger(alert_path)
        terminal_ids, open_intents = _ledger_state(alert_records)
        eligible = _eligible_first_revisions(candidate_records, targets)
        attempted_ids = terminal_ids.union(
            str(intent["candidate_id"]) for intent in open_intents
        )
        pending = tuple(
            candidate
            for candidate in eligible
            if str(candidate["candidate_id"]) not in attempted_ids
        )
        if not pending and not open_intents:
            return _result(
                "NO_NEW_CANDIDATE_ALERTS",
                ledger_head=(
                    str(alert_records[-1]["record_hash"]) if alert_records else None
                ),
            )

        receipt = preflight(write_class="candidate_alert", run_id=run_id)
        _validate_preflight(receipt, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - no intent may be written on failure
        return _result(
            "CANDIDATE_ALERT_RETRY_NEXT_INTERVAL",
            pending_retry=True,
            reason=f"{type(exc).__name__}: {exc}",
        )


    notifier_impl: Any = notifier or WindowsToastNotifier()
    try:
        with _alert_ledger_lock(alert_path, run_id=run_id):
            commit_receipt = preflight(write_class="candidate_alert", run_id=run_id)
            _validate_preflight(commit_receipt, run_id=run_id)
            authority_fields = ("plan_id", "plan_hash", "resolved_paths_hash")
            if any(
                commit_receipt.get(field) != receipt.get(field)
                for field in authority_fields
            ):
                raise CandidateAlertError(
                    "candidate alert authority changed while acquiring the lock"
                )
            # Re-read every authority surface under the alert exclusion before
            # writing an intent.  A discovery append or terminal transition that
            # raced the preliminary read must not be notified from stale state.
            candidate_records = candidate_store.load_verified_candidate_records(
                candidate_path
            )
            targets = _target_snapshot(target_selector, now_ts=timestamp)
            alert_records = _load_alert_ledger(alert_path)
            terminal_ids, open_intents = _ledger_state(alert_records)
            uncertain_count = 0
            for open_intent in open_intents:
                _append_alert_record(
                    alert_path,
                    _terminal_from_intent(
                        open_intent,
                        phase="DELIVERY_UNCERTAIN_NO_AUTO_RETRY",
                        now_ts=timestamp,
                        run_id=run_id,
                        reason=(
                            "previous process ended after submission intent; "
                            "automatic retry is forbidden"
                        ),
                    ),
                )
                uncertain_count += 1

            alert_records = _load_alert_ledger(alert_path)
            terminal_ids, _ = _ledger_state(alert_records)
            eligible = _eligible_first_revisions(candidate_records, targets)
            pending = tuple(
                candidate
                for candidate in eligible
                if str(candidate["candidate_id"]) not in terminal_ids
            )
            if not pending:
                if uncertain_count:
                    return _result(
                        "DELIVERY_UNCERTAIN_NO_AUTO_RETRY",
                        uncertain=uncertain_count,
                        ledger_head=str(
                            _load_alert_ledger(alert_path)[-1]["record_hash"]
                        ),
                    )
                return _result(
                    "NO_NEW_CANDIDATE_ALERTS",
                    ledger_head=(
                        str(alert_records[-1]["record_hash"])
                        if alert_records
                        else None
                    ),
                )

            notifier_preflight = getattr(notifier_impl, "preflight", None)
            if callable(notifier_preflight):
                notifier_preflight({})

            confirmed_count = 0
            submitted_unconfirmed_count = 0
            for candidate in pending:
                payload = _notification_payload(candidate)
                intent = _append_alert_record(
                    alert_path,
                    _base_ledger_record(
                        payload=payload,
                        phase=_INTENT_PHASE,
                        now_ts=timestamp,
                        run_id=run_id,
                    ),
                )
                try:
                    raw_result = notifier_impl(dict(payload))
                    notifier_result = _validate_notifier_result(
                        raw_result, payload=payload
                    )
                    terminal_phase = str(notifier_result["status"])
                    _append_alert_record(
                        alert_path,
                        _terminal_from_intent(
                            intent,
                            phase=terminal_phase,
                            now_ts=timestamp,
                            run_id=run_id,
                            notifier_result=notifier_result,
                        ),
                    )
                    if terminal_phase == "WINDOWS_HISTORY_CONFIRMED":
                        confirmed_count += 1
                    else:
                        submitted_unconfirmed_count += 1
                except Exception as exc:  # noqa: BLE001 - never resubmit after intent
                    _append_alert_record(
                        alert_path,
                        _terminal_from_intent(
                            intent,
                            phase="DELIVERY_UNCERTAIN_NO_AUTO_RETRY",
                            now_ts=timestamp,
                            run_id=run_id,
                            reason=f"{type(exc).__name__}: {str(exc)[:1024]}",
                        ),
                    )
                    uncertain_count += 1

            head = str(_load_alert_ledger(alert_path)[-1]["record_hash"])
            if uncertain_count:
                return _result(
                    "DELIVERY_UNCERTAIN_NO_AUTO_RETRY",
                    submitted=confirmed_count + submitted_unconfirmed_count,
                    history_confirmed=confirmed_count,
                    uncertain=uncertain_count,
                    ledger_head=head,
                )
            if submitted_unconfirmed_count:
                return _result(
                    "CANDIDATE_ALERTS_SUBMITTED_UNCONFIRMED",
                    submitted=confirmed_count + submitted_unconfirmed_count,
                    history_confirmed=confirmed_count,
                    ledger_head=head,
                )
            return _result(
                "CANDIDATE_ALERTS_HISTORY_CONFIRMED",
                submitted=confirmed_count,
                history_confirmed=confirmed_count,
                ledger_head=head,
            )
    except Exception as exc:  # noqa: BLE001 - pre-intent failures retry next interval
        return _result(
            "CANDIDATE_ALERT_RETRY_NEXT_INTERVAL",
            pending_retry=True,
            reason=f"{type(exc).__name__}: {exc}",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read the verified candidate queue without sending a notification."
    )
    parser.add_argument("--review-status", action="store_true")
    parser.add_argument("--now-ts", type=int)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.review_status:
        parser.error("--review-status is required")
    try:
        import event_registry

        result = inspect_candidate_review_queue(
            now_ts=(int(args.now_ts) if args.now_ts is not None else int(time.time())),
            candidate_store_path=config.ANNOUNCEMENT_CANDIDATE_PATH,
            alert_ledger_path=config.CANDIDATE_ALERT_LEDGER_PATH,
            target_selector=event_registry.select_unattested_crypto_premarket_episodes,
        )
        code = 0
    except Exception as exc:  # noqa: BLE001 - status must surface fail-closed detail
        result = {
            "schema": "premarket_candidate_review_queue_v1",
            "status": "CANDIDATE_REVIEW_STATUS_UNAVAILABLE",
            "reason": f"{type(exc).__name__}: {exc}",
            "network_used": False,
            "writes_performed": False,
            "capture_authorized": False,
        }
        code = 2
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            indent=None if args.json else 2,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
