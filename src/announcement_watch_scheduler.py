"""Adaptive no-model scheduler for bounded announcement discovery."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from announcement_watch_state import WatchStateError, WatchStateStore
from announcement_watch_state import WatchPaths
import frozen_plan_bindings as trust_root
import project_config as config


def _control_preflight(**kwargs: Any) -> Mapping[str, Any]:
    """Load the control gate only after a wake is known to be due."""
    import risk_gate

    return risk_gate.preflight(**kwargs)


def _metadata_refresh(**kwargs: Any) -> Mapping[str, Any]:
    """Load the network metadata collector only for a due attempt."""
    import event_registry

    return event_registry.refresh(**kwargs)


def _announcement_discovery(**kwargs: Any) -> Mapping[str, Any]:
    """Load announcement discovery only after metadata refresh succeeds."""
    import announcement_discovery

    return announcement_discovery.run_discovery(**kwargs)


def _candidate_inspection(**kwargs: Any) -> Mapping[str, Any]:
    """Load the local candidate reader only at the end of a due attempt."""
    import event_registry

    return event_registry.inspect_capture_candidates(**kwargs)


def _candidate_alert(**kwargs: Any) -> Mapping[str, Any]:
    """Load the local Windows alert runtime only on a due research path."""
    import candidate_alert
    import event_registry
    import risk_gate

    return candidate_alert.process_candidate_alerts(
        **kwargs,
        candidate_store_path=config.ANNOUNCEMENT_CANDIDATE_PATH,
        alert_ledger_path=config.CANDIDATE_ALERT_LEDGER_PATH,
        target_selector=event_registry.select_unattested_crypto_premarket_episodes,
        preflight=risk_gate.preflight,
    )


def _validate_control_receipt(
    receipt: Mapping[str, Any], *, run_id: str
) -> Mapping[str, Any]:
    """Load the exact receipt validator only on a due control path."""
    import risk_gate

    return risk_gate.validate_control_preflight_receipt(receipt, run_id=run_id)


DISCOVERY_SUCCESS_STATUSES = frozenset({
    "NO_ANNOUNCEMENT_TARGETS",
    "NO_MATCHING_ANNOUNCEMENTS",
    "CANDIDATES_RECORDED_HUMAN_ATTESTATION_REQUIRED",
})
CANDIDATE_SUCCESS_STATUSES = frozenset({
    "NO_SECONDS_GRADE_CANDIDATE",
    "SECONDS_GRADE_CANDIDATE_FOUND_NO_CAPTURE_AUTHORITY",
})
ALERT_SUCCESS_STATUSES = frozenset({
    "NO_NEW_CANDIDATE_ALERTS",
    "CANDIDATE_ALERTS_HISTORY_CONFIRMED",
    "CANDIDATE_ALERTS_SUBMITTED_UNCONFIRMED",
    "DELIVERY_UNCERTAIN_NO_AUTO_RETRY",
})


def _fresh_now(clock: Callable[[], float | int], *, floor: int) -> int:
    """Read a fresh wall clock without allowing time to move backwards."""
    return max(int(floor), int(clock()))


def choose_cadence(
    *,
    now_ts: int,
    active_unattested_count: int,
    unverified_candidate_count: int,
    candidate_report: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Select the next discovery cadence from already-collected evidence."""
    official_seen = False
    exact_within_24h = False
    terminal_reasons = {
        "LIFECYCLE_GENERATION_NOT_CURRENT",
    }
    exact_blockers = {
        "OFFICIAL_T0_CONFLICT",
        "ASSET_IDENTITY_CONFLICT",
        "SPOT_SYMBOL_MAPPING_INVALID",
        "OFFICIAL_EVIDENCE_NOT_YET_RECEIVED",
    }
    rows = [
        *(candidate_report.get("candidates") or []),
        *(candidate_report.get("rejections") or []),
    ]
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            continue
        reasons = {
            str(value) for value in (raw_row.get("reasons") or [])
        }
        if reasons & terminal_reasons:
            continue
        if "SOURCE_CLASS_NOT_OFFICIAL_ANNOUNCEMENT" in reasons:
            continue
        if raw_row.get("asset_class") != "CRYPTO_TOKEN":
            continue
        try:
            t0 = int(raw_row.get("official_spot_t0") or 0)
        except (TypeError, ValueError):
            t0 = 0
        if t0 <= int(now_ts):
            continue
        official_seen = True
        precision = raw_row.get("t0_precision_sec")
        if (
            isinstance(precision, int)
            and not isinstance(precision, bool)
            and 0 < precision <= 1
            and t0 <= int(now_ts) + 24 * 60 * 60
            and not reasons & exact_blockers
        ):
            exact_within_24h = True

    if exact_within_24h:
        return {"cadence_stage": "EXACT_T0_WITHIN_24H", "interval_sec": 300}
    if official_seen:
        return {"cadence_stage": "OFFICIAL_CONFIRMED", "interval_sec": 3_600}
    if int(unverified_candidate_count) > 0:
        return {"cadence_stage": "UNVERIFIED_CANDIDATE", "interval_sec": 10_800}
    if int(active_unattested_count) > 0:
        return {"cadence_stage": "ACTIVE_UNATTESTED", "interval_sec": 10_800}
    return {"cadence_stage": "SEARCH", "interval_sec": 21_600}


