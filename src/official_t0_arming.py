"""Seal one seconds-grade official spot t0 without granting capture authority.

The human attestation and the arming checkpoint are intentionally separate.  The
attestation records what a person read in an official announcement; this module then
re-reads the verified registry, confirms the event is still current and far enough in
the future, and creates one immutable no-capture receipt.  It performs no network I/O,
does not mint a capture token, and does not create an event-bound PlanOnly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import time
import unicodedata
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from canonical_hash import canonical_hash, canonical_json_bytes
from announcement_watch_state import process_is_alive
import event_registry as registry
import frozen_plan_bindings as trust_root
import project_config as config
import risk_gate


ARMING_SCHEMA = "premarket_official_t0_arming_receipt_v1"
ARMING_RESULT_SCHEMA = "premarket_official_t0_arming_result_v1"
ARMING_VERIFY_SCHEMA = "premarket_official_t0_arming_verification_v1"
ARMING_RECORD_TYPE = "official_t0_arming_receipt"
ARMED_STATUS = "ARMED_NO_CAPTURE_AUTHORITY"
ALREADY_ARMED_STATUS = "ALREADY_ARMED_NO_CAPTURE_AUTHORITY"
ARMING_ACTION = (
    "seal one human-attested exact seconds-grade official spot t0 "
    "as immutable no-capture arming evidence"
)
ARMING_ROOT = getattr(
    config,
    "OFFICIAL_T0_ARMING_ROOT",
    config.PROJECT_ROOT / "docs/arming/official-t0-v1",
)

_SHA_ANCHOR_FIELDS = frozenset(
    {
        "official_record_hash",
        "registry_sha256",
        "registry_tail_record_hash",
        "mutation_receipt_hash",
        "summary_content_sha256",
        "registry_authority_state_hash",
        "plan_hash",
        "asset_identity_hash",
    }
)
_TEXT_ANCHOR_FIELDS = frozenset(
    {
        "episode_id",
        "venue",
        "listing_venue",
        "premarket_contract_id",
        "spot_symbol",
        "t0_source_class",
        "official_source_url",
        "official_source_identity",
        "plan_id",
        "asset_class",
        "issuer_namespace",
        "issuer_id",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "record_type",
        "arming_id",
        "revision",
        "supersedes_arming_receipt_hash",
        "status",
        "run_id",
        "armed_at_utc",
        "armed_by",
        "lead_sec_at_arming",
        "plan_id",
        "plan_hash",
        "resolved_paths_hash",
        "event_anchor",
        "capture_authorized",
        "capture_token_issued",
        "event_bound_plan_generated",
        "receipt_hash",
    }
)


class ArmingError(RuntimeError):
    """The event, preflight or immutable arming chain failed closed."""


def _is_canonical_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not any(
            unicodedata.category(character) in {"Cc", "Cf"}
            for character in value
        )
    )


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _utc_iso(timestamp: int) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _is_explicit_utc(value: Any) -> bool:
    if not _is_canonical_text(value):
        return False
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return moment.tzinfo is not None and moment.utcoffset() == timezone.utc.utcoffset(None)


def _arming_id(episode_id: str) -> str:
    digest = hashlib.sha256(episode_id.encode("utf-8")).hexdigest()
    return f"arming-{digest[:32]}"


def _stream_dir(root: Path, episode_id: str) -> Path:
    return root / _arming_id(episode_id)


def _receipt_hash(payload: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "receipt_hash"}
    return canonical_hash(unsigned)


def _known_plan_identities() -> frozenset[tuple[str, str]]:
    entries = [trust_root.ACTIVE_PLAN, *trust_root.RETIRED_PLANS]
    return frozenset(
        (str(entry.get("plan_id") or ""), str(entry.get("plan_hash") or ""))
        for entry in entries
        if isinstance(entry, Mapping)
    )


def _validate_anchor(
    anchor: Mapping[str, Any], *, require_active_plan: bool = False
) -> None:
    expected_fields = frozenset(config.CAPTURE_LINEAGE_FIELDS)
    if frozenset(anchor) != expected_fields:
        missing = sorted(expected_fields - frozenset(anchor))
        extra = sorted(frozenset(anchor) - expected_fields)
        raise ArmingError(
            f"event anchor field set is invalid; missing={missing}, extra={extra}"
        )
    for field in _TEXT_ANCHOR_FIELDS:
        if not _is_canonical_text(anchor.get(field)):
            raise ArmingError(f"event anchor {field} is missing or non-canonical")
    for field in _SHA_ANCHOR_FIELDS:
        if not _is_sha256(anchor.get(field)):
            raise ArmingError(f"event anchor {field} is missing or invalid")
    if not _is_nonnegative_int(anchor.get("mutation_receipt_seq")):
        raise ArmingError("event anchor mutation_receipt_seq is missing or invalid")
    if not _is_positive_int(anchor.get("official_spot_t0")):
        raise ArmingError("event anchor official_spot_t0 is missing or invalid")
    if anchor.get("t0_precision_sec") != 1:
        raise ArmingError("event anchor requires exact seconds-grade t0_precision_sec=1")
    if anchor.get("t0_source_class") != registry.SOURCE_OFFICIAL_ANNOUNCEMENT:
        raise ArmingError("event anchor requires OFFICIAL_ANNOUNCEMENT provenance")
    if anchor.get("asset_class") != registry.ASSET_CLASS_CRYPTO_TOKEN:
        raise ArmingError("event anchor requires the explicit CRYPTO_TOKEN asset class")
    plan_identity = (str(anchor.get("plan_id") or ""), str(anchor.get("plan_hash") or ""))
    if plan_identity not in _known_plan_identities():
        raise ArmingError("event anchor PlanOnly identity is not trusted")
    if require_active_plan and plan_identity != (trust_root.PLAN_ID, trust_root.PLAN_HASH):
        raise ArmingError("event anchor is not bound to the active PlanOnly")
    if not registry.market_symbols_equivalent(
        str(anchor["venue"]),
        anchor["premarket_contract_id"],
        anchor["spot_symbol"],
    ):
        raise ArmingError("event anchor contract-to-spot mapping is not exact")


def _event_anchor(event: Mapping[str, Any]) -> dict[str, Any]:
    missing = [field for field in config.CAPTURE_LINEAGE_FIELDS if field not in event]
    if missing:
        raise ArmingError(f"selected event lacks arming lineage fields: {missing}")
    try:
        anchor = json.loads(
            canonical_json_bytes(
                {field: event[field] for field in config.CAPTURE_LINEAGE_FIELDS}
            ).decode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise ArmingError("selected event lineage is not canonical JSON") from exc
    _validate_anchor(anchor, require_active_plan=True)
    return anchor


def _select_one_event(
    selector: Callable[..., Sequence[Mapping[str, Any]]],
    *,
    now_ts: int,
    episode_id: str,
) -> Mapping[str, Any]:
    try:
        selected = selector(now_ts=now_ts)
    except Exception as exc:  # noqa: BLE001 - authority selection must fail closed
        raise ArmingError(f"official event selector failed: {type(exc).__name__}: {exc}") from exc
    if isinstance(selected, (str, bytes, bytearray)) or not isinstance(
        selected, Sequence
    ):
        raise ArmingError("official event selector did not return a sequence")
    matches = [
        item
        for item in selected
        if isinstance(item, Mapping) and item.get("episode_id") == episode_id
    ]
    if len(matches) != 1:
        raise ArmingError(
            "official event selector must return exactly one matching episode"
        )
    return matches[0]


def _validate_expected_event(
    event: Mapping[str, Any],
    *,
    now_ts: int,
    episode_id: str,
    expected_official_record_hash: str,
    expected_official_t0: int,
    expected_contract: str,
    expected_spot_symbol: str,
) -> dict[str, Any]:
    if event.get("t0_source_class") != registry.SOURCE_OFFICIAL_ANNOUNCEMENT:
        raise ArmingError("arming requires OFFICIAL_ANNOUNCEMENT provenance")
    if event.get("official_record_hash") != expected_official_record_hash:
        raise ArmingError("selected official record does not match operator confirmation")
    selected_t0 = event.get("official_spot_t0")
    if not _is_positive_int(selected_t0):
        raise ArmingError("selected official t0 is invalid")
    if int(selected_t0) - now_ts < config.CAPTURE_WINDOW_BEFORE_SEC:
        raise ArmingError(
            "official t0 is too close to preserve the full pre-listing window"
        )
    expected = {
        "episode_id": episode_id,
        "official_spot_t0": expected_official_t0,
        "premarket_contract_id": expected_contract,
        "spot_symbol": expected_spot_symbol,
    }
    mismatched = [field for field, value in expected.items() if event.get(field) != value]
    if mismatched:
        raise ArmingError(
            "selected official event does not match operator confirmation: "
            + ", ".join(mismatched)
        )
    if event.get("t0_precision_sec") != 1:
        raise ArmingError("arming requires exact seconds-grade t0_precision_sec=1")
    if event.get("asset_class") != registry.ASSET_CLASS_CRYPTO_TOKEN:
        raise ArmingError("arming requires the explicit CRYPTO_TOKEN asset class")
    return _event_anchor(event)


def _validate_preflight(
    decision: Mapping[str, Any], *, run_id: str
) -> dict[str, Any]:
    if not isinstance(decision, Mapping):
        raise ArmingError("official t0 arming preflight did not return an object")
    expectations = {
        "schema": "premarket_write_preflight_v2",
        "ok": True,
        "verified": True,
        "decision": "ALLOW_OFFICIAL_T0_ARMING",
        "write_class": "official_t0_arming",
        "run_id": run_id,
        "action": ARMING_ACTION,
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
    }
    mismatched = [
        field for field, expected in expectations.items() if decision.get(field) != expected
    ]
    if mismatched:
        raise ArmingError(
            "official t0 arming preflight is not exact: " + ", ".join(mismatched)
        )
    if not _is_sha256(decision.get("resolved_paths_hash")):
        raise ArmingError("official t0 arming preflight resolved_paths_hash is invalid")
    if "capture_token" in decision or decision.get("capture_token_issued") is True:
        raise ArmingError("official t0 arming preflight must never issue a capture token")
    return dict(decision)


def _validate_receipt(record: Mapping[str, Any]) -> dict[str, Any]:
    if frozenset(record) != _RECEIPT_FIELDS:
        missing = sorted(_RECEIPT_FIELDS - frozenset(record))
        extra = sorted(frozenset(record) - _RECEIPT_FIELDS)
        raise ArmingError(
            f"arming receipt field set is invalid; missing={missing}, extra={extra}"
        )
    if record.get("schema") != ARMING_SCHEMA:
        raise ArmingError("arming receipt schema is invalid")
    if record.get("record_type") != ARMING_RECORD_TYPE:
        raise ArmingError("arming receipt record_type is invalid")
    if record.get("status") != ARMED_STATUS:
        raise ArmingError("arming receipt is not armed")
    for field in ("arming_id", "run_id", "armed_at_utc", "armed_by", "plan_id"):
        if not _is_canonical_text(record.get(field)):
            raise ArmingError(f"arming receipt {field} is missing or non-canonical")
    if not _is_explicit_utc(record.get("armed_at_utc")):
        raise ArmingError("arming receipt armed_at_utc is not explicit UTC")
    if not _is_nonnegative_int(record.get("revision")):
        raise ArmingError("arming receipt revision is invalid")
    if not _is_nonnegative_int(record.get("lead_sec_at_arming")):
        raise ArmingError("arming receipt lead_sec_at_arming is invalid")
    if int(record["lead_sec_at_arming"]) < config.CAPTURE_WINDOW_BEFORE_SEC:
        raise ArmingError("arming receipt does not preserve the full pre-listing window")
    plan_identity = (str(record.get("plan_id") or ""), str(record.get("plan_hash") or ""))
    if plan_identity not in _known_plan_identities():
        raise ArmingError("arming receipt PlanOnly identity is not trusted")
    if not _is_sha256(record.get("plan_hash")) or not _is_sha256(
        record.get("resolved_paths_hash")
    ):
        raise ArmingError("arming receipt PlanOnly lineage is invalid")
    supersedes = record.get("supersedes_arming_receipt_hash")
    if supersedes is not None and not _is_sha256(supersedes):
        raise ArmingError("arming receipt supersedes hash is invalid")
    if record.get("capture_authorized") is not False:
        raise ArmingError("arming receipt must not authorize capture")
    if record.get("capture_token_issued") is not False:
        raise ArmingError("arming receipt must not issue a capture token")
    if record.get("event_bound_plan_generated") is not False:
        raise ArmingError("arming receipt must not claim an event-bound plan")
    anchor = record.get("event_anchor")
    if not isinstance(anchor, Mapping):
        raise ArmingError("arming receipt event_anchor is missing")
    _validate_anchor(anchor)
    expected_arming_id = _arming_id(str(anchor["episode_id"]))
    if record.get("arming_id") != expected_arming_id:
        raise ArmingError("arming receipt arming_id does not match event_anchor episode_id")
    armed_at = datetime.fromisoformat(str(record["armed_at_utc"]).replace("Z", "+00:00"))
    armed_at_timestamp = armed_at.timestamp()
    if not armed_at_timestamp.is_integer():
        raise ArmingError("arming receipt armed_at_utc must have exact second precision")
    expected_lead = int(anchor["official_spot_t0"]) - int(armed_at_timestamp)
    if record.get("lead_sec_at_arming") != expected_lead:
        raise ArmingError(
            "arming receipt lead_sec_at_arming does not match official t0 and armed_at_utc"
        )
    if (
        anchor.get("plan_id") != record.get("plan_id")
        or anchor.get("plan_hash") != record.get("plan_hash")
    ):
        raise ArmingError("arming receipt and event anchor PlanOnly identity differ")
    if record.get("receipt_hash") != _receipt_hash(record):
        raise ArmingError("arming receipt hash is invalid")
    return dict(record)


def load_arming_receipt(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load and fully verify one immutable arming receipt."""
    receipt_path = Path(path)
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArmingError(f"arming receipt is unreadable: {receipt_path}") from exc
    if not isinstance(payload, Mapping):
        raise ArmingError("arming receipt root is not an object")
    return _validate_receipt(payload)


