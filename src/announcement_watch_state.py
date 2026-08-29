"""Durable local state for the no-model announcement watcher."""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from canonical_hash import canonical_hash


ATTEMPT_SCHEMA = "premarket_announcement_watch_attempt_v1"
STATE_SCHEMA = "premarket_announcement_watch_state_v1"


class WatchStateError(RuntimeError):
    """Control-plane state is missing authority or has a broken hash chain."""


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _parse_utc(value: object) -> int:
    if not isinstance(value, str) or not value or value != value.strip():
        raise WatchStateError("timestamp must be a canonical UTC string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WatchStateError(f"invalid UTC timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise WatchStateError("timestamp must carry an explicit UTC offset")
    return int(parsed.timestamp())


def _hashed(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = dict(payload)
    result[field] = canonical_hash(result)
    return result


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def process_is_alive(pid: object) -> bool | None:
    try:
        numeric = int(pid)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, numeric)
            if not handle:
                return kernel32.GetLastError() == 5
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - liveness uncertainty must fail closed
            return None
    try:
        os.kill(numeric, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


@dataclass(frozen=True)
class WatchPaths:
    state: Path
    ledger: Path
    claim: Path
    claim_archive: Path


class WatchStateStore:
    def __init__(self, paths: WatchPaths, *, plan_id: str, plan_hash: str) -> None:
        self.paths = paths
        self.plan_id = plan_id
        self.plan_hash = plan_hash

    def acquire_claim(self, *, now_ts: int) -> dict[str, Any]:
        self.paths.claim.parent.mkdir(parents=True, exist_ok=True)
        stale_recovered = False
        for _attempt in range(2):
            claim = _hashed({
                "schema": "premarket_announcement_watch_claim_v1",
                "claim_id": secrets.token_hex(16),
                "owner_pid": os.getpid(),
                "owner_host": socket.gethostname(),
                "acquired_at_utc": _iso(now_ts),
                "plan_id": self.plan_id,
                "plan_hash": self.plan_hash,
            }, "claim_hash")
            try:
                descriptor = os.open(
                    self.paths.claim,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                existing = self._read_claim()
                same_host = existing.get("owner_host") == socket.gethostname()
                alive = process_is_alive(existing.get("owner_pid")) if same_host else None
                if alive is not False:
                    return {
                        "status": "ALREADY_RUNNING",
                        "claim": existing,
                        "stale_claim_recovered": False,
                    }
                self._archive_current_claim(existing, terminal_status="STALE_RECOVERED")
                stale_recovered = True
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(claim, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            return {
                "status": "ACQUIRED",
                "claim": claim,
                "stale_claim_recovered": stale_recovered,
            }
        return {
            "status": "ALREADY_RUNNING",
            "claim": self._read_claim(),
            "stale_claim_recovered": stale_recovered,
        }

    def release_claim(
        self, claim: Mapping[str, Any], *, terminal_status: str, now_ts: int
    ) -> Path:
        del now_ts  # release time is represented by the terminal attempt ledger
        current = self._read_claim()
        if (
            current.get("claim_id") != claim.get("claim_id")
            or current.get("claim_hash") != claim.get("claim_hash")
        ):
            raise WatchStateError("watch claim ownership was lost before release")
        return self._archive_current_claim(current, terminal_status=terminal_status)

    def _read_claim(self) -> dict[str, Any]:
        try:
            claim = json.loads(self.paths.claim.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WatchStateError(f"watch claim is unreadable: {exc}") from exc
        if not isinstance(claim, dict):
            raise WatchStateError("watch claim is not an object")
        if claim.get("schema") != "premarket_announcement_watch_claim_v1":
            raise WatchStateError("watch claim schema mismatch")
        if claim.get("plan_id") != self.plan_id or claim.get("plan_hash") != self.plan_hash:
            raise WatchStateError("watch claim plan identity mismatch")
        recorded = claim.get("claim_hash")
        payload = {key: value for key, value in claim.items() if key != "claim_hash"}
        if not isinstance(recorded, str) or canonical_hash(payload) != recorded:
            raise WatchStateError("watch claim hash mismatch")
        return claim

    def _archive_current_claim(
        self, claim: Mapping[str, Any], *, terminal_status: str
    ) -> Path:
        current = self._read_claim()
        if current.get("claim_hash") != claim.get("claim_hash"):
            raise WatchStateError("watch claim changed before archive")
        self.paths.claim_archive.mkdir(parents=True, exist_ok=True)
        safe_status = re.sub(r"[^A-Z0-9_-]+", "_", str(terminal_status).upper())
        archive = self.paths.claim_archive / (
            f"{current['acquired_at_utc'].replace(':', '').replace('-', '')}-"
            f"{current['claim_id']}-{safe_status}.json"
        )
        try:
            os.replace(self.paths.claim, archive)
        except FileExistsError as exc:
            raise WatchStateError("watch claim archive target already exists") from exc
        return archive

    def _verify_record(self, record: Mapping[str, Any]) -> None:
        if record.get("schema") != ATTEMPT_SCHEMA:
            raise WatchStateError("attempt ledger schema mismatch")
        if record.get("plan_id") != self.plan_id or record.get("plan_hash") != self.plan_hash:
            raise WatchStateError("attempt ledger plan identity mismatch")
        recorded = record.get("record_hash")
        payload = {key: value for key, value in record.items() if key != "record_hash"}
        if not isinstance(recorded, str) or canonical_hash(payload) != recorded:
            raise WatchStateError("attempt ledger record hash mismatch")

    def verify_ledger(self) -> list[dict[str, Any]]:
        if not self.paths.ledger.is_file():
            return []
        records: list[dict[str, Any]] = []
        previous: str | None = None
        try:
            lines = self.paths.ledger.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise WatchStateError(f"attempt ledger unreadable: {exc}") from exc
        for sequence, line in enumerate(lines):
            if not line.strip():
                raise WatchStateError("attempt ledger contains a blank record")
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise WatchStateError("attempt ledger contains invalid JSON") from exc
            if not isinstance(record, dict):
                raise WatchStateError("attempt ledger record is not an object")
            self._verify_record(record)
            if record.get("record_seq") != sequence:
                raise WatchStateError("attempt ledger sequence mismatch")
            if record.get("previous_record_hash") != previous:
                raise WatchStateError("attempt ledger previous hash mismatch")
            phase = record.get("phase")
            if phase not in {"STARTED", "TERMINAL"}:
                raise WatchStateError("attempt ledger phase is invalid")
            if phase == "STARTED" and sequence and records[-1].get("phase") != "TERMINAL":
                raise WatchStateError("attempt ledger has overlapping attempts")
            if phase == "TERMINAL":
                if not records or records[-1].get("phase") != "STARTED":
                    raise WatchStateError("terminal attempt has no started predecessor")
                if record.get("attempt_id") != records[-1].get("attempt_id"):
                    raise WatchStateError("terminal attempt id mismatch")
            previous = str(record["record_hash"])
            records.append(record)
        return records

    def _append_record(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        record = _hashed(payload, "record_hash")
        self.paths.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.ledger.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def begin_attempt(
        self,
        *,
        now_ts: int,
        cadence_stage: str,
        interval_sec: int,
    ) -> dict[str, Any]:
        records = self.verify_ledger()
        if records and records[-1].get("phase") != "TERMINAL":
            raise WatchStateError("previous watcher attempt is unfinished")
        attempt_id = (
            "announcement_watch_"
            + datetime.fromtimestamp(int(now_ts), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "_"
            + secrets.token_hex(4)
        )
        return self._append_record({
            "schema": ATTEMPT_SCHEMA,
            "record_seq": len(records),
            "previous_record_hash": records[-1]["record_hash"] if records else None,
            "phase": "STARTED",
            "attempt_id": attempt_id,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "started_at_utc": _iso(now_ts),
            "cadence_stage_start": str(cadence_stage),
            "interval_sec_start": int(interval_sec),
            "capture_authorized": False,
        })

    def record_terminal(
        self,
        *,
        attempt_id: str,
        now_ts: int,
        terminal_status: str,
        cadence_stage: str,
        interval_sec: int,
        pending_retry: bool,
        metadata_status: str | None,
        discovery_status: str | None,
        candidate_status: str | None,
        announcement_requests: int,
        appended_candidates: int,
        reason: str | None,
        failure_stage: str | None = None,
    ) -> dict[str, Any]:
        records = self.verify_ledger()
        if not records or records[-1].get("phase") != "STARTED":
            raise WatchStateError("terminal attempt has no live started record")
        started = records[-1]
        if started.get("attempt_id") != attempt_id:
            raise WatchStateError("terminal attempt does not own the ledger head")
        interval = int(interval_sec)
        if interval <= 0:
            raise WatchStateError("cadence interval must be positive")
        return self._append_record({
            "schema": ATTEMPT_SCHEMA,
            "record_seq": len(records),
            "previous_record_hash": started["record_hash"],
            "phase": "TERMINAL",
            "attempt_id": attempt_id,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "started_at_utc": started["started_at_utc"],
            "finished_at_utc": _iso(now_ts),
            "terminal_status": str(terminal_status),
            "cadence_stage": str(cadence_stage),
            "interval_sec": interval,
            "next_interval_at_utc": _iso(int(now_ts) + interval),
            "pending_retry": bool(pending_retry),
            "metadata_status": metadata_status,
            "discovery_status": discovery_status,
            "candidate_status": candidate_status,
            "announcement_requests": int(announcement_requests),
            "appended_candidates": int(appended_candidates),
            "failure_stage": failure_stage,
            "reason": reason,
            "capture_authorized": False,
        })

    def _state_from_terminal(self, terminal: Mapping[str, Any]) -> dict[str, Any]:
        if terminal.get("phase") != "TERMINAL":
            raise WatchStateError("state requires a terminal attempt record")
        payload = {
            "schema": STATE_SCHEMA,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "status": terminal.get("terminal_status"),
            "cadence_stage": terminal.get("cadence_stage"),
            "interval_sec": terminal.get("interval_sec"),
            "next_interval_at_utc": terminal.get("next_interval_at_utc"),
            "pending_retry": terminal.get("pending_retry"),
            "last_attempt_id": terminal.get("attempt_id"),
            "attempt_ledger_head_hash": terminal.get("record_hash"),
            "last_candidate_store_head_hash": terminal.get("candidate_store_head_hash"),
            "updated_at_utc": terminal.get("finished_at_utc"),
        }
        return _hashed(payload, "state_hash")

    def commit_terminal_state(self, terminal: Mapping[str, Any]) -> dict[str, Any]:
        records = self.verify_ledger()
        if not records or terminal.get("record_hash") != records[-1].get("record_hash"):
            raise WatchStateError("terminal record is not the verified ledger head")
        state = self._state_from_terminal(terminal)
        _write_json_atomic(self.paths.state, state)
        return state

    def _read_state(self) -> dict[str, Any] | None:
        if not self.paths.state.is_file():
            return None
        try:
            state = json.loads(self.paths.state.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WatchStateError(f"watch state is unreadable: {exc}") from exc
        if not isinstance(state, dict):
            raise WatchStateError("watch state is not an object")
        if state.get("schema") != STATE_SCHEMA:
            raise WatchStateError("watch state schema mismatch")
        if state.get("plan_id") != self.plan_id or state.get("plan_hash") != self.plan_hash:
            raise WatchStateError("watch state plan identity mismatch")
        recorded = state.get("state_hash")
        payload = {key: value for key, value in state.items() if key != "state_hash"}
        if not isinstance(recorded, str) or canonical_hash(payload) != recorded:
            raise WatchStateError("watch state hash mismatch")
        _parse_utc(state.get("next_interval_at_utc"))
        return state

    def probe_due(self, *, now_ts: int) -> dict[str, Any]:
        records = self.verify_ledger()
        state = self._read_state()
        if not records and state is None:
            return {"status": "DUE", "next_interval_at_utc": None}
        if not records:
            raise WatchStateError("watch state exists without an attempt ledger")
        tail = records[-1]
        if tail.get("phase") != "TERMINAL":
            return {"status": "CONTROL_RECOVERY_REQUIRED", "next_interval_at_utc": None}
        if state is None or state.get("attempt_ledger_head_hash") != tail.get("record_hash"):
            return {
                "status": "CONTROL_RECOVERY_REQUIRED",
                "next_interval_at_utc": tail.get("next_interval_at_utc"),
            }
        next_ts = _parse_utc(state["next_interval_at_utc"])
        return {
            "status": "DUE" if int(now_ts) >= next_ts else "NOT_DUE",
            "next_interval_at_utc": state["next_interval_at_utc"],
            "cadence_stage": state.get("cadence_stage"),
            "interval_sec": state.get("interval_sec"),
            "pending_retry": state.get("pending_retry"),
        }

    def reconcile_from_ledger(self) -> dict[str, Any]:
        records = self.verify_ledger()
        if not records or records[-1].get("phase") != "TERMINAL":
            raise WatchStateError("no terminal ledger head is available for reconciliation")
        return self.commit_terminal_state(records[-1])

    def recover_control_state(self, *, now_ts: int) -> dict[str, Any]:
        """Reconcile a terminal head or close one interrupted STARTED attempt."""
        records = self.verify_ledger()
        if not records:
            raise WatchStateError("no attempt ledger is available for recovery")
        tail = records[-1]
        if tail.get("phase") == "TERMINAL":
            return self.commit_terminal_state(tail)
        if tail.get("phase") != "STARTED":
            raise WatchStateError("attempt ledger head cannot be recovered")
        interval = int(tail.get("interval_sec_start") or 0)
        if interval <= 0:
            raise WatchStateError("interrupted attempt cadence is invalid")
        terminal = self.record_terminal(
            attempt_id=str(tail["attempt_id"]),
            now_ts=int(now_ts),
            terminal_status="RETRY_NEXT_INTERVAL",
            cadence_stage=str(tail.get("cadence_stage_start") or "SEARCH"),
            interval_sec=interval,
            pending_retry=True,
            metadata_status=None,
            discovery_status=None,
            candidate_status=None,
            announcement_requests=0,
            appended_candidates=0,
            reason="previous watcher process ended before a terminal record",
            failure_stage="interrupted_attempt_recovery",
        )
        return self.commit_terminal_state(terminal)