def _count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, (list, tuple)):
        return len(value)
    return 0


def _terminal_result(
    *,
    store: WatchStateStore,
    attempt_id: str,
    now_ts: int,
    terminal_status: str,
    cadence: Mapping[str, Any],
    pending_retry: bool,
    metadata_status: str | None,
    discovery_status: str | None,
    candidate_status: str | None,
    announcement_requests: int,
    appended_candidates: int,
    reason: str | None,
    failure_stage: str | None = None,
    alert_status: str | None = None,
    submitted_alerts: int = 0,
    history_confirmed_alerts: int = 0,
    alert_ledger_head_hash: str | None = None,
) -> dict[str, Any]:
    terminal = store.record_terminal(
        attempt_id=attempt_id,
        now_ts=now_ts,
        terminal_status=terminal_status,
        cadence_stage=str(cadence["cadence_stage"]),
        interval_sec=int(cadence["interval_sec"]),
        pending_retry=pending_retry,
        metadata_status=metadata_status,
        discovery_status=discovery_status,
        candidate_status=candidate_status,
        announcement_requests=announcement_requests,
        appended_candidates=appended_candidates,
        reason=reason,
        failure_stage=failure_stage,
        alert_status=alert_status,
        submitted_alerts=submitted_alerts,
        history_confirmed_alerts=history_confirmed_alerts,
        alert_ledger_head_hash=alert_ledger_head_hash,
    )
    state = store.commit_terminal_state(terminal)
    return {
        "status": terminal_status,
        "attempt_id": attempt_id,
        "cadence_stage": state["cadence_stage"],
        "interval_sec": state["interval_sec"],
        "next_interval_at_utc": state["next_interval_at_utc"],
        "pending_retry": state["pending_retry"],
        "metadata_status": metadata_status,
        "discovery_status": discovery_status,
        "candidate_status": candidate_status,
        "announcement_requests": announcement_requests,
        "appended_candidates": appended_candidates,
        "alert_status": alert_status,
        "submitted_alerts": submitted_alerts,
        "history_confirmed_alerts": history_confirmed_alerts,
        "alert_ledger_head_hash": alert_ledger_head_hash,
        "failure_stage": failure_stage,
        "reason": reason,
        "capture_authorized": False,
    }