def validate_arming_receipt(record: Mapping[str, Any]) -> dict[str, Any]:
    """Verify an in-memory receipt against active-or-retired immutable lineage."""
    if not isinstance(record, Mapping):
        raise ArmingError("arming receipt root is not an object")
    return _validate_receipt(record)


def _load_stream(root: Path, episode_id: str) -> list[tuple[Path, dict[str, Any]]]:
    stream_dir = _stream_dir(root, episode_id)
    if not stream_dir.exists():
        return []
    if not stream_dir.is_dir():
        raise ArmingError("arming stream path is not a directory")
    loaded: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(stream_dir.glob("*.json")):
        record = load_arming_receipt(path)
        loaded.append((path, record))
    expected_id = _arming_id(episode_id)
    previous_hash: str | None = None
    for expected_revision, (path, record) in enumerate(loaded):
        if record["arming_id"] != expected_id:
            raise ArmingError("arming stream contains a different arming_id")
        if record["event_anchor"]["episode_id"] != episode_id:
            raise ArmingError("arming stream contains a different episode_id")
        if record["revision"] != expected_revision:
            raise ArmingError("arming receipt revision chain has a gap or reorder")
        if record["supersedes_arming_receipt_hash"] != previous_hash:
            raise ArmingError("arming receipt supersedes chain is invalid")
        expected_name = f"{expected_revision:020d}-{record['receipt_hash']}.json"
        if path.name != expected_name:
            raise ArmingError("arming receipt filename does not match its revision/hash")
        previous_hash = str(record["receipt_hash"])
    return loaded


