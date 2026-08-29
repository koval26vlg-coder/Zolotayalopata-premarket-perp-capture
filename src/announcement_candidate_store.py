"""Append-only storage for unverified announcement-discovery candidates.

The records written here are discovery hints, not official listing evidence.  In
particular this module refuses fields which could make a candidate look capture
eligible.  Candidate identity is stable across article-content revisions, while
the global record chain and the per-candidate revision chain make every change
auditable.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import unicodedata
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import project_config as config
from canonical_hash import canonical_json_bytes


CANDIDATE_SCHEMA = "premarket_official_announcement_candidate_v1"
CANDIDATE_RECORD_TYPE = "unverified_announcement_candidate"
LOCK_SCHEMA = "premarket_perp_announcement_candidate_store_lock_v1"
EVIDENCE_CLASS = "UNVERIFIED_ANNOUNCEMENT_DISCOVERY"
IDENTITY_MATCH_BASIS = "EXACT_TICKER_TOKEN_HEURISTIC_ONLY"
REVIEW_STATE = "HUMAN_ATTESTATION_REQUIRED"
IDENTITY_AUTHORITY = "NONE_UNTIL_HUMAN_SAME_UNDERLYING_ATTESTATION"

_AUTHORITY_FIELDS = frozenset(
    {"official_spot_t0", "capture_eligible", "evidence_use"}
)
_STORE_MANAGED_FIELDS = frozenset(
    {
        "record_type",
        "record_seq",
        "content_hash",
        "content_revision",
        "previous_record_hash",
        "supersedes_record_hash",
        "record_hash",
        "write_run_id",
    }
)
_REQUIRED_TEXT_FIELDS = (
    "candidate_id",
    "episode_id",
    "perpetual_venue",
    "premarket_contract_id",
    "asset_class",
    "issuer_namespace",
    "issuer_id",
    "asset_identity_hash",
    "registry_sha256",
    "registry_tail_record_hash",
    "mutation_receipt_hash",
    "summary_content_hash",
    "registry_authority_state_hash",
    "plan_id",
    "plan_hash",
    "metadata_refresh_received_at",
    "listing_venue",
    "article_id",
    "article_title",
    "article_url",
    "source_payload_sha256",
    "detected_at_utc",
)
_CANDIDATE_FIELDS = frozenset(
    {
        "schema",
        "candidate_id",
        "evidence_class",
        "review_state",
        "identity_match_basis",
        "identity_authority",
        "episode_id",
        "perpetual_venue",
        "premarket_contract_id",
        "asset_class",
        "issuer_namespace",
        "issuer_id",
        "lifecycle_generation",
        "asset_identity_hash",
        "registry_sha256",
        "registry_tail_record_hash",
        "mutation_receipt_seq",
        "mutation_receipt_hash",
        "summary_content_hash",
        "registry_authority_state_hash",
        "plan_id",
        "plan_hash",
        "metadata_refresh_received_at",
        "listing_venue",
        "article_id",
        "article_title",
        "article_url",
        "article_published_at_ms",
        "source_page",
        "source_payload_sha256",
        "detected_at_utc",
        "article_body_fetched",
        "registry_write",
        "human_attestation_required",
    }
)
_HEX_HASH_FIELDS = (
    "candidate_id",
    "asset_identity_hash",
    "registry_sha256",
    "registry_tail_record_hash",
    "mutation_receipt_hash",
    "summary_content_hash",
    "registry_authority_state_hash",
    "plan_hash",
    "source_payload_sha256",
)
_CONTENT_HASH_IGNORED_FIELDS = frozenset({"detected_at_utc"})


class CandidateStoreError(RuntimeError):
    """The candidate input, lock or persisted hash chain is invalid."""


@dataclass(frozen=True)
class CandidateStoreLockOwner:
    store_path: Path
    lock_path: Path
    owner_pid: int
    owner_host: str
    run_id: str
    nonce: str


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


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


def _is_explicit_utc(value: Any) -> bool:
    if not _is_canonical_text(value):
        return False
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return moment.tzinfo is not None and moment.utcoffset() == timezone.utc.utcoffset(None)


def _forbidden_authority_path(value: Any, prefix: str = "candidate") -> str | None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            if not isinstance(raw_key, str):
                return f"{prefix} has a non-string field name"
            if raw_key in _AUTHORITY_FIELDS:
                return f"{prefix}.{raw_key} is forbidden authority"
            found = _forbidden_authority_path(nested, f"{prefix}.{raw_key}")
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            found = _forbidden_authority_path(nested, f"{prefix}[{index}]")
            if found:
                return found
    return None


def _identity_payload(candidate: Mapping[str, Any]) -> dict[str, str]:
    return {
        "episode_id": str(candidate.get("episode_id") or ""),
        "listing_venue": str(candidate.get("listing_venue") or ""),
        "article_id": str(candidate.get("article_id") or ""),
    }


def make_candidate_id(
    candidate: Mapping[str, Any] | None = None,
    *,
    episode_id: str | None = None,
    listing_venue: str | None = None,
    article_id: str | None = None,
) -> str:
    """Return the stable episode/listing-venue/article identity hash.

    Discovery uses the explicit keyword form while store verification uses the
    already-materialised mapping form.  Both routes feed the same canonical
    identity payload.
    """
    if candidate is not None:
        if any(value is not None for value in (episode_id, listing_venue, article_id)):
            raise CandidateStoreError("candidate identity has conflicting inputs")
        identity = _identity_payload(candidate)
    else:
        identity = {
            "episode_id": episode_id or "",
            "listing_venue": listing_venue or "",
            "article_id": article_id or "",
        }
    if any(not _is_canonical_text(value) for value in identity.values()):
        raise CandidateStoreError("candidate identity fields are missing or non-canonical")
    return _sha256(identity)


def _candidate_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in _STORE_MANAGED_FIELDS
    }


def _content_hash(candidate: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in candidate.items()
        if key not in _CONTENT_HASH_IGNORED_FIELDS
    }
    return _sha256(semantic)


def _record_hash(record: Mapping[str, Any]) -> str:
    return _sha256({key: value for key, value in record.items() if key != "record_hash"})


def _validate_article_url(value: Any, *, listing_venue: Any) -> None:
    if not _is_canonical_text(value) or "\\" in value:
        raise CandidateStoreError("candidate article_url is not canonical HTTPS")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise CandidateStoreError("candidate article_url is malformed") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or parsed.query
    ):
        raise CandidateStoreError("candidate article_url is not canonical HTTPS")
    if not _is_canonical_text(listing_venue):
        raise CandidateStoreError("candidate listing_venue is non-canonical")
    venue = str(listing_venue)
    host = str(parsed.hostname).lower()
    if venue not in {"bybit", "bitget", "kucoin"}:
        raise CandidateStoreError("candidate listing_venue is outside discovery scope")
    if host not in config.OFFICIAL_ANNOUNCEMENT_HOSTS.get(venue, ()):
        raise CandidateStoreError(
            f"candidate article host is not official for {venue}: {host}"
        )
    path = parsed.path
    decoded_segments = urllib.parse.unquote(path).split("/")
    if not path.startswith("/") or ".." in decoded_segments:
        raise CandidateStoreError("candidate article path is not canonical")
    if venue == "bybit":
        bybit_path_ok = (
            host == "announcements.bybit.com" and "/article/" in path
        ) or (
            host == "www.bybit.com" and path.startswith("/en/help-center/article/")
        )
        if not bybit_path_ok:
            raise CandidateStoreError(
                "candidate Bybit article path is outside the official article index"
            )
    elif venue == "bitget" and not path.startswith("/support/articles/"):
        raise CandidateStoreError(
            "candidate Bitget article path is outside the support index"
        )
    elif venue == "kucoin" and not path.startswith("/announcement/"):
        raise CandidateStoreError(
            "candidate KuCoin article path is outside the announcement index"
        )
    canonical = urllib.parse.urlunsplit(("https", host, path, "", ""))
    if value != canonical:
        raise CandidateStoreError("candidate article_url is not canonical")


def _validate_candidate_payload(candidate: Mapping[str, Any]) -> None:
    authority = _forbidden_authority_path(candidate)
    if authority:
        raise CandidateStoreError(authority)
    managed = sorted(_STORE_MANAGED_FIELDS.intersection(candidate))
    if managed:
        raise CandidateStoreError(
            "candidate attempts to supply store-managed fields: " + ", ".join(managed)
        )
    unknown_schema = candidate.get("schema")
    if unknown_schema not in (None, CANDIDATE_SCHEMA):
        raise CandidateStoreError("candidate schema is unknown")
    supplied_fields = frozenset(candidate)
    unknown_fields = sorted(supplied_fields - _CANDIDATE_FIELDS)
    missing_fields = sorted(_CANDIDATE_FIELDS - supplied_fields)
    if unknown_fields:
        raise CandidateStoreError(
            "candidate has fields outside the exact schema: "
            + ", ".join(unknown_fields)
        )
    if missing_fields:
        raise CandidateStoreError(
            "candidate is missing exact-schema fields: " + ", ".join(missing_fields)
        )
    for field in _REQUIRED_TEXT_FIELDS:
        if not _is_canonical_text(candidate.get(field)):
            raise CandidateStoreError(f"candidate {field} is missing or non-canonical")
    for field in _HEX_HASH_FIELDS:
        if not _is_sha256(candidate.get(field)):
            raise CandidateStoreError(f"candidate {field} is not a SHA-256 value")
    if candidate.get("evidence_class") != EVIDENCE_CLASS:
        raise CandidateStoreError("candidate evidence_class cannot confer authority")
    if candidate.get("identity_match_basis") != IDENTITY_MATCH_BASIS:
        raise CandidateStoreError("candidate identity_match_basis is not heuristic-only")
    if candidate.get("review_state") != REVIEW_STATE:
        raise CandidateStoreError("candidate review_state does not require attestation")
    if candidate.get("identity_authority") != IDENTITY_AUTHORITY:
        raise CandidateStoreError("candidate identity_authority must remain NONE")
    if candidate.get("article_body_fetched") is not False:
        raise CandidateStoreError("candidate article_body_fetched must remain false")
    if candidate.get("registry_write") is not False:
        raise CandidateStoreError("candidate registry_write must remain false")
    if candidate.get("human_attestation_required") is not True:
        raise CandidateStoreError(
            "candidate human_attestation_required must remain true"
        )
    if candidate.get("asset_class") != "CRYPTO_TOKEN":
        raise CandidateStoreError("candidate asset_class must be CRYPTO_TOKEN")
    for field in ("lifecycle_generation", "mutation_receipt_seq"):
        if not _is_nonnegative_int(candidate.get(field)):
            raise CandidateStoreError(f"candidate {field} is not a non-negative integer")
    source_page = candidate.get("source_page")
    if not isinstance(source_page, int) or isinstance(source_page, bool) or source_page < 1:
        raise CandidateStoreError("candidate source_page is not a positive integer")
    published_at = candidate.get("article_published_at_ms")
    if published_at is not None and not _is_nonnegative_int(published_at):
        raise CandidateStoreError("candidate article_published_at_ms is malformed")
    for field in ("metadata_refresh_received_at", "detected_at_utc"):
        if not _is_explicit_utc(candidate.get(field)):
            raise CandidateStoreError(f"candidate {field} is not explicit UTC")
    _validate_article_url(
        candidate.get("article_url"),
        listing_venue=candidate.get("listing_venue"),
    )
    if candidate.get("candidate_id") != make_candidate_id(candidate):
        raise CandidateStoreError("candidate_id does not match deterministic identity")


def _prepare_candidate(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CandidateStoreError("candidate must be a mapping")
    authority = _forbidden_authority_path(raw)
    if authority:
        raise CandidateStoreError(authority)
    try:
        # Canonical round-trip rejects unsupported objects, NaN and Infinity while
        # ensuring the caller cannot mutate nested structures after validation.
        candidate = json.loads(canonical_json_bytes(dict(raw)).decode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise CandidateStoreError("candidate is not canonical JSON") from exc
    if not isinstance(candidate, dict):  # defensive: Mapping may have odd conversion
        raise CandidateStoreError("candidate must be a JSON object")
    candidate.setdefault("schema", CANDIDATE_SCHEMA)
    _validate_candidate_payload(candidate)
    return candidate


def _validate_run_id(run_id: Any) -> str:
    if not _is_canonical_text(run_id) or len(run_id) > 256:
        raise CandidateStoreError("candidate store run_id is missing or non-canonical")
    return run_id


def _lock_path(store_path: Path) -> Path:
    return Path(str(store_path) + ".lock")


def acquire_candidate_store_lock(
    path: str | os.PathLike[str], *, run_id: str
) -> CandidateStoreLockOwner:
    store_path = Path(path)
    run_id = _validate_run_id(run_id)
    lock_path = _lock_path(store_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    owner = CandidateStoreLockOwner(
        store_path=store_path,
        lock_path=lock_path,
        owner_pid=os.getpid(),
        owner_host=socket.gethostname(),
        run_id=run_id,
        nonce=secrets.token_hex(32),
    )
    payload = {
        "schema": LOCK_SCHEMA,
        "store_path": str(store_path.resolve(strict=False)),
        "owner_pid": owner.owner_pid,
        "owner_host": owner.owner_host,
        "run_id": owner.run_id,
        "nonce": owner.nonce,
    }
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise CandidateStoreError(f"CANDIDATE_STORE_LOCKED: {lock_path}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            lock_path.unlink(missing_ok=True)
        finally:
            raise
    return owner


def _assert_lock_owner(owner: CandidateStoreLockOwner, store_path: Path) -> None:
    if not isinstance(owner, CandidateStoreLockOwner):
        raise CandidateStoreError("candidate store lock owner has the wrong type")
    if owner.store_path.resolve(strict=False) != store_path.resolve(strict=False):
        raise CandidateStoreError("candidate store lock does not cover the target path")
    try:
        payload = json.loads(owner.lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CandidateStoreError("candidate store lock is unreadable") from exc
    expected = {
        "schema": LOCK_SCHEMA,
        "store_path": str(store_path.resolve(strict=False)),
        "owner_pid": owner.owner_pid,
        "owner_host": owner.owner_host,
        "run_id": owner.run_id,
        "nonce": owner.nonce,
    }
    if not isinstance(payload, dict) or any(
        payload.get(field) != value for field, value in expected.items()
    ):
        raise CandidateStoreError("candidate store lock owner mismatch")


def release_candidate_store_lock(owner: CandidateStoreLockOwner) -> None:
    _assert_lock_owner(owner, owner.store_path)
    owner.lock_path.unlink()


@contextmanager
def candidate_store_lock(
    path: str | os.PathLike[str], run_id: str
) -> Iterator[CandidateStoreLockOwner]:
    """Hold the per-store O_EXCL lock, including across bounded discovery I/O."""
    owner = acquire_candidate_store_lock(path, run_id=run_id)
    try:
        yield owner
    finally:
        release_candidate_store_lock(owner)


def _load_and_verify(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if not path.is_file():
        raise CandidateStoreError("candidate store path is not a regular file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CandidateStoreError("candidate store is unreadable") from exc
    if not raw:
        raise CandidateStoreError("existing candidate store is empty")
    if not raw.endswith(b"\n"):
        raise CandidateStoreError("candidate store has a truncated final record")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateStoreError("candidate store is not UTF-8") from exc

    records: list[dict[str, Any]] = []
    previous_global_hash: str | None = None
    candidate_heads: dict[str, dict[str, Any]] = {}
    seen_hashes: set[str] = set()
    for index, line in enumerate(text.splitlines()):
        line_number = index + 1
        if not line:
            raise CandidateStoreError(f"candidate store line {line_number} is blank")
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise CandidateStoreError(
                f"candidate store line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(record, dict):
            raise CandidateStoreError(
                f"candidate store line {line_number} is not an object"
            )
        if record.get("schema") != CANDIDATE_SCHEMA:
            raise CandidateStoreError(
                f"candidate store line {line_number} has an unknown schema"
            )
        if record.get("record_type") != CANDIDATE_RECORD_TYPE:
            raise CandidateStoreError(
                f"candidate store line {line_number} has an unknown record_type"
            )
        if record.get("record_seq") != index:
            raise CandidateStoreError(
                f"candidate store line {line_number} has a broken record_seq"
            )
        if record.get("previous_record_hash") != previous_global_hash:
            raise CandidateStoreError(
                f"candidate store line {line_number} has a broken global chain"
            )
        claimed_hash = record.get("record_hash")
        if not _is_sha256(claimed_hash) or claimed_hash != _record_hash(record):
            raise CandidateStoreError(
                f"candidate store line {line_number} has an invalid record_hash"
            )
        if claimed_hash in seen_hashes:
            raise CandidateStoreError(
                f"candidate store line {line_number} repeats a record_hash"
            )
        candidate = _candidate_payload(record)
        _validate_candidate_payload(candidate)
        claimed_content = record.get("content_hash")
        if not _is_sha256(claimed_content) or claimed_content != _content_hash(candidate):
            raise CandidateStoreError(
                f"candidate store line {line_number} has an invalid content_hash"
            )
        candidate_id = candidate["candidate_id"]
        previous_candidate = candidate_heads.get(candidate_id)
        expected_revision = (
            int(previous_candidate["content_revision"]) + 1
            if previous_candidate is not None
            else 0
        )
        if record.get("content_revision") != expected_revision:
            raise CandidateStoreError(
                f"candidate store line {line_number} has a broken content revision"
            )
        expected_supersedes = (
            previous_candidate["record_hash"] if previous_candidate is not None else None
        )
        if record.get("supersedes_record_hash") != expected_supersedes:
            raise CandidateStoreError(
                f"candidate store line {line_number} has a broken candidate chain"
            )
        if (
            previous_candidate is not None
            and previous_candidate["content_hash"] == claimed_content
        ):
            raise CandidateStoreError(
                f"candidate store line {line_number} is a persisted duplicate"
            )
        seen_hashes.add(claimed_hash)
        candidate_heads[candidate_id] = record
        previous_global_hash = claimed_hash
        records.append(record)
    return records


def _append_locked(
    path: Path,
    candidates: Sequence[dict[str, Any]],
    *,
    run_id: str,
    lock_owner: CandidateStoreLockOwner,
) -> dict[str, Any]:
    _assert_lock_owner(lock_owner, path)
    if lock_owner.run_id != run_id:
        raise CandidateStoreError("candidate store lock run_id does not match append")
    existing = _load_and_verify(path)
    candidate_heads = {
        str(record["candidate_id"]): record
        for record in existing
    }
    global_head = existing[-1] if existing else None
    appended: list[dict[str, Any]] = []
    duplicates = 0
    for candidate in candidates:
        candidate_id = str(candidate["candidate_id"])
        content_hash = _content_hash(candidate)
        previous_candidate = candidate_heads.get(candidate_id)
        if (
            previous_candidate is not None
            and previous_candidate["content_hash"] == content_hash
        ):
            duplicates += 1
            continue
        record = dict(candidate)
        record["record_type"] = CANDIDATE_RECORD_TYPE
        record["record_seq"] = int(global_head["record_seq"]) + 1 if global_head else 0
        record["content_hash"] = content_hash
        record["content_revision"] = (
            int(previous_candidate["content_revision"]) + 1
            if previous_candidate is not None
            else 0
        )
        record["previous_record_hash"] = (
            global_head["record_hash"] if global_head is not None else None
        )
        record["supersedes_record_hash"] = (
            previous_candidate["record_hash"] if previous_candidate is not None else None
        )
        record["write_run_id"] = run_id
        record["record_hash"] = _record_hash(record)
        appended.append(record)
        candidate_heads[candidate_id] = record
        global_head = record

    if appended:
        payload = b"".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
            for record in appended
        )
        flags = os.O_WRONLY | os.O_APPEND
        if existing:
            descriptor = os.open(path, flags)
        else:
            try:
                descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise CandidateStoreError(
                    "candidate store appeared after its locked snapshot"
                ) from exc
        try:
            with os.fdopen(descriptor, "ab", buffering=0) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise CandidateStoreError("candidate store append or fsync failed") from exc

    return {
        "appended_records": len(appended),
        "duplicate_records": duplicates,
        "head_record_hash": global_head["record_hash"] if global_head else None,
    }


def append_candidates(
    path: str | os.PathLike[str],
    candidates: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    lock_owner: CandidateStoreLockOwner | None = None,
) -> dict[str, Any]:
    """Validate, de-duplicate and durably append candidate revisions.

    Validation deliberately precedes directory, lock and data-file creation.  A
    caller which already holds ``candidate_store_lock`` may pass its owner so the
    same exclusion covers bounded network discovery and this append.
    """
    run_id = _validate_run_id(run_id)
    if isinstance(candidates, (str, bytes, bytearray)) or not isinstance(
        candidates, Sequence
    ):
        raise CandidateStoreError("candidates must be a sequence of mappings")
    prepared = tuple(_prepare_candidate(candidate) for candidate in candidates)
    store_path = Path(path)
    if lock_owner is not None:
        return _append_locked(
            store_path,
            prepared,
            run_id=run_id,
            lock_owner=lock_owner,
        )
    with candidate_store_lock(store_path, run_id) as owner:
        return _append_locked(
            store_path,
            prepared,
            run_id=run_id,
            lock_owner=owner,
        )