def run_scheduled_tick(
    *,
    now_ts: int,
    store: WatchStateStore,
    control_preflight: Any,
    metadata_refresh: Any,
    announcement_discovery: Any,
    candidate_inspection: Any,
    candidate_alert: Any | None = None,
    clock: Callable[[], float | int] | None = None,
) -> dict[str, Any]:
    """Run one wake. The NOT_DUE path performs reads only and returns immediately."""
    clock_fn = clock or time.time
    try:
        due = store.probe_due(now_ts=int(now_ts))
    except WatchStateError as exc:
        return {
            "status": "CONTROL_STATE_INVALID",
            "reason": str(exc),
            "pending_retry": True,
            "capture_authorized": False,
        }
    if due["status"] == "NOT_DUE":
        return {
            "status": "NOT_DUE",
            "next_interval_at_utc": due.get("next_interval_at_utc"),
            "cadence_stage": due.get("cadence_stage"),
            "interval_sec": due.get("interval_sec"),
            "pending_retry": bool(due.get("pending_retry")),
            "capture_authorized": False,
        }

    starting_cadence = {
        "cadence_stage": due.get("cadence_stage") or "SEARCH",
        "interval_sec": int(due.get("interval_sec") or 21_600),
    }
    preflight_run_id = (
        "announcement_watch_control_"
        + datetime.fromtimestamp(int(now_ts), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + f"_{os.getpid()}"
    )
    try:
        control = control_preflight(
            write_class="announcement_watch_control",
            run_id=preflight_run_id,
        )
        _validate_control_receipt(control, run_id=preflight_run_id)
    except Exception as exc:  # noqa: BLE001 - no unauthorized control write
        return {
            "status": "CONTROL_PREFLIGHT_BLOCKED",
            "reason": f"{type(exc).__name__}: {exc}",
            "pending_retry": True,
            "capture_authorized": False,
        }
    try:
        claim_result = store.acquire_claim(now_ts=int(now_ts))
    except WatchStateError as exc:
        return {
            "status": "CONTROL_CLAIM_INVALID",
            "reason": str(exc),
            "pending_retry": True,
            "capture_authorized": False,
        }
    if claim_result.get("status") != "ACQUIRED":
        return {
            "status": "ALREADY_RUNNING",
            "reason": "watch claim is held by a live or unresolved owner",
            "pending_retry": bool(due.get("pending_retry")),
            "next_interval_at_utc": due.get("next_interval_at_utc"),
            "capture_authorized": False,
        }

    claim = claim_result["claim"]
    release_status = "STOPPED_INCOMPLETE"
    try:
        if due["status"] == "CONTROL_RECOVERY_REQUIRED":
            finish_ts = _fresh_now(clock_fn, floor=int(now_ts))
            reconciled = store.recover_control_state(now_ts=finish_ts)
            recovered_status = str(reconciled.get("status") or "")
            release_status = (
                "RETRY_NEXT_INTERVAL"
                if recovered_status == "RETRY_NEXT_INTERVAL"
                else "CONTROL_STATE_RECONCILED"
            )
            return {
                "status": release_status,
                "next_interval_at_utc": reconciled["next_interval_at_utc"],
                "cadence_stage": reconciled["cadence_stage"],
                "interval_sec": reconciled["interval_sec"],
                "pending_retry": reconciled["pending_retry"],
                "capture_authorized": False,
            }

        started = store.begin_attempt(
            now_ts=int(now_ts),
            cadence_stage=str(starting_cadence["cadence_stage"]),
            interval_sec=int(starting_cadence["interval_sec"]),
        )
        attempt_id = str(started["attempt_id"])
        if claim_result.get("stale_claim_recovered") is True:
            release_status = "RETRY_NEXT_INTERVAL"
            return _terminal_result(
                store=store,
                attempt_id=attempt_id,
                now_ts=_fresh_now(clock_fn, floor=int(now_ts)),
                terminal_status=release_status,
                cadence=starting_cadence,
                pending_retry=True,
                metadata_status=None,
                discovery_status=None,
                candidate_status=None,
                announcement_requests=0,
                appended_candidates=0,
                reason="stale watcher claim archived; deferred without network",
                failure_stage="watch_claim_recovery",
            )

        try:
            metadata = metadata_refresh(run_id=attempt_id)
        except Exception as exc:  # noqa: BLE001 - persist and defer
            metadata = {
                "status": "ERROR",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        metadata_status = str(metadata.get("status") or "") if isinstance(metadata, Mapping) else "INVALID_RESULT"
        if metadata_status != "REFRESH_COMPLETE":
            reason = (
                str(metadata.get("reason") or metadata.get("venue_errors") or metadata_status)
                if isinstance(metadata, Mapping)
                else "metadata refresh returned a non-object"
            )
            release_status = "RETRY_NEXT_INTERVAL"
            return _terminal_result(
                store=store,
                attempt_id=attempt_id,
                now_ts=_fresh_now(clock_fn, floor=int(now_ts)),
                terminal_status=release_status,
                cadence=starting_cadence,
                pending_retry=True,
                metadata_status=metadata_status,
                discovery_status=None,
                candidate_status=None,
                announcement_requests=0,
                appended_candidates=0,
                reason=reason,
                failure_stage="metadata_registry_refresh",
            )

        discovery_ts = _fresh_now(clock_fn, floor=int(now_ts))
        try:
            discovery = announcement_discovery(now_ts=discovery_ts)
        except Exception as exc:  # noqa: BLE001 - persist and defer
            discovery = {
                "status": "RETRY_NEXT_INTERVAL",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        if not isinstance(discovery, Mapping):
            discovery = {
                "status": "RETRY_NEXT_INTERVAL",
                "reason": "announcement discovery returned a non-object",
            }
        discovery_status = str(discovery.get("status") or "")
        requests = _count(discovery.get("announcement_requests"))
        appended = _count(discovery.get("appended_candidates"))

        alert_ts = _fresh_now(clock_fn, floor=discovery_ts)
        if candidate_alert is None:
            alert_report: Mapping[str, Any] = {
                "status": "NO_NEW_CANDIDATE_ALERTS",
                "submitted_alerts": 0,
                "history_confirmed_alerts": 0,
            }
        else:
            try:
                raw_alert = candidate_alert(now_ts=alert_ts, run_id=attempt_id)
            except Exception as exc:  # noqa: BLE001 - persist and defer before retry
                raw_alert = {
                    "status": "CANDIDATE_ALERT_RETRY_NEXT_INTERVAL",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            alert_report = (
                raw_alert
                if isinstance(raw_alert, Mapping)
                else {
                    "status": "CANDIDATE_ALERT_RETRY_NEXT_INTERVAL",
                    "reason": "candidate alert returned a non-object",
                }
            )
        alert_status = str(alert_report.get("status") or "")
        submitted_alerts = _count(alert_report.get("submitted_alerts"))
        history_confirmed_alerts = _count(
            alert_report.get("history_confirmed_alerts")
        )
        alert_ledger_head_hash = alert_report.get("alert_ledger_head_hash")
        if alert_status not in ALERT_SUCCESS_STATUSES:
            release_status = "PARTIAL_RETRY_NEXT_INTERVAL"
            return _terminal_result(
                store=store,
                attempt_id=attempt_id,
                now_ts=_fresh_now(clock_fn, floor=alert_ts),
                terminal_status=release_status,
                cadence=starting_cadence,
                pending_retry=True,
                metadata_status=metadata_status,
                discovery_status=discovery_status,
                candidate_status=None,
                announcement_requests=requests,
                appended_candidates=appended,
                alert_status=alert_status,
                submitted_alerts=submitted_alerts,
                history_confirmed_alerts=history_confirmed_alerts,
                alert_ledger_head_hash=(
                    str(alert_ledger_head_hash) if alert_ledger_head_hash else None
                ),
                reason=str(
                    alert_report.get("reason")
                    or alert_status
                    or "unknown candidate alert status"
                ),
                failure_stage="candidate_alert",
            )
        if discovery_status not in DISCOVERY_SUCCESS_STATUSES:
            release_status = "PARTIAL_RETRY_NEXT_INTERVAL"
            return _terminal_result(
                store=store,
                attempt_id=attempt_id,
                now_ts=_fresh_now(clock_fn, floor=discovery_ts),
                terminal_status=release_status,
                cadence=starting_cadence,
                pending_retry=True,
                metadata_status=metadata_status,
                discovery_status=discovery_status,
                candidate_status=None,
                announcement_requests=requests,
                appended_candidates=appended,
                alert_status=alert_status,
                submitted_alerts=submitted_alerts,
                history_confirmed_alerts=history_confirmed_alerts,
                alert_ledger_head_hash=(
                    str(alert_ledger_head_hash) if alert_ledger_head_hash else None
                ),
                reason=str(
                    discovery.get("reason")
                    or discovery_status
                    or "unknown announcement discovery status"
                ),
                failure_stage="announcement_discovery",
            )

        inspection_ts = _fresh_now(clock_fn, floor=discovery_ts)
        try:
            candidate_report = candidate_inspection(
                now_ts=inspection_ts, horizon_sec=24 * 60 * 60
            )
        except Exception as exc:  # noqa: BLE001 - local authority read failed
            candidate_report = {
                "status": "CANDIDATE_INSPECTION_FAILED",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        if (
            not isinstance(candidate_report, Mapping)
            or candidate_report.get("status") not in CANDIDATE_SUCCESS_STATUSES
        ):
            candidate_status = (
                str(candidate_report.get("status") or "INVALID_RESULT")
                if isinstance(candidate_report, Mapping)
                else "INVALID_RESULT"
            )
            release_status = "PARTIAL_RETRY_NEXT_INTERVAL"
            return _terminal_result(
                store=store,
                attempt_id=attempt_id,
                now_ts=_fresh_now(clock_fn, floor=inspection_ts),
                terminal_status=release_status,
                cadence=starting_cadence,
                pending_retry=True,
                metadata_status=metadata_status,
                discovery_status=discovery_status,
                candidate_status=candidate_status,
                announcement_requests=requests,
                appended_candidates=appended,
                alert_status=alert_status,
                submitted_alerts=submitted_alerts,
                history_confirmed_alerts=history_confirmed_alerts,
                alert_ledger_head_hash=(
                    str(alert_ledger_head_hash) if alert_ledger_head_hash else None
                ),
                reason=(
                    str(candidate_report.get("reason") or candidate_status)
                    if isinstance(candidate_report, Mapping)
                    else "candidate inspection returned a non-object"
                ),
                failure_stage="candidate_inspection",
            )

        cadence = choose_cadence(
            now_ts=inspection_ts,
            active_unattested_count=_count(discovery.get("targets")),
            unverified_candidate_count=_count(discovery.get("matched_candidates")),
            candidate_report=candidate_report,
        )
        release_status = "COMPLETE"
        return _terminal_result(
            store=store,
            attempt_id=attempt_id,
            now_ts=_fresh_now(clock_fn, floor=inspection_ts),
            terminal_status=release_status,
            cadence=cadence,
            pending_retry=False,
            metadata_status=metadata_status,
            discovery_status=discovery_status,
            candidate_status=str(candidate_report.get("status")),
            announcement_requests=requests,
            appended_candidates=appended,
            alert_status=alert_status,
            submitted_alerts=submitted_alerts,
            history_confirmed_alerts=history_confirmed_alerts,
            alert_ledger_head_hash=(
                str(alert_ledger_head_hash) if alert_ledger_head_hash else None
            ),
            reason=None,
        )
    finally:
        store.release_claim(
            claim,
            terminal_status=release_status,
            now_ts=int(now_ts),
        )


def default_store() -> WatchStateStore:
    return WatchStateStore(
        WatchPaths(
            state=config.ANNOUNCEMENT_STATE_PATH,
            ledger=config.ANNOUNCEMENT_ATTEMPTS_PATH,
            claim=config.ANNOUNCEMENT_WATCH_CLAIM_PATH,
            claim_archive=config.ANNOUNCEMENT_WATCH_CLAIM_ARCHIVE,
        ),
        plan_id=trust_root.PLAN_ID,
        plan_hash=trust_root.PLAN_HASH,
    )


def scheduler_status(*, store: WatchStateStore, now_ts: int) -> dict[str, Any]:
    try:
        result = store.probe_due(now_ts=now_ts)
    except WatchStateError as exc:
        return {
            "status": "CONTROL_STATE_INVALID",
            "reason": str(exc),
            "capture_authorized": False,
        }
    return {**result, "capture_authorized": False}


def _is_error_status(status: object) -> bool:
    value = str(status or "")
    if value in {"RETRY_NEXT_INTERVAL", "PARTIAL_RETRY_NEXT_INTERVAL"}:
        return True
    return value.startswith("CONTROL_") and value != "CONTROL_STATE_RECONCILED"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Quiet adaptive scheduler for official listing discovery."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scheduled-tick", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store = default_store()
    now_ts = int(time.time())
    if args.status:
        result = scheduler_status(store=store, now_ts=now_ts)
    else:
        result = run_scheduled_tick(
            now_ts=now_ts,
            store=store,
            control_preflight=_control_preflight,
            metadata_refresh=_metadata_refresh,
            announcement_discovery=_announcement_discovery,
            candidate_inspection=_candidate_inspection,
            candidate_alert=_candidate_alert,
        )
    is_error = _is_error_status(result.get("status"))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    elif is_error:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 1 if is_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