def load_current_arming(
    episode_id: str, *, arming_root: str | os.PathLike[str] | None = None
) -> dict[str, Any] | None:
    """Return the verified current receipt for one episode, if present."""
    if not _is_canonical_text(episode_id):
        raise ArmingError("episode_id is missing or non-canonical")
    loaded = _load_stream(Path(arming_root or ARMING_ROOT), episode_id)
    return dict(loaded[-1][1]) if loaded else None


@contextmanager
def guard_current_arming_head(
    *,
    episode_id: str,
    expected_receipt_hash: str,
    run_id: str,
    arming_root: str | os.PathLike[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Hold the arming writer lock while a consumer uses the verified current head."""
    if not _is_canonical_text(episode_id):
        raise ArmingError("episode_id is missing or non-canonical")
    if not _is_sha256(expected_receipt_hash):
        raise ArmingError("expected arming receipt hash is invalid")
    if not _is_canonical_text(run_id):
        raise ArmingError("run_id is missing or non-canonical")
    root = Path(arming_root or ARMING_ROOT)
    with _arming_lock(root, run_id=run_id):
        current = load_current_arming(episode_id, arming_root=root)
        if current is None or current.get("receipt_hash") != expected_receipt_hash:
            raise ArmingError("supplied receipt is not the current arming head")
        yield current


_LOCK_FIELDS = frozenset({"schema", "run_id", "owner_pid", "owner_host", "nonce"})
_LOCK_SCHEMA = "premarket_official_t0_arming_lock_v1"
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


def _validated_lock_payload(raw: bytes) -> dict[str, Any]:
    try:
        decoded = raw.decode("utf-8")
        parsed = json.loads(decoded)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArmingError("OFFICIAL_T0_ARMING_LOCKED: lock is unreadable") from exc
    if not isinstance(parsed, dict) or frozenset(parsed) != _LOCK_FIELDS:
        raise ArmingError("OFFICIAL_T0_ARMING_LOCKED: lock identity is invalid")
    if canonical_json_bytes(parsed) + b"\n" != raw:
        raise ArmingError("OFFICIAL_T0_ARMING_LOCKED: lock is not canonical")
    if parsed.get("schema") != _LOCK_SCHEMA:
        raise ArmingError("OFFICIAL_T0_ARMING_LOCKED: lock schema is invalid")
    if not _is_canonical_text(parsed.get("run_id")):
        raise ArmingError("OFFICIAL_T0_ARMING_LOCKED: lock run_id is invalid")
    owner_pid = parsed.get("owner_pid")
    if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0:
        raise ArmingError("OFFICIAL_T0_ARMING_LOCKED: lock owner_pid is invalid")
    if not _is_canonical_text(parsed.get("owner_host")):
        raise ArmingError("OFFICIAL_T0_ARMING_LOCKED: lock owner_host is invalid")
    nonce = parsed.get("nonce")
    if not isinstance(nonce, str) or _HEX_64.fullmatch(nonce) is None:
        raise ArmingError("OFFICIAL_T0_ARMING_LOCKED: lock nonce is invalid")
    return dict(parsed)


def _recover_dead_same_host_lock(root: Path, lock_path: Path) -> None:
    """Losslessly archive one conclusively dead local lock, or fail closed."""
    try:
        raw = lock_path.read_bytes()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ArmingError(
            f"OFFICIAL_T0_ARMING_LOCKED: cannot read lock: {exc}"
        ) from exc
    payload = _validated_lock_payload(raw)
    if payload["owner_host"] != socket.gethostname():
        raise ArmingError("OFFICIAL_T0_ARMING_LOCKED: lock belongs to another host")
    if process_is_alive(payload["owner_pid"]) is not False:
        raise ArmingError("OFFICIAL_T0_ARMING_LOCKED: lock owner is live or uncertain")

    archive_root = root.parent / f"{root.name}.lock-archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_path = archive_root / f"{hashlib.sha256(raw).hexdigest()}.json"
    try:
        # A hard link is the recovery CAS: only one contender can reserve this exact
        # stale inode.  A second contender fails before it can touch a replacement.
        os.link(lock_path, archive_path)
    except FileExistsError:
        try:
            same_archived_inode = (
                not archive_path.is_symlink()
                and os.path.samefile(lock_path, archive_path)
            )
        except OSError as exc:
            raise ArmingError(
                "OFFICIAL_T0_ARMING_LOCKED: stale-lock archive cannot be verified"
            ) from exc
        if not same_archived_inode:
            raise ArmingError(
                "OFFICIAL_T0_ARMING_LOCKED: stale-lock archive conflicts"
            )
    except OSError as exc:
        raise ArmingError(
            f"OFFICIAL_T0_ARMING_LOCKED: stale-lock archive failed: {exc}"
        ) from exc
    try:
        if lock_path.read_bytes() != raw or archive_path.read_bytes() != raw:
            raise ArmingError(
                "OFFICIAL_T0_ARMING_LOCKED: lock changed during recovery"
            )
        lock_path.unlink()
    except BaseException:
        # The archive is immutable recovery evidence.  Never remove it on an
        # uncertain race; the remaining lock continues to fail closed.
        raise


@contextmanager
def _arming_lock(root: Path, *, run_id: str) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".official-t0-arming.lock"
    lock_payload = {
        "schema": _LOCK_SCHEMA,
        "run_id": run_id,
        "owner_pid": os.getpid(),
        "owner_host": socket.gethostname(),
        "nonce": secrets.token_hex(32),
    }
    lock_bytes = canonical_json_bytes(lock_payload) + b"\n"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        _recover_dead_same_host_lock(root, lock_path)
        try:
            descriptor = os.open(
                lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError as exc:
            raise ArmingError(
                f"OFFICIAL_T0_ARMING_LOCKED: lock was reacquired: {lock_path}"
            ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(lock_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        try:
            current = lock_path.read_bytes()
        except FileNotFoundError as exc:
            raise ArmingError("official t0 arming lock ownership was lost") from exc
        except OSError as exc:
            raise ArmingError(
                f"official t0 arming lock ownership cannot be verified: {exc}"
            ) from exc
        if current != lock_bytes:
            raise ArmingError("official t0 arming lock ownership changed")
        lock_path.unlink()


def _write_receipt(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ArmingError(f"arming receipt already exists: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(record) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise


def _result(
    record: Mapping[str, Any], *, status: str, receipt_path: Path
) -> dict[str, Any]:
    return {
        "schema": ARMING_RESULT_SCHEMA,
        "status": status,
        "arming_id": record["arming_id"],
        "revision": record["revision"],
        "episode_id": record["event_anchor"]["episode_id"],
        "official_spot_t0": record["event_anchor"]["official_spot_t0"],
        "official_record_hash": record["event_anchor"]["official_record_hash"],
        "receipt_hash": record["receipt_hash"],
        "receipt_path": str(receipt_path.resolve(strict=False)),
        "capture_authorized": False,
        "capture_token_issued": False,
        "event_bound_plan_generated": False,
    }


def arm_official_t0(
    *,
    now_ts: int,
    run_id: str,
    episode_id: str,
    expected_official_record_hash: str,
    expected_official_t0: int,
    expected_contract: str,
    expected_spot_symbol: str,
    expected_current_arming_receipt_hash: str | None = None,
    armed_by: str,
    acknowledge_no_capture_authority: bool,
    arming_root: str | os.PathLike[str] | None = None,
    event_selector: Callable[..., Sequence[Mapping[str, Any]]] | None = None,
    preflight: Callable[..., Mapping[str, Any]] | None = None,
    clock: Callable[[], float | int] | None = None,
) -> dict[str, Any]:
    """Seal a verified official event as immutable, explicitly non-authorizing evidence."""
    if acknowledge_no_capture_authority is not True:
        raise ArmingError("explicit acknowledge_no_capture_authority is required")
    if not _is_nonnegative_int(now_ts):
        raise ArmingError("now_ts must be a non-negative integer")
    if not _is_canonical_text(run_id):
        raise ArmingError("run_id is missing or non-canonical")
    if not _is_canonical_text(episode_id):
        raise ArmingError("episode_id is missing or non-canonical")
    if not _is_sha256(expected_official_record_hash):
        raise ArmingError("expected official record hash is invalid")
    if not _is_positive_int(expected_official_t0):
        raise ArmingError("expected official t0 is invalid")
    for field, value in (
        ("expected_contract", expected_contract),
        ("expected_spot_symbol", expected_spot_symbol),
        ("armed_by", armed_by),
    ):
        if not _is_canonical_text(value):
            raise ArmingError(f"{field} is missing or non-canonical")
    if (
        expected_current_arming_receipt_hash is not None
        and not _is_sha256(expected_current_arming_receipt_hash)
    ):
        raise ArmingError("expected current arming receipt hash is invalid")

    selector = event_selector or registry.events_for_arming
    first_event = _select_one_event(selector, now_ts=now_ts, episode_id=episode_id)
    first_anchor = _validate_expected_event(
        first_event,
        now_ts=now_ts,
        episode_id=episode_id,
        expected_official_record_hash=expected_official_record_hash,
        expected_official_t0=expected_official_t0,
        expected_contract=expected_contract,
        expected_spot_symbol=expected_spot_symbol,
    )
    preflight_call = preflight or risk_gate.preflight
    try:
        preflight_result = preflight_call(
            write_class="official_t0_arming", run_id=run_id
        )
    except Exception as exc:  # noqa: BLE001 - evidence write must fail closed
        raise ArmingError(
            f"official t0 arming preflight failed: {type(exc).__name__}: {exc}"
        ) from exc
    exact_preflight = _validate_preflight(preflight_result, run_id=run_id)

    root = Path(arming_root or ARMING_ROOT)
    with _arming_lock(root, run_id=run_id):
        commit_ts = max(int(now_ts), int((clock or time.time)()))
        # Re-select under the arming lock using a fresh clock.  This catches lifecycle,
        # lineage, plan or timing drift while preflight/lock acquisition was in flight.
        commit_event = _select_one_event(
            selector, now_ts=commit_ts, episode_id=episode_id
        )
        commit_anchor = _validate_expected_event(
            commit_event,
            now_ts=commit_ts,
            episode_id=episode_id,
            expected_official_record_hash=expected_official_record_hash,
            expected_official_t0=expected_official_t0,
            expected_contract=expected_contract,
            expected_spot_symbol=expected_spot_symbol,
        )
        if commit_anchor != first_anchor:
            raise ArmingError("official event authority changed during arming preflight")
        try:
            commit_preflight = preflight_call(
                write_class="official_t0_arming", run_id=run_id
            )
        except Exception as exc:  # noqa: BLE001 - fail closed under the lock
            raise ArmingError(
                f"official t0 arming commit preflight failed: {type(exc).__name__}: {exc}"
            ) from exc
        exact_commit_preflight = _validate_preflight(
            commit_preflight, run_id=run_id
        )
        authority_fields = ("plan_id", "plan_hash", "resolved_paths_hash")
        if any(
            exact_commit_preflight.get(field) != exact_preflight.get(field)
            for field in authority_fields
        ):
            raise ArmingError("official t0 arming authority changed under the lock")

        registry_guard = (
            registry.registry_lock(
                registry.REGISTRY_LOCK_PATH,
                run_id=f"official-t0-arming-{run_id}",
                plan_hash=trust_root.PLAN_HASH,
            )
            if event_selector is None
            else nullcontext()
        )
        try:
            with registry_guard as registry_lock_owner:
                # Re-select after commit preflight while holding the production
                # registry writer lock.  The selected lineage therefore stays current
                # until the O_EXCL receipt is durable.
                final_selection_ts = max(commit_ts, int((clock or time.time)()))
                if event_selector is None:
                    def final_selector(**kwargs: Any) -> Sequence[Mapping[str, Any]]:
                        return registry.events_for_arming(
                            **kwargs,
                            _registry_lock_owner=registry_lock_owner,
                        )
                else:
                    final_selector = selector
                final_event = _select_one_event(
                    final_selector,
                    now_ts=final_selection_ts,
                    episode_id=episode_id,
                )
                final_anchor = _validate_expected_event(
                    final_event,
                    now_ts=final_selection_ts,
                    episode_id=episode_id,
                    expected_official_record_hash=expected_official_record_hash,
                    expected_official_t0=expected_official_t0,
                    expected_contract=expected_contract,
                    expected_spot_symbol=expected_spot_symbol,
                )
                if final_anchor != commit_anchor or final_anchor != first_anchor:
                    raise ArmingError(
                        "official event authority changed after arming commit preflight"
                    )
                final_write_ts = max(
                    final_selection_ts, int((clock or time.time)())
                )
                # Revalidate the held immutable event at the actual write clock.  This
                # catches freshness/window expiry during the final selector itself.
                final_anchor_at_write = _validate_expected_event(
                    final_event,
                    now_ts=final_write_ts,
                    episode_id=episode_id,
                    expected_official_record_hash=expected_official_record_hash,
                    expected_official_t0=expected_official_t0,
                    expected_contract=expected_contract,
                    expected_spot_symbol=expected_spot_symbol,
                )
                if final_anchor_at_write != final_anchor:
                    raise ArmingError(
                        "official event authority changed at arming receipt write"
                    )

                existing = _load_stream(root, episode_id)
                if existing:
                    current_path, current = existing[-1]
                    current_anchor = current["event_anchor"]
                    if current_anchor == final_anchor:
                        return _result(
                            current,
                            status=ALREADY_ARMED_STATUS,
                            receipt_path=current_path,
                        )
                    if (
                        expected_current_arming_receipt_hash is None
                        or current.get("receipt_hash")
                        != expected_current_arming_receipt_hash
                    ):
                        raise ArmingError(
                            "current arming receipt changed; an exact revision "
                            "compare-and-swap hash is required"
                        )
                elif expected_current_arming_receipt_hash is not None:
                    raise ArmingError(
                        "expected current arming receipt hash was supplied but no "
                        "receipt exists"
                    )

                revision = len(existing)
                previous_hash = existing[-1][1]["receipt_hash"] if existing else None
                record: dict[str, Any] = {
                    "schema": ARMING_SCHEMA,
                    "record_type": ARMING_RECORD_TYPE,
                    "arming_id": _arming_id(episode_id),
                    "revision": revision,
                    "supersedes_arming_receipt_hash": previous_hash,
                    "status": ARMED_STATUS,
                    "run_id": run_id,
                    "armed_at_utc": _utc_iso(final_write_ts),
                    "armed_by": armed_by,
                    "lead_sec_at_arming": expected_official_t0 - final_write_ts,
                    "plan_id": trust_root.PLAN_ID,
                    "plan_hash": trust_root.PLAN_HASH,
                    "resolved_paths_hash": exact_commit_preflight[
                        "resolved_paths_hash"
                    ],
                    "event_anchor": final_anchor,
                    "capture_authorized": False,
                    "capture_token_issued": False,
                    "event_bound_plan_generated": False,
                }
                record["receipt_hash"] = _receipt_hash(record)
                _validate_receipt(record)
                receipt_path = _stream_dir(root, episode_id) / (
                    f"{revision:020d}-{record['receipt_hash']}.json"
                )
                _write_receipt(receipt_path, record)
                committed = load_arming_receipt(receipt_path)
                return _result(
                    committed, status=ARMED_STATUS, receipt_path=receipt_path
                )
        except registry.EventRegistryError as exc:
            raise ArmingError(
                f"official t0 arming registry guard failed: {type(exc).__name__}: {exc}"
            ) from exc


def verify_arming_chain(
    *, arming_root: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Verify every immutable arming stream without granting any authority."""
    root = Path(arming_root or ARMING_ROOT)
    if not root.exists():
        return {
            "schema": ARMING_VERIFY_SCHEMA,
            "status": "NO_ARMING_RECEIPTS",
            "streams": 0,
            "receipts": 0,
            "capture_authorized": False,
        }
    problems: list[str] = []
    streams = 0
    receipts = 0
    for stream_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        streams += 1
        files = sorted(stream_dir.glob("*.json"))
        if not files:
            problems.append(f"empty arming stream: {stream_dir.name}")
            continue
        try:
            first = load_arming_receipt(files[0])
            episode_id = str(first["event_anchor"]["episode_id"])
            loaded = _load_stream(root, episode_id)
            if _stream_dir(root, episode_id).resolve(strict=False) != stream_dir.resolve(
                strict=False
            ):
                raise ArmingError("arming stream directory does not match episode_id")
            receipts += len(loaded)
        except ArmingError as exc:
            problems.append(f"{stream_dir.name}: {exc}")
    return {
        "schema": ARMING_VERIFY_SCHEMA,
        "status": "ARMING_CHAIN_OK" if not problems else "ARMING_CHAIN_INVALID",
        "streams": streams,
        "receipts": receipts,
        "problems": problems,
        "capture_authorized": False,
    }


def _parse_expected_t0(value: str) -> int:
    text = str(value or "").strip()
    try:
        numeric = int(text)
    except ValueError:
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ArmingError("--expected-official-t0 must be epoch seconds or ISO UTC") from exc
        if moment.tzinfo is None or moment.utcoffset() != timezone.utc.utcoffset(None):
            raise ArmingError("--expected-official-t0 must be explicit UTC")
        numeric = int(moment.timestamp())
    if numeric <= 0:
        raise ArmingError("--expected-official-t0 must be positive")
    return numeric


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seal a seconds-grade official spot t0 without capture authority."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--arm", action="store_true")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--episode-id", default="")
    parser.add_argument("--official-record-hash", default="")
    parser.add_argument("--expected-official-t0", default="")
    parser.add_argument("--expected-contract", default="")
    parser.add_argument("--expected-spot-symbol", default="")
    parser.add_argument("--expected-current-arming-receipt-hash", default="")
    parser.add_argument("--armed-by", default="")
    parser.add_argument("--acknowledge-no-capture-authority", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.status:
        if args.episode_id:
            current = load_current_arming(args.episode_id)
            result: Mapping[str, Any] = current or {
                "schema": ARMING_VERIFY_SCHEMA,
                "status": "NO_ARMING_RECEIPT",
                "episode_id": args.episode_id,
                "capture_authorized": False,
            }
        else:
            result = verify_arming_chain()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("status") != "ARMING_CHAIN_INVALID" else 2

    required = {
        "--run-id": args.run_id,
        "--episode-id": args.episode_id,
        "--official-record-hash": args.official_record_hash,
        "--expected-official-t0": args.expected_official_t0,
        "--expected-contract": args.expected_contract,
        "--expected-spot-symbol": args.expected_spot_symbol,
        "--armed-by": args.armed_by,
    }
    missing = [flag for flag, value in required.items() if not str(value or "").strip()]
    if missing:
        parser.error("--arm requires " + ", ".join(missing))
    result = arm_official_t0(
        now_ts=int(time.time()),
        run_id=args.run_id,
        episode_id=args.episode_id,
        expected_official_record_hash=args.official_record_hash,
        expected_official_t0=_parse_expected_t0(args.expected_official_t0),
        expected_contract=args.expected_contract,
        expected_spot_symbol=args.expected_spot_symbol,
        expected_current_arming_receipt_hash=(
            args.expected_current_arming_receipt_hash or None
        ),
        armed_by=args.armed_by,
        acknowledge_no_capture_authority=args.acknowledge_no_capture_authority,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
