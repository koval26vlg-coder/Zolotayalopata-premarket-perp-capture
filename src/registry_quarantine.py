"""Fail-closed archival transaction for one invalid registry generation.

Quarantine is a recovery write, not a convenient rename.  The operator first reads
the complete generation CAS, then supplies that exact value to this module.  A valid
initial preflight, the registry mutation lock, an exact verifier recovery action, a
second preflight, and a second CAS are all required before an archive is published.

The archive is fsync'd and atomically published before any source byte is removed.
If a failure happens after publication, the registry lock is deliberately retained
and automatic recovery is refused: the archived bytes and immutable state receipts
make manual recovery possible without pretending that an untested crash continuation
is safe.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import event_registry as registry
import frozen_plan_bindings as trust_root
import global_market_writer_claim as writer_claim
import project_config as config
import risk_gate
from canonical_hash import canonical_hash, canonical_json_bytes


WRITE_CLASS = "registry_quarantine"
PREFLIGHT_DECISION = "ALLOW_REGISTRY_QUARANTINE"
REQUIRED_RECOVERY_ACTION = (
    "RESTORE_MATCHING_SUMMARY_OR_QUARANTINE_AND_BOOTSTRAP_NEW_GENERATION"
)
SNAPSHOT_SCHEMA = "premarket_perp_registry_generation_cas_v1"
ARCHIVE_SCHEMA = "premarket_perp_registry_quarantine_v2"
STATE_SCHEMA = "premarket_perp_registry_quarantine_state_v1"
RESULT_SCHEMA = "premarket_perp_registry_quarantine_result_v2"
RECEIPT_ARCHIVE_SCHEMA = "premarket_perp_registry_receipt_bytes_v1"

STATE_PREPARED = "PREPARED"
STATE_ARCHIVE_DURABLE = "ARCHIVE_DURABLE"
STATE_SOURCE_DEACTIVATED = "SOURCE_DEACTIVATED"
STATE_LOCK_RELEASED = "LOCK_RELEASED"
LOCK_RELEASE_PROOF_FILE = "003-LOCK-RELEASED-PROOF.json"

STATE_FILES = {
    STATE_PREPARED: "000-PREPARED.json",
    STATE_ARCHIVE_DURABLE: "001-ARCHIVE_DURABLE.json",
    STATE_SOURCE_DEACTIVATED: "002-SOURCE_DEACTIVATED.json",
}

_TRANSACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class QuarantineError(RuntimeError):
    """A quarantine precondition failed before a recoverable boundary was crossed."""


class QuarantineRecoveryRequired(QuarantineError):
    """A durable transaction requires an operator; no automatic mutation is safe."""

    def __init__(
        self,
        message: str,
        *,
        transaction_id: str,
        archive_path: Path,
    ) -> None:
        super().__init__(message)
        self.transaction_id = transaction_id
        self.archive_path = archive_path


@dataclass(frozen=True)
class QuarantinePaths:
    registry_path: Path
    summary_path: Path
    receipt_dir: Path
    lock_path: Path
    quarantine_root: Path
    shared_writer_claim_path: Path


@dataclass(frozen=True)
class RawReceipt:
    name: str
    content: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class RegistryGenerationSnapshot:
    registry_name: str
    registry_bytes: bytes
    summary_name: str
    summary_bytes: bytes | None
    receipt_dir_name: str
    receipt_dir_present: bool
    receipts: tuple[RawReceipt, ...]
    generation_cas: str


def _production_paths() -> QuarantinePaths:
    return QuarantinePaths(
        registry_path=registry.REGISTRY_PATH,
        summary_path=registry.REGISTRY_SUMMARY_PATH,
        receipt_dir=registry.REGISTRY_PATH.with_name(
            registry.REGISTRY_PATH.name + registry.MUTATION_RECEIPT_DIR_SUFFIX
        ),
        lock_path=registry.REGISTRY_LOCK_PATH,
        quarantine_root=config.REGISTRY_QUARANTINE_ROOT,
        shared_writer_claim_path=config.SHARED_WRITER_CLAIM_PATH,
    )


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_link_like(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(is_junction and is_junction(path))


def _file_bytes(path: Path, *, required: bool, role: str) -> bytes | None:
    if not _path_exists(path):
        if required:
            raise QuarantineError(f"{role} is missing: {path.name}")
        return None
    if _is_link_like(path) or not path.is_file():
        raise QuarantineError(f"{role} must be one regular file: {path.name}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise QuarantineError(f"cannot read {role} {path.name}: {exc}") from exc


def _generation_descriptor(
    *,
    registry_name: str,
    registry_bytes: bytes,
    summary_name: str,
    summary_bytes: bytes | None,
    receipt_dir_name: str,
    receipt_dir_present: bool,
    receipts: tuple[RawReceipt, ...],
) -> dict[str, Any]:
    def item(role: str, name: str, content: bytes | None) -> dict[str, Any]:
        return {
            "role": role,
            "name": name,
            "present": content is not None,
            "size": len(content) if content is not None else None,
            "sha256": hashlib.sha256(content).hexdigest() if content is not None else None,
        }

    return {
        "schema": SNAPSHOT_SCHEMA,
        "registry": item("registry", registry_name, registry_bytes),
        "summary": item("summary", summary_name, summary_bytes),
        "receipt_directory": {
            "role": "mutation_receipt_directory",
            "name": receipt_dir_name,
            "present": receipt_dir_present,
            "entries": [
                item("mutation_receipt", receipt.name, receipt.content)
                for receipt in receipts
            ],
        },
    }


def snapshot_registry_generation(paths: QuarantinePaths) -> RegistryGenerationSnapshot:
    """Read every source byte and calculate one operator-visible generation CAS."""
    registry_bytes = _file_bytes(paths.registry_path, required=True, role="registry")
    assert registry_bytes is not None
    summary_bytes = _file_bytes(paths.summary_path, required=False, role="summary")

    receipts: list[RawReceipt] = []
    receipt_dir_present = _path_exists(paths.receipt_dir)
    if receipt_dir_present:
        if _is_link_like(paths.receipt_dir) or not paths.receipt_dir.is_dir():
            raise QuarantineError("mutation receipt anchor must be one directory")
        try:
            entries = sorted(paths.receipt_dir.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise QuarantineError(f"cannot enumerate mutation receipts: {exc}") from exc
        for entry in entries:
            if _is_link_like(entry) or not entry.is_file():
                raise QuarantineError(
                    "mutation receipt directory may contain regular files only"
                )
            try:
                receipts.append(RawReceipt(entry.name, entry.read_bytes()))
            except OSError as exc:
                raise QuarantineError(
                    f"cannot read mutation receipt {entry.name}: {exc}"
                ) from exc

    frozen_receipts = tuple(receipts)
    descriptor = _generation_descriptor(
        registry_name=paths.registry_path.name,
        registry_bytes=registry_bytes,
        summary_name=paths.summary_path.name,
        summary_bytes=summary_bytes,
        receipt_dir_name=paths.receipt_dir.name,
        receipt_dir_present=receipt_dir_present,
        receipts=frozen_receipts,
    )
    return RegistryGenerationSnapshot(
        registry_name=paths.registry_path.name,
        registry_bytes=registry_bytes,
        summary_name=paths.summary_path.name,
        summary_bytes=summary_bytes,
        receipt_dir_name=paths.receipt_dir.name,
        receipt_dir_present=receipt_dir_present,
        receipts=frozen_receipts,
        generation_cas=canonical_hash(descriptor),
    )


def _verifier_evidence(report: Mapping[str, Any]) -> dict[str, Any]:
    problems = report.get("problems")
    if not isinstance(problems, list):
        problems = []
    # Store a digest, not potentially absolute local paths embedded in diagnostics.
    return {
        "status": report.get("status"),
        "summary_verified": report.get("summary_verified") is True,
        "recovery_action": report.get("recovery_action"),
        "problem_count": len(problems),
        "problems_hash": canonical_hash({"problems": problems}),
    }


def inspect_quarantine_candidate() -> dict[str, Any]:
    """Read-only operator step: return the CAS that a later write must match."""
    paths = _production_paths()
    snapshot = snapshot_registry_generation(paths)
    try:
        report = registry.verify_registry(paths.registry_path)
    except Exception as exc:  # noqa: BLE001 - verifier failure is evidence, not authority
        return {
            "schema": RESULT_SCHEMA,
            "status": "VERIFIER_FAILED_NOT_QUARANTINE_AUTHORIZED",
            "expected_generation_cas": snapshot.generation_cas,
            "error": f"{type(exc).__name__}: {exc}",
        }
    candidate = report.get("recovery_action") == REQUIRED_RECOVERY_ACTION
    return {
        "schema": RESULT_SCHEMA,
        "status": "QUARANTINE_CANDIDATE" if candidate else "NOT_QUARANTINE_CANDIDATE",
        "expected_generation_cas": snapshot.generation_cas,
        "verifier": _verifier_evidence(report),
    }


def _require_exact_preflight(
    receipt: Any,
    *,
    phase: str,
    run_id: str,
    initial: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    if not registry._preflight_is_exact(
        receipt,
        write_class=WRITE_CLASS,
        run_id=run_id,
        decision=PREFLIGHT_DECISION,
        action=risk_gate.REGISTRY_QUARANTINE_ACTION,
    ):
        raise QuarantineError(f"{phase} preflight is not exact")
    assert isinstance(receipt, Mapping)
    if initial is not None:
        for field in ("plan_id", "plan_hash", "resolved_paths_hash"):
            if receipt.get(field) != initial.get(field):
                raise QuarantineError(
                    f"{phase} preflight changed authority field {field}"
                )
    return receipt


def _run_preflight(*, phase: str, run_id: str) -> Mapping[str, Any]:
    try:
        receipt = risk_gate.preflight(write_class=WRITE_CLASS, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - every gate failure must become a block
        raise QuarantineError(
            f"{phase} preflight failed: {type(exc).__name__}: {exc}"
        ) from exc
    return _require_exact_preflight(receipt, phase=phase, run_id=run_id)


def _windows_move_path_write_through(
    source: Path,
    destination: Path,
    *,
    replace: bool,
) -> None:
    import ctypes
    from ctypes import wintypes

    move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file_ex.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move_file_ex.restype = wintypes.BOOL
    movefile_replace_existing = 0x1
    movefile_write_through = 0x8
    flags = movefile_write_through | (movefile_replace_existing if replace else 0)
    if not move_file_ex(str(source), str(destination), flags):
        error = ctypes.get_last_error()
        if not replace and error in {80, 183}:
            raise FileExistsError(destination)
        raise QuarantineError(
            f"write-through move failed ({error}): {source.name} -> {destination.name}"
        )


def _move_path_durable(source: Path, destination: Path, *, replace: bool) -> None:
    """Publish one same-volume metadata transition with an explicit durability path."""
    if source.resolve(strict=False).anchor.lower() != destination.resolve(
        strict=False
    ).anchor.lower():
        raise QuarantineError("durable quarantine move must remain on one volume")
    if os.name == "nt":
        _windows_move_path_write_through(source, destination, replace=replace)
        return
    if replace:
        os.replace(source, destination)
    else:
        os.rename(source, destination)
    _fsync_directory(source.parent)
    if destination.parent.resolve(strict=False) != source.parent.resolve(strict=False):
        _fsync_directory(destination.parent)


def _mkdir_exclusive_durable(path: Path) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.mkdir")
    os.mkdir(temporary)
    try:
        _move_path_durable(temporary, path, replace=False)
    except BaseException:
        try:
            temporary.rmdir()
        except OSError:
            pass
        raise


def _ensure_directory_durable(path: Path) -> None:
    if _path_exists(path):
        if _is_link_like(path) or not path.is_dir():
            raise QuarantineError(f"durable directory path is invalid: {path.name}")
        return
    _ensure_directory_durable(path.parent)
    try:
        _mkdir_exclusive_durable(path)
    except FileExistsError:
        if _is_link_like(path) or not path.is_dir():
            raise QuarantineError(f"durable directory race is invalid: {path.name}")


def _write_bytes_exclusive(path: Path, content: bytes) -> None:
    _ensure_directory_durable(path.parent)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _move_path_durable(temporary, path, replace=False)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(payload), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    _write_bytes_exclusive(path, _json_bytes(payload))


def _fsync_directory(path: Path) -> None:
    """Persist POSIX directory metadata; Windows callers use MoveFileExW instead."""
    if os.name == "nt":
        raise QuarantineError(
            "Windows directory fsync is unsupported; use a write-through path transition"
        )
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _transaction_id(run_id: str) -> str:
    stamp = registry.utc_now_iso().replace("-", "").replace(":", "")
    tag = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:10]
    return f"{stamp}-{tag}-{secrets.token_hex(4)}"


def _validate_transaction_id(transaction_id: str) -> str:
    if not _TRANSACTION_ID.fullmatch(str(transaction_id or "")):
        raise QuarantineError("invalid quarantine transaction id")
    return transaction_id


def _ensure_transaction_roots(paths: QuarantinePaths) -> Path:
    root = paths.quarantine_root
    if _path_exists(root) and (_is_link_like(root) or not root.is_dir()):
        raise QuarantineError("quarantine root must be one local directory")
    _ensure_directory_durable(root)
    transactions = root / ".transactions"
    if _path_exists(transactions) and (
        _is_link_like(transactions) or not transactions.is_dir()
    ):
        raise QuarantineError("quarantine transaction root must be one local directory")
    _ensure_directory_durable(transactions)
    return transactions


def _receipt_archive_bytes(receipts: tuple[RawReceipt, ...]) -> bytes:
    lines: list[bytes] = []
    for receipt in receipts:
        row = {
            "schema": RECEIPT_ARCHIVE_SCHEMA,
            "original_name": receipt.name,
            "size": len(receipt.content),
            "sha256": receipt.sha256,
            "content_b64": base64.b64encode(receipt.content).decode("ascii"),
        }
        lines.append(
            json.dumps(
                row,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return b"\n".join(lines) + (b"\n" if lines else b"")


def _source_summary_identity(summary_bytes: bytes | None) -> dict[str, Any] | None:
    if summary_bytes is None:
        return None
    try:
        payload = json.loads(summary_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return {
        "plan_id": payload.get("plan_id"),
        "plan_hash": payload.get("plan_hash"),
    }


def _archive_file_record(role: str, name: str, content: bytes) -> dict[str, Any]:
    return {
        "role": role,
        "archive_name": name,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _owned_lock_bytes(owner: registry.RegistryLockOwner) -> bytes:
    registry._assert_registry_lock_owner(owner)
    if _is_link_like(owner.path) or not owner.path.is_file():
        raise QuarantineError("owned registry lock is not one regular file")
    try:
        return owner.path.read_bytes()
    except OSError as exc:
        raise QuarantineError(f"cannot read owned registry lock: {exc}") from exc


def _state_record(
    *,
    transaction_id: str,
    state: str,
    previous_state_hash: str | None,
    source_generation_cas: str,
    archive_manifest_sha256: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record = {
        "schema": STATE_SCHEMA,
        "transaction_id": transaction_id,
        "state": state,
        "recorded_at_utc": registry.utc_now_iso(),
        "previous_state_hash": previous_state_hash,
        "source_generation_cas": source_generation_cas,
        "archive_manifest_sha256": archive_manifest_sha256,
    }
    if details is not None:
        record["details"] = dict(details)
    record["state_hash"] = hashlib.sha256(canonical_json_bytes(record)).hexdigest()
    return record


def _stage_archive(
    *,
    stage_path: Path,
    transaction_id: str,
    run_id: str,
    reason: str,
    snapshot: RegistryGenerationSnapshot,
    verifier_report: Mapping[str, Any],
    authority: Mapping[str, Any],
    owned_lock_name: str,
    owned_lock_bytes: bytes,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _mkdir_exclusive_durable(stage_path)
    files: list[dict[str, Any]] = []

    _write_bytes_exclusive(stage_path / "registry.bin", snapshot.registry_bytes)
    files.append(_archive_file_record("registry", "registry.bin", snapshot.registry_bytes))
    if snapshot.summary_bytes is not None:
        _write_bytes_exclusive(stage_path / "summary.bin", snapshot.summary_bytes)
        files.append(_archive_file_record("summary", "summary.bin", snapshot.summary_bytes))

    receipt_archive = _receipt_archive_bytes(snapshot.receipts)
    _write_bytes_exclusive(stage_path / "mutation-receipts.jsonl", receipt_archive)
    files.append(
        _archive_file_record(
            "mutation_receipt_archive", "mutation-receipts.jsonl", receipt_archive
        )
    )
    receipt_entries = [
        {
            "original_name": receipt.name,
            "size": len(receipt.content),
            "sha256": receipt.sha256,
        }
        for receipt in snapshot.receipts
    ]

    manifest = {
        "schema": ARCHIVE_SCHEMA,
        "transaction_id": transaction_id,
        "created_at_utc": registry.utc_now_iso(),
        "run_id": run_id,
        "reason": reason,
        "source_generation_cas": snapshot.generation_cas,
        "source": {
            "registry_name": snapshot.registry_name,
            "summary_name": snapshot.summary_name,
            "summary_present": snapshot.summary_bytes is not None,
            "receipt_dir_name": snapshot.receipt_dir_name,
            "receipt_dir_present": snapshot.receipt_dir_present,
            "summary_plan_identity": _source_summary_identity(snapshot.summary_bytes),
        },
        "archive_files": files,
        "receipt_entries": receipt_entries,
        "verifier": _verifier_evidence(verifier_report),
        "authority": {
            "plan_id": authority.get("plan_id"),
            "plan_hash": authority.get("plan_hash"),
            "resolved_paths_hash": authority.get("resolved_paths_hash"),
            "registry_lock_name": owned_lock_name,
            "registry_lock_sha256": hashlib.sha256(owned_lock_bytes).hexdigest(),
        },
    }
    manifest_bytes = _json_bytes(manifest)
    _write_bytes_exclusive(stage_path / "archive-manifest.json", manifest_bytes)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    prepared = _state_record(
        transaction_id=transaction_id,
        state=STATE_PREPARED,
        previous_state_hash=None,
        source_generation_cas=snapshot.generation_cas,
        archive_manifest_sha256=manifest_sha,
    )
    _write_json_exclusive(stage_path / STATE_FILES[STATE_PREPARED], prepared)
    return manifest, prepared


def _remove_stage(stage_path: Path, transaction_root: Path) -> None:
    if not _path_exists(stage_path):
        return
    if stage_path.parent.resolve(strict=False) != transaction_root.resolve(strict=False):
        raise QuarantineError("refusing to clean an unexpected staging path")
    shutil.rmtree(stage_path)


def _restore_unverified_tombstone(tombstone: Path, source: Path) -> None:
    """Restore bytes we did not prove archived, or retain them fail-closed."""
    if _path_exists(source):
        raise QuarantineError(
            f"source changed during deactivation; unverified tombstone retained as "
            f"{tombstone.name}"
        )
    _move_path_durable(tombstone, source, replace=False)


def _deactivate_source_component(
    role: str,
    path: Path,
    expected: bytes | tuple[RawReceipt, ...],
    *,
    transaction_id: str,
) -> Path:
    """Atomically detach and verify source bytes, retaining a recovery tombstone.

    A writer that ignores the registry lock may replace the canonical source after the
    second generation CAS. Renaming first means such late bytes are either verified
    against the snapshot or restored/retained; they are never blindly unlinked.
    """
    if role not in {"mutation_receipts", "summary", "registry"}:
        raise QuarantineError(f"unknown source component: {role}")
    if not _path_exists(path):
        raise QuarantineError(f"{role} source disappeared before deactivation")

    tombstone = path.with_name(
        f".{path.name}.quarantine-{transaction_id}.{role}.deactivated"
    )
    if _path_exists(tombstone):
        raise QuarantineError("quarantine deactivation tombstone already exists")
    _move_path_durable(path, tombstone, replace=False)

    matches = False
    try:
        if role == "mutation_receipts":
            if not isinstance(expected, tuple):
                raise QuarantineError("mutation receipt expectation is malformed")
            if _is_link_like(tombstone) or not tombstone.is_dir():
                matches = False
            else:
                entries = sorted(tombstone.iterdir(), key=lambda item: item.name)
                actual = []
                valid = True
                for entry in entries:
                    if _is_link_like(entry) or not entry.is_file():
                        valid = False
                        break
                    actual.append(RawReceipt(entry.name, entry.read_bytes()))
                matches = valid and tuple(actual) == expected
        else:
            if not isinstance(expected, bytes):
                raise QuarantineError(f"{role} byte expectation is malformed")
            matches = (
                not _is_link_like(tombstone)
                and tombstone.is_file()
                and tombstone.read_bytes() == expected
            )
    except OSError as exc:
        try:
            _restore_unverified_tombstone(tombstone, path)
        except BaseException as restore_exc:  # noqa: BLE001
            raise QuarantineError(
                f"cannot verify or restore {role} source; tombstone retained"
            ) from restore_exc
        raise QuarantineError(f"cannot verify {role} before deactivation: {exc}") from exc

    if not matches:
        _restore_unverified_tombstone(tombstone, path)
        raise QuarantineError(
            f"{role} source bytes changed after generation CAS; source restored"
        )

    if _path_exists(path):
        raise QuarantineError(
            f"{role} source reappeared during deactivation; new bytes were not deleted"
        )
    return tombstone


def _require_source_components_absent(paths: QuarantinePaths) -> None:
    remaining = [
        role
        for role, path in (
            ("mutation_receipts", paths.receipt_dir),
            ("summary", paths.summary_path),
            ("registry", paths.registry_path),
        )
        if _path_exists(path)
    ]
    if remaining:
        raise QuarantineError(
            "source components appeared during deactivation: " + ", ".join(remaining)
        )


def _release_registry_lock_to_archive(
    owner: registry.RegistryLockOwner,
    *,
    archive_path: Path,
    expected_lock_bytes: bytes,
) -> str:
    """Atomically turn the canonical lock into its durable release proof.

    Before ``os.replace`` the canonical lock blocks every cooperating writer. After
    it, the exact same bytes exist at the terminal proof path. There is no interval in
    which the lock is absent while release evidence is also absent.
    """
    current = _owned_lock_bytes(owner)
    if current != expected_lock_bytes:
        raise QuarantineError("registry lock bytes changed before release")
    proof_path = archive_path / LOCK_RELEASE_PROOF_FILE
    if _path_exists(proof_path):
        raise QuarantineError("registry lock release proof already exists")
    proof_sha256 = hashlib.sha256(expected_lock_bytes).hexdigest()
    # This is the final fallible transition. On Windows MoveFileExW carries
    # MOVEFILE_WRITE_THROUGH; on POSIX both parent directories are fsync'd by the
    # durable move helper. Nothing that can fail is executed after it.
    _move_path_durable(owner.path, proof_path, replace=False)
    return proof_sha256


def _archive_state(
    archive_path: Path,
    *,
    transaction_id: str,
    state: str,
    previous_state_hash: str,
    source_generation_cas: str,
    archive_manifest_sha256: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record = _state_record(
        transaction_id=transaction_id,
        state=state,
        previous_state_hash=previous_state_hash,
        source_generation_cas=source_generation_cas,
        archive_manifest_sha256=archive_manifest_sha256,
        details=details,
    )
    _write_json_exclusive(archive_path / STATE_FILES[state], record)
    return record


def _verify_published_archive_before_deactivation(
    archive_path: Path,
    *,
    manifest: Mapping[str, Any],
    prepared: Mapping[str, Any],
    snapshot: RegistryGenerationSnapshot,
) -> str:
    """Read every published byte back and reconstruct the exact source CAS."""
    problems: list[str] = []
    manifest_path = archive_path / "archive-manifest.json"
    if _is_link_like(manifest_path) or not manifest_path.is_file():
        raise QuarantineError("published archive manifest is not one regular file")
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise QuarantineError(f"published archive manifest is unreadable: {exc}") from exc
    expected_manifest_bytes = _json_bytes(manifest)
    if manifest_bytes != expected_manifest_bytes:
        raise QuarantineError("published archive manifest differs from staged bytes")
    prepared_path = archive_path / STATE_FILES[STATE_PREPARED]
    if _is_link_like(prepared_path) or not prepared_path.is_file():
        raise QuarantineError("published PREPARED state is not one regular file")
    try:
        prepared_bytes = prepared_path.read_bytes()
    except OSError as exc:
        raise QuarantineError(f"published PREPARED state is unreadable: {exc}") from exc
    if prepared_bytes != _json_bytes(prepared):
        raise QuarantineError("published PREPARED state differs from staged bytes")
    problems.extend(_verify_archive_files(archive_path, manifest))
    declared_files = manifest.get("archive_files")
    declared_names = {
        str(item.get("archive_name"))
        for item in declared_files
        if isinstance(declared_files, list) and isinstance(item, Mapping)
    }
    expected_names = {
        "archive-manifest.json",
        STATE_FILES[STATE_PREPARED],
        *declared_names,
    }
    try:
        actual_names = {item.name for item in archive_path.iterdir()}
    except OSError as exc:
        problems.append(f"published archive cannot be enumerated: {exc}")
    else:
        if actual_names != expected_names:
            problems.append(
                "published archive entry set differs from staged transaction"
            )
    receipt_problems, recovered_receipts = _verify_receipt_archive(
        archive_path, manifest
    )
    problems.extend(receipt_problems)
    try:
        archived_registry = (archive_path / "registry.bin").read_bytes()
        archived_summary = (
            (archive_path / "summary.bin").read_bytes()
            if snapshot.summary_bytes is not None
            else None
        )
    except OSError as exc:
        problems.append(f"published source archive is unreadable: {exc}")
        archived_registry = b""
        archived_summary = None
    if archived_registry != snapshot.registry_bytes:
        problems.append("published registry bytes differ from source snapshot")
    if archived_summary != snapshot.summary_bytes:
        problems.append("published summary bytes differ from source snapshot")
    if recovered_receipts != snapshot.receipts:
        problems.append("published receipt bytes differ from source snapshot")
    descriptor = _generation_descriptor(
        registry_name=snapshot.registry_name,
        registry_bytes=archived_registry,
        summary_name=snapshot.summary_name,
        summary_bytes=archived_summary,
        receipt_dir_name=snapshot.receipt_dir_name,
        receipt_dir_present=snapshot.receipt_dir_present,
        receipts=recovered_receipts,
    )
    if canonical_hash(descriptor) != snapshot.generation_cas:
        problems.append("published archive does not reconstruct source generation CAS")
    if problems:
        raise QuarantineError(
            "published archive readback failed: " + "; ".join(problems)
        )
    return hashlib.sha256(manifest_bytes).hexdigest()


def quarantine_registry(
    *,
    run_id: str,
    reason: str,
    expected_generation_cas: str,
) -> dict[str, Any]:
    """Archive and deactivate exactly one operator-approved invalid generation."""
    run_id = str(run_id or "").strip()
    reason = str(reason or "").strip()
    if not run_id:
        raise QuarantineError("quarantine run_id is required")
    if not reason:
        raise QuarantineError("quarantine reason is required")
    if not _is_sha256(expected_generation_cas):
        raise QuarantineError("expected_generation_cas must be one lowercase SHA-256")

    initial = _run_preflight(phase="initial", run_id=run_id)
    paths = _production_paths()
    owner: registry.RegistryLockOwner | None = None
    stage_path: Path | None = None
    transaction_root: Path | None = None
    archive_path: Path | None = None
    transaction_id = ""
    archive_published = False
    global_claim: dict[str, Any] | None = None

    try:
        try:
            owner = registry.acquire_registry_lock(
                paths.lock_path,
                run_id=run_id,
                plan_hash=str(initial["plan_hash"]),
            )
        except Exception as exc:  # noqa: BLE001 - lock failure is a hard block
            raise QuarantineError(f"registry lock blocked: {exc}") from exc

        snapshot = snapshot_registry_generation(paths)
        try:
            verifier_report = registry.verify_registry(
                paths.registry_path,
                bootstrap_lock_owner=owner,
            )
        except Exception as exc:  # noqa: BLE001 - an exception grants no recovery right
            raise QuarantineError(
                f"registry verifier did not authorize quarantine: {type(exc).__name__}: {exc}"
            ) from exc
        if verifier_report.get("recovery_action") != REQUIRED_RECOVERY_ACTION:
            raise QuarantineError("registry does not require quarantine")
        if snapshot.generation_cas != expected_generation_cas:
            raise QuarantineError(
                "generation CAS mismatch: inspect again before requesting quarantine"
            )
        owned_lock_bytes = _owned_lock_bytes(owner)

        transaction_root = _ensure_transaction_roots(paths)
        transaction_id = _transaction_id(run_id)
        _validate_transaction_id(transaction_id)
        stage_path = transaction_root / f"{transaction_id}.staging"
        archive_path = paths.quarantine_root / transaction_id
        if _path_exists(archive_path):
            raise QuarantineError("quarantine transaction identity already exists")
        manifest, prepared = _stage_archive(
            stage_path=stage_path,
            transaction_id=transaction_id,
            run_id=run_id,
            reason=reason,
            snapshot=snapshot,
            verifier_report=verifier_report,
            authority=initial,
            owned_lock_name=owner.path.name,
            owned_lock_bytes=owned_lock_bytes,
        )

        commit = _run_preflight(phase="commit", run_id=run_id)
        _require_exact_preflight(
            commit,
            phase="commit",
            run_id=run_id,
            initial=initial,
        )
        commit_snapshot = snapshot_registry_generation(paths)
        if commit_snapshot.generation_cas != snapshot.generation_cas:
            raise QuarantineError("registry generation changed before commit")
        if _owned_lock_bytes(owner) != owned_lock_bytes:
            raise QuarantineError("registry lock bytes changed before commit")

        try:
            global_claim = writer_claim.claim_global_market_writer(
                paths.shared_writer_claim_path,
                run_id=run_id,
                owner_pid=os.getpid(),
                owner_kind="registry_quarantine",
                plan_hash=str(initial["plan_hash"]),
                output_namespace=paths.quarantine_root,
            )
        except Exception as exc:  # noqa: BLE001 - the global writer race is a block
            raise QuarantineError(
                f"global market writer claim blocked quarantine: {exc}"
            ) from exc
        claimed_snapshot = snapshot_registry_generation(paths)
        if claimed_snapshot.generation_cas != snapshot.generation_cas:
            raise QuarantineError(
                "registry generation changed while acquiring global writer claim"
            )
        if _owned_lock_bytes(owner) != owned_lock_bytes:
            raise QuarantineError(
                "registry lock bytes changed while acquiring global writer claim"
            )

        _move_path_durable(stage_path, archive_path, replace=False)
        archive_published = True
        manifest_sha = _verify_published_archive_before_deactivation(
            archive_path,
            manifest=manifest,
            prepared=prepared,
            snapshot=snapshot,
        )
        durable = _archive_state(
            archive_path,
            transaction_id=transaction_id,
            state=STATE_ARCHIVE_DURABLE,
            previous_state_hash=str(prepared["state_hash"]),
            source_generation_cas=snapshot.generation_cas,
            archive_manifest_sha256=manifest_sha,
        )

        post_archive_snapshot = snapshot_registry_generation(paths)
        if post_archive_snapshot.generation_cas != snapshot.generation_cas:
            raise QuarantineError(
                "registry generation changed after archive readback"
            )
        if _owned_lock_bytes(owner) != owned_lock_bytes:
            raise QuarantineError("registry lock bytes changed after archive readback")

        # Source registry is deliberately last. A partial failure therefore keeps the
        # primary source present wherever possible and always retains the lock.
        tombstones: dict[str, str] = {}
        if snapshot.receipt_dir_present:
            receipt_tombstone = _deactivate_source_component(
                "mutation_receipts",
                paths.receipt_dir,
                snapshot.receipts,
                transaction_id=transaction_id,
            )
            tombstones["mutation_receipts"] = receipt_tombstone.name
        elif _path_exists(paths.receipt_dir):
            raise QuarantineError("mutation receipts appeared after generation CAS")
        if snapshot.summary_bytes is not None:
            summary_tombstone = _deactivate_source_component(
                "summary",
                paths.summary_path,
                snapshot.summary_bytes,
                transaction_id=transaction_id,
            )
            tombstones["summary"] = summary_tombstone.name
        elif _path_exists(paths.summary_path):
            raise QuarantineError("summary appeared after generation CAS")
        registry_tombstone = _deactivate_source_component(
            "registry",
            paths.registry_path,
            snapshot.registry_bytes,
            transaction_id=transaction_id,
        )
        tombstones["registry"] = registry_tombstone.name
        _require_source_components_absent(paths)

        source_deactivated = _archive_state(
            archive_path,
            transaction_id=transaction_id,
            state=STATE_SOURCE_DEACTIVATED,
            previous_state_hash=str(durable["state_hash"]),
            source_generation_cas=snapshot.generation_cas,
            archive_manifest_sha256=manifest_sha,
            details={"retained_source_tombstones": dict(sorted(tombstones.items()))},
        )
        terminal_boundary = quarantine_transaction_status(transaction_id)
        if not (
            terminal_boundary.get("status") == "RECOVERY_REQUIRED_FAIL_CLOSED"
            and terminal_boundary.get("state") == STATE_SOURCE_DEACTIVATED
            and terminal_boundary.get("problems") == []
        ):
            raise QuarantineError(
                "source-deactivated quarantine boundary failed exact verification"
            )
        assert global_claim is not None
        writer_claim.release_global_market_writer(
            paths.shared_writer_claim_path,
            run_id=run_id,
            owner_pid=os.getpid(),
            ownership_token=str(global_claim["ownership_token"]),
            final_status="REGISTRY_QUARANTINED",
        )
        global_claim = None
        final_boundary = quarantine_transaction_status(transaction_id)
        if not (
            final_boundary.get("status") == "RECOVERY_REQUIRED_FAIL_CLOSED"
            and final_boundary.get("state") == STATE_SOURCE_DEACTIVATED
            and final_boundary.get("problems") == []
        ):
            raise QuarantineError(
                "quarantine boundary changed during global writer claim release"
            )
        release_proof_sha = _release_registry_lock_to_archive(
            owner,
            archive_path=archive_path,
            expected_lock_bytes=owned_lock_bytes,
        )
        owner = None
        return {
            "schema": RESULT_SCHEMA,
            "status": "QUARANTINED",
            "transaction_id": transaction_id,
            "source_generation_cas": snapshot.generation_cas,
            "archive_path": str(archive_path),
            "archive_manifest_sha256": manifest_sha,
            "terminal_state_hash": source_deactivated["state_hash"],
            "lock_release_proof_sha256": release_proof_sha,
            "archived_files": len(manifest["archive_files"]),
            "archived_receipts": len(snapshot.receipts),
        }
    except BaseException as exc:
        if archive_published:
            assert archive_path is not None
            raise QuarantineRecoveryRequired(
                "durable quarantine archive exists; recovery is fail-closed and "
                "requires an operator",
                transaction_id=transaction_id,
                archive_path=archive_path,
            ) from exc
        cleanup_error: BaseException | None = None
        if stage_path is not None and transaction_root is not None:
            try:
                _remove_stage(stage_path, transaction_root)
            except BaseException as stage_exc:  # noqa: BLE001
                cleanup_error = stage_exc
        if cleanup_error is None and global_claim is not None:
            try:
                writer_claim.release_global_market_writer(
                    paths.shared_writer_claim_path,
                    run_id=run_id,
                    owner_pid=os.getpid(),
                    ownership_token=str(global_claim["ownership_token"]),
                    final_status="REGISTRY_QUARANTINE_ABORTED_BEFORE_PUBLISH",
                )
                global_claim = None
            except BaseException as claim_exc:  # noqa: BLE001
                cleanup_error = claim_exc
        if cleanup_error is None and owner is not None:
            try:
                registry.release_registry_lock(owner)
                owner = None
            except BaseException as lock_exc:  # noqa: BLE001
                cleanup_error = cleanup_error or lock_exc
        if cleanup_error is not None:
            recovery_path = stage_path or transaction_root or paths.quarantine_root
            raise QuarantineRecoveryRequired(
                "quarantine failed before archive publication and cleanup is incomplete; "
                "global and registry locks remain fail-closed",
                transaction_id=transaction_id or "prepublication-recovery",
                archive_path=recovery_path,
            ) from cleanup_error
        if isinstance(exc, QuarantineError):
            raise
        raise QuarantineError(f"quarantine failed before commit: {exc}") from exc


def _load_json_object(path: Path) -> dict[str, Any]:
    if _is_link_like(path) or not path.is_file():
        raise QuarantineError(f"archive object is not one regular file: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise QuarantineError(f"archive object is unreadable: {path.name}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise QuarantineError(f"archive object is not a mapping: {path.name}")
    return dict(payload)


def _verify_archive_files(archive_path: Path, manifest: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    files = manifest.get("archive_files")
    if not isinstance(files, list):
        return ["archive manifest file list is malformed"]
    roles: list[str] = []
    names: list[str] = []
    for record in files:
        if not isinstance(record, Mapping):
            problems.append("archive file record is malformed")
            continue
        name = str(record.get("archive_name") or "")
        role = str(record.get("role") or "")
        roles.append(role)
        names.append(name)
        if Path(name).name != name or not name:
            problems.append("archive file name is unsafe")
            continue
        path = archive_path / name
        if _is_link_like(path) or not path.is_file():
            problems.append(f"archive file {name} is not one regular file")
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            problems.append(f"archive file {name} is unreadable: {exc}")
            continue
        if len(raw) != record.get("size"):
            problems.append(f"archive file {name} size mismatch")
        if hashlib.sha256(raw).hexdigest() != record.get("sha256"):
            problems.append(f"archive file {name} sha256 mismatch")
    source = manifest.get("source")
    summary_present = bool(
        isinstance(source, Mapping) and source.get("summary_present") is True
    )
    expected_roles = {"registry", "mutation_receipt_archive"}
    if summary_present:
        expected_roles.add("summary")
    if len(roles) != len(set(roles)) or set(roles) != expected_roles:
        problems.append("archive file roles are not the exact expected set")
    expected_names = {"registry.bin", "mutation-receipts.jsonl"}
    if summary_present:
        expected_names.add("summary.bin")
    if len(names) != len(set(names)) or set(names) != expected_names:
        problems.append("archive file names are not the exact expected set")
    return problems


def _verify_receipt_archive(
    archive_path: Path,
    manifest: Mapping[str, Any],
) -> tuple[list[str], tuple[RawReceipt, ...]]:
    problems: list[str] = []
    expected = manifest.get("receipt_entries")
    if not isinstance(expected, list):
        return ["receipt entry manifest is malformed"], ()
    expected_by_name = {
        str(item.get("original_name")): dict(item)
        for item in expected
        if isinstance(item, Mapping)
    }
    if len(expected_by_name) != len(expected):
        problems.append("receipt entry manifest contains duplicate or malformed names")
    path = archive_path / "mutation-receipts.jsonl"
    if _is_link_like(path) or not path.is_file():
        return ["receipt byte archive is not one regular file"], ()
    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        return [f"receipt byte archive is unreadable: {exc}"], ()
    seen: set[str] = set()
    recovered: list[RawReceipt] = []
    for line in lines:
        try:
            row = json.loads(line.decode("utf-8"))
            name = str(row["original_name"])
            raw = base64.b64decode(row["content_b64"], validate=True)
        except (binascii.Error, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            problems.append(f"receipt byte archive row is invalid: {exc}")
            continue
        expected_row = expected_by_name.get(name)
        if (
            row.get("schema") != RECEIPT_ARCHIVE_SCHEMA
            or expected_row is None
            or name in seen
        ):
            problems.append(f"receipt byte archive identity is invalid: {name}")
            continue
        seen.add(name)
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) != row.get("size") or digest != row.get("sha256"):
            problems.append(f"receipt byte archive row content mismatch: {name}")
        if len(raw) != expected_row.get("size") or digest != expected_row.get("sha256"):
            problems.append(f"receipt byte archive differs from manifest: {name}")
        recovered.append(RawReceipt(name=name, content=raw))
    if seen != set(expected_by_name):
        problems.append("receipt byte archive does not contain the exact manifest set")
    return problems, tuple(sorted(recovered, key=lambda receipt: receipt.name))


def _load_state(
    archive_path: Path,
    *,
    transaction_id: str,
    state: str,
    previous_state_hash: str | None,
    archive_manifest_sha256: str,
    source_generation_cas: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    path = archive_path / STATE_FILES[state]
    if not _path_exists(path):
        return None, []
    try:
        record = _load_json_object(path)
    except QuarantineError as exc:
        return None, [str(exc)]
    claimed = record.get("state_hash")
    computed = hashlib.sha256(
        canonical_json_bytes(
            {key: value for key, value in record.items() if key != "state_hash"}
        )
    ).hexdigest()
    problems: list[str] = []
    if record.get("schema") != STATE_SCHEMA:
        problems.append(f"{path.name} schema mismatch")
    if record.get("transaction_id") != transaction_id or record.get("state") != state:
        problems.append(f"{path.name} identity mismatch")
    if record.get("previous_state_hash") != previous_state_hash:
        problems.append(f"{path.name} state chain mismatch")
    if claimed != computed:
        problems.append(f"{path.name} hash mismatch")
    if record.get("archive_manifest_sha256") != archive_manifest_sha256:
        problems.append(f"{path.name} archive manifest binding mismatch")
    if record.get("source_generation_cas") != source_generation_cas:
        problems.append(f"{path.name} source generation CAS mismatch")
    return record, problems


def _load_lock_release_proof(
    archive_path: Path,
    *,
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    path = archive_path / LOCK_RELEASE_PROOF_FILE
    if not _path_exists(path):
        return None, []
    if _is_link_like(path) or not path.is_file():
        return None, ["registry lock release proof is not one regular file"]
    try:
        raw = path.read_bytes()
        proof = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, [f"registry lock release proof is unreadable: {exc}"]
    if not isinstance(proof, Mapping):
        return None, ["registry lock release proof is not a mapping"]
    proof = dict(proof)
    authority = manifest.get("authority")
    if not isinstance(authority, Mapping):
        return proof, ["archive authority is malformed"]
    problems: list[str] = []
    if hashlib.sha256(raw).hexdigest() != authority.get("registry_lock_sha256"):
        problems.append("registry lock release proof sha256 mismatch")
    if proof.get("schema") != "premarket_perp_registry_lock_v1":
        problems.append("registry lock release proof schema mismatch")
    if proof.get("run_id") != manifest.get("run_id"):
        problems.append("registry lock release proof run_id mismatch")
    if proof.get("plan_hash") != authority.get("plan_hash"):
        problems.append("registry lock release proof plan_hash mismatch")
    for field in ("owner_pid", "owner_host", "nonce", "acquired_at_utc"):
        if proof.get(field) in (None, ""):
            problems.append(f"registry lock release proof is missing {field}")
    return proof, problems


def _verify_retained_source_tombstones(
    paths: QuarantinePaths,
    *,
    transaction_id: str,
    manifest: Mapping[str, Any],
    source_deactivated: Mapping[str, Any],
) -> list[str]:
    problems: list[str] = []
    source = manifest.get("source")
    details = source_deactivated.get("details")
    tombstones = (
        details.get("retained_source_tombstones")
        if isinstance(details, Mapping)
        else None
    )
    expected_roles = {"registry"}
    if isinstance(source, Mapping) and source.get("summary_present") is True:
        expected_roles.add("summary")
    if isinstance(source, Mapping) and source.get("receipt_dir_present") is True:
        expected_roles.add("mutation_receipts")
    if not isinstance(tombstones, Mapping) or set(tombstones) != expected_roles:
        return ["source deactivation tombstone set is invalid"]

    expected_paths = {
        "registry": paths.registry_path.with_name(
            f".{paths.registry_path.name}.quarantine-{transaction_id}.registry.deactivated"
        ),
        "summary": paths.summary_path.with_name(
            f".{paths.summary_path.name}.quarantine-{transaction_id}.summary.deactivated"
        ),
        "mutation_receipts": paths.receipt_dir.with_name(
            f".{paths.receipt_dir.name}.quarantine-{transaction_id}.mutation_receipts.deactivated"
        ),
    }
    for role in expected_roles:
        path = expected_paths[role]
        if tombstones.get(role) != path.name:
            problems.append(f"{role} tombstone name mismatch")
            continue
        try:
            if role == "mutation_receipts":
                receipt_problems, expected_receipts = _verify_receipt_archive(
                    archive_path=paths.quarantine_root / transaction_id,
                    manifest=manifest,
                )
                problems.extend(receipt_problems)
                if _is_link_like(path) or not path.is_dir():
                    problems.append("mutation receipt tombstone is not one directory")
                    continue
                entries = sorted(path.iterdir(), key=lambda item: item.name)
                if any(_is_link_like(item) or not item.is_file() for item in entries):
                    problems.append("mutation receipt tombstone shape mismatch")
                    continue
                actual = tuple(RawReceipt(item.name, item.read_bytes()) for item in entries)
                if actual != expected_receipts:
                    problems.append("mutation receipt tombstone bytes mismatch")
            else:
                archive_name = "registry.bin" if role == "registry" else "summary.bin"
                expected = (paths.quarantine_root / transaction_id / archive_name).read_bytes()
                if _is_link_like(path) or not path.is_file() or path.read_bytes() != expected:
                    problems.append(f"{role} tombstone bytes mismatch")
        except OSError as exc:
            problems.append(f"{role} tombstone is unreadable: {exc}")
    for role, canonical in (
        ("registry", paths.registry_path),
        ("summary", paths.summary_path),
        ("mutation_receipts", paths.receipt_dir),
    ):
        if _path_exists(canonical):
            problems.append(f"canonical {role} source exists after deactivation")
    return problems


def _transaction_location(paths: QuarantinePaths, transaction_id: str) -> Path:
    transaction_id = _validate_transaction_id(transaction_id)
    published = paths.quarantine_root / transaction_id
    staged = paths.quarantine_root / ".transactions" / f"{transaction_id}.staging"
    if published.is_dir() and not _is_link_like(published):
        return published
    if staged.is_dir() and not _is_link_like(staged):
        return staged
    raise QuarantineError("quarantine transaction does not exist")


def quarantine_transaction_status(transaction_id: str) -> dict[str, Any]:
    """Verify archive bytes and immutable states without changing recovery state."""
    paths = _production_paths()
    transaction_id = _validate_transaction_id(transaction_id)
    archive_path = _transaction_location(paths, transaction_id)
    problems: list[str] = []
    try:
        manifest = _load_json_object(archive_path / "archive-manifest.json")
    except QuarantineError as exc:
        manifest = {}
        problems.append(str(exc))
    if manifest.get("schema") != ARCHIVE_SCHEMA:
        problems.append("archive manifest schema mismatch")
    if manifest.get("transaction_id") != transaction_id:
        problems.append("archive manifest transaction identity mismatch")
    problems.extend(_verify_archive_files(archive_path, manifest))
    receipt_problems, recovered_receipts = _verify_receipt_archive(
        archive_path, manifest
    )
    problems.extend(receipt_problems)

    manifest_path = archive_path / "archive-manifest.json"
    try:
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    except OSError:
        manifest_sha = ""
    source_generation_cas = str(manifest.get("source_generation_cas") or "")
    source = manifest.get("source")
    if isinstance(source, Mapping):
        try:
            registry_bytes = (archive_path / "registry.bin").read_bytes()
            summary_bytes = (
                (archive_path / "summary.bin").read_bytes()
                if source.get("summary_present") is True
                else None
            )
            descriptor = _generation_descriptor(
                registry_name=str(source.get("registry_name") or ""),
                registry_bytes=registry_bytes,
                summary_name=str(source.get("summary_name") or ""),
                summary_bytes=summary_bytes,
                receipt_dir_name=str(source.get("receipt_dir_name") or ""),
                receipt_dir_present=source.get("receipt_dir_present") is True,
                receipts=recovered_receipts,
            )
            if canonical_hash(descriptor) != source_generation_cas:
                problems.append("archive bytes do not reconstruct the source generation CAS")
        except OSError as exc:
            problems.append(f"cannot reconstruct source generation CAS: {exc}")
    else:
        problems.append("archive source descriptor is malformed")

    prepared, state_problems = _load_state(
        archive_path,
        transaction_id=transaction_id,
        state=STATE_PREPARED,
        previous_state_hash=None,
        archive_manifest_sha256=manifest_sha,
        source_generation_cas=source_generation_cas,
    )
    problems.extend(state_problems)
    if prepared is None:
        problems.append("PREPARED state is missing or invalid")
    durable = None
    source_deactivated = None
    lock_released = None
    if prepared is not None:
        durable, state_problems = _load_state(
            archive_path,
            transaction_id=transaction_id,
            state=STATE_ARCHIVE_DURABLE,
            previous_state_hash=str(prepared.get("state_hash")),
            archive_manifest_sha256=manifest_sha,
            source_generation_cas=source_generation_cas,
        )
        problems.extend(state_problems)
    elif _path_exists(archive_path / STATE_FILES[STATE_ARCHIVE_DURABLE]):
        problems.append("ARCHIVE_DURABLE state exists without PREPARED")
    if durable is not None:
        source_deactivated, state_problems = _load_state(
            archive_path,
            transaction_id=transaction_id,
            state=STATE_SOURCE_DEACTIVATED,
            previous_state_hash=str(durable.get("state_hash")),
            archive_manifest_sha256=manifest_sha,
            source_generation_cas=source_generation_cas,
        )
        problems.extend(state_problems)
    elif _path_exists(archive_path / STATE_FILES[STATE_SOURCE_DEACTIVATED]):
        problems.append("SOURCE_DEACTIVATED state exists without ARCHIVE_DURABLE")
    if source_deactivated is not None:
        problems.extend(
            _verify_retained_source_tombstones(
                paths,
                transaction_id=transaction_id,
                manifest=manifest,
                source_deactivated=source_deactivated,
            )
        )
        lock_released, state_problems = _load_lock_release_proof(
            archive_path,
            manifest=manifest,
        )
        problems.extend(state_problems)
    elif _path_exists(archive_path / LOCK_RELEASE_PROOF_FILE):
        problems.append("lock release proof exists without SOURCE_DEACTIVATED")
    if lock_released is not None and _path_exists(paths.lock_path):
        try:
            current_lock = paths.lock_path.read_bytes()
            authority = manifest.get("authority")
            released_sha = (
                authority.get("registry_lock_sha256")
                if isinstance(authority, Mapping)
                else None
            )
            if hashlib.sha256(current_lock).hexdigest() == released_sha:
                problems.append(
                    "released registry lock still exists at the canonical path"
                )
        except OSError as exc:
            problems.append(f"canonical registry lock is unreadable: {exc}")

    manifest_files = manifest.get("archive_files")
    declared_names = {
        str(item.get("archive_name"))
        for item in manifest_files
        if isinstance(manifest_files, list) and isinstance(item, Mapping)
    } if isinstance(manifest_files, list) else set()
    allowed_names = {"archive-manifest.json"} | declared_names | {
        name for name in STATE_FILES.values() if _path_exists(archive_path / name)
    }
    if _path_exists(archive_path / LOCK_RELEASE_PROOF_FILE):
        allowed_names.add(LOCK_RELEASE_PROOF_FILE)
    try:
        unexpected = sorted(
            item.name
            for item in archive_path.iterdir()
            if item.name not in allowed_names
        )
    except OSError as exc:
        unexpected = []
        problems.append(f"cannot enumerate quarantine archive: {exc}")
    if unexpected:
        problems.append("quarantine archive contains unexpected entries")

    if problems:
        status = "INVALID_ARCHIVE_FAIL_CLOSED"
    elif lock_released is not None:
        status = "COMPLETED"
    else:
        status = "RECOVERY_REQUIRED_FAIL_CLOSED"
    return {
        "schema": RESULT_SCHEMA,
        "status": status,
        "transaction_id": transaction_id,
        "state": (
            STATE_LOCK_RELEASED
            if lock_released is not None
            else STATE_SOURCE_DEACTIVATED
            if source_deactivated is not None
            else STATE_ARCHIVE_DURABLE
            if durable is not None
            else STATE_PREPARED
            if prepared is not None
            else "UNKNOWN"
        ),
        "problems": problems,
        "source_generation_cas": manifest.get("source_generation_cas"),
    }


def recover_quarantine_transaction(transaction_id: str) -> None:
    """Honest boundary: recovery is not implemented and therefore never mutates."""
    paths = _production_paths()
    archive_path = _transaction_location(paths, transaction_id)
    quarantine_transaction_status(transaction_id)
    raise QuarantineRecoveryRequired(
        "automatic recovery is disabled; inspect the archived bytes, source remnants, "
        "and retained registry lock before an operator-authored recovery",
        transaction_id=transaction_id,
        archive_path=archive_path,
    )


def main(argv: list[str] | None = None) -> int:
    """Expose read-only inspection/status and the CAS-bound write as separate modes."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Fail-closed pre-market registry quarantine transaction."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect", action="store_true")
    mode.add_argument("--status", metavar="TRANSACTION_ID")
    mode.add_argument("--quarantine", action="store_true")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--expected-generation-cas", default="")
    args = parser.parse_args(argv)

    if args.inspect:
        result = inspect_quarantine_candidate()
    elif args.status:
        result = quarantine_transaction_status(args.status)
    else:
        if not args.run_id or not args.reason or not args.expected_generation_cas:
            parser.error(
                "--quarantine requires --run-id, --reason and "
                "--expected-generation-cas"
            )
        result = quarantine_registry(
            run_id=args.run_id,
            reason=args.reason,
            expected_generation_cas=args.expected_generation_cas,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
