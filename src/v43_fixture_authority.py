"""Fail-closed, process-local authority for one offline v43 fixture replay.

This module is deliberately disconnected from the production authority path.  It
does not import the risk gate, capture token, writer claim, scheduler, collector,
or any venue adapter.  It only verifies a fixed canonical directory outside every
production root and retains the already sealed L2 replay request behind a random,
single-use in-memory capability.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import re
import secrets
import threading
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from canonical_hash import canonical_json_bytes
import l2_evidence
import project_config as config


HANDOFF_SCHEMA = "premarket_perp_v43_verified_fixture_handoff_v1"
HANDOFF_STATUS = "VERIFIED_FIXTURE_HANDOFF_SINGLE_USE"
REPLAY_RECORD_SCHEMA = "premarket_perp_v43_verified_replay_record_v1"
REPLAY_RECORD_STATUS = "VERIFIED_FIXTURE_REPLAY_RECORD_SINGLE_USE"
RECEIPT_SCHEMA = "premarket_perp_v43_fixture_external_authority_verification_v1"
RECEIPT_STATUS = "FIXTURE_AUTHORITY_CHAIN_OK_NO_CAPTURE_AUTHORITY"

ROOT_FILES = frozenset(
    {
        "plan.json",
        "authority.json",
        "arming.json",
        "proposal.json",
        "lifecycle.json",
        "approval.json",
        "attempt.json",
        "claim-terminal.json",
    }
)
ROOT_ENTRIES = frozenset({*ROOT_FILES, "capture"})
INVARIANT_CHAIN = (
    "external_plan_file_sha256->plan",
    "plan->event",
    "event->arming",
    "arming->proposal",
    "proposal->lifecycle",
    "lifecycle->approval",
    "approval->attempt",
    "attempt->claim_terminal",
    "claim_terminal->capture_manifest",
    "claim_terminal->terminal_receipt",
    "capture_lineage->plan+event+arming+lifecycle+claim_release",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_MAX_ROOT_ARTIFACT_BYTES = 2 * 1024 * 1024

_PLAN_FIELDS = frozenset(
    {
        "schema",
        "plan_id",
        "plan_hash",
        "status",
        "fixture_only",
        "production_capture_authorized",
        "market_data_capture_write_class_authorized",
        "public_data_only",
        "network_allowed",
        "orders_allowed",
        "private_api_allowed",
        "live_execution_allowed",
        "acceptance_capable",
        "event_id",
        "event_lineage_hash",
        "venue",
        "contract_id",
        "official_spot_t0",
        "t0_source_class",
        "t0_precision_sec",
        "official_record_hash",
        "official_source_url",
        "capture_relative_path",
        "max_attempts",
        "plan_content_hash",
    }
)
_ARMING_FIELDS = frozenset(
    {
        "schema",
        "status",
        "fixture_only",
        "plan_id",
        "plan_hash",
        "plan_file_sha256",
        "event_id",
        "event_lineage_hash",
        "official_record_hash",
        "official_spot_t0",
        "t0_source_class",
        "t0_precision_sec",
        "capture_arming_receipt_hash",
        "capture_arming_lineage_hash",
        "arming_receipt_hash",
    }
)
_PROPOSAL_FIELDS = frozenset(
    {
        "schema",
        "status",
        "fixture_only",
        "plan_id",
        "plan_hash",
        "event_id",
        "event_lineage_hash",
        "arming_receipt_hash",
        "official_spot_t0",
        "proposal_hash",
    }
)
_LIFECYCLE_FIELDS = frozenset(
    {
        "schema",
        "status",
        "fixture_only",
        "plan_id",
        "plan_hash",
        "event_id",
        "event_lineage_hash",
        "proposal_hash",
        "contract_id",
        "official_spot_t0",
        "phase_at_entry",
        "terminal_phase",
        "transition_ts",
        "lifecycle_record_hash",
        "capture_lifecycle_lineage_hash",
        "lifecycle_hash",
    }
)
_APPROVAL_FIELDS = frozenset(
    {
        "schema",
        "status",
        "approval_scope",
        "fixture_only",
        "one_shot",
        "plan_id",
        "plan_hash",
        "event_id",
        "event_lineage_hash",
        "proposal_hash",
        "lifecycle_hash",
        "public_data_only",
        "network_allowed",
        "orders_allowed",
        "private_api_allowed",
        "live_execution_allowed",
        "acceptance_capable",
        "approval_hash",
    }
)
_ATTEMPT_FIELDS = frozenset(
    {
        "schema",
        "status",
        "fixture_only",
        "plan_id",
        "plan_hash",
        "event_id",
        "event_lineage_hash",
        "proposal_hash",
        "lifecycle_hash",
        "approval_hash",
        "attempt_number",
        "max_attempts",
        "fixture_token_consumed",
        "fixture_token_hash",
        "claim_id",
        "claim_record_hash",
        "attempt_hash",
    }
)
_CLAIM_TERMINAL_FIELDS = frozenset(
    {
        "schema",
        "status",
        "final_status",
        "fixture_only",
        "released_after_terminal_record",
        "plan_id",
        "plan_hash",
        "event_id",
        "event_lineage_hash",
        "attempt_hash",
        "capture_id",
        "claim_id",
        "claim_record_hash",
        "capture_terminal_record_hash",
        "release_record_hash",
        "manifest_sha256",
        "manifest_hash",
        "terminal_receipt_sha256",
        "terminal_receipt_hash",
        "claim_terminal_hash",
    }
)
_AUTHORITY_FIELDS = frozenset(
    {
        "schema",
        "status",
        "fixture_only",
        "production_authority",
        "capture_authorized",
        "capture_token_issued",
        "network_allowed",
        "orders_allowed",
        "acceptance_capable",
        "invariant_chain",
        "plan_id",
        "plan_hash",
        "plan_file_sha256",
        "event_id",
        "event_lineage_hash",
        "official_record_hash",
        "venue",
        "contract_id",
        "official_spot_t0",
        "capture_id",
        "artifact_sha256",
        "artifact_claim_hashes",
        "authority_hash",
    }
)
_ARTIFACT_SHA_KEYS = frozenset(
    {
        "plan",
        "arming",
        "proposal",
        "lifecycle",
        "approval",
        "attempt",
        "claim_terminal",
        "capture_lineage",
        "capture_manifest",
        "capture_terminal_receipt",
    }
)
_ARTIFACT_CLAIM_KEYS = frozenset(
    {
        "plan_content_hash",
        "arming_receipt_hash",
        "proposal_hash",
        "lifecycle_hash",
        "approval_hash",
        "attempt_hash",
        "claim_terminal_hash",
        "capture_lineage_hash",
        "capture_manifest_hash",
        "capture_terminal_receipt_hash",
    }
)
_RECEIPT_HASH_KEYS = frozenset(
    {
        "authority",
        "arming",
        "proposal",
        "lifecycle",
        "approval",
        "attempt",
        "claim_terminal",
        "manifest",
        "terminal_receipt",
        "lineage",
    }
)


class FixtureAuthorityError(RuntimeError):
    """The external fixture chain cannot yield a replay capability."""


@dataclass(frozen=True, slots=True)
class VerifiedFixtureHandoff:
    """Opaque process-local reference; no replay request is exposed here."""

    schema: str
    status: str
    capability_id: str = field(repr=False)
    verification_hash: str


@dataclass(frozen=True, slots=True)
class VerifiedReplayRecord:
    """Second opaque boundary used only by the private replay callback."""

    schema: str
    status: str
    record_id: str = field(repr=False)
    verification_hash: str


@dataclass(slots=True)
class _CapabilityRecord:
    request: dict[str, Any]
    receipt: dict[str, Any]


_CAPABILITY_LOCK = threading.Lock()
_CAPABILITIES: dict[str, _CapabilityRecord] = {}
_REPLAY_RECORDS: dict[str, _CapabilityRecord] = {}


def _fail(message: str) -> None:
    raise FixtureAuthorityError(message)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _require_sha256(value: object, label: str) -> str:
    if not _is_sha256(value):
        _fail(f"{label} must be a lowercase SHA-256")
    return str(value)


def _require_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _fail(f"{label} is missing or non-canonical")
    return value


def _require_exact_mapping(
    value: object,
    fields: frozenset[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    actual = frozenset(value)
    if actual != fields:
        _fail(
            f"{label} fields mismatch: missing={sorted(fields - actual)}, "
            f"extra={sorted(actual - fields)}"
        )
    return dict(value)


def _claimed_hash(payload: Mapping[str, Any], field_name: str) -> str:
    material = copy.deepcopy(dict(payload))
    material.pop(field_name, None)
    return _sha256(canonical_json_bytes(material))


def _verify_claim(payload: Mapping[str, Any], field_name: str, label: str) -> str:
    claimed = _require_sha256(payload.get(field_name), f"{label}.{field_name}")
    if not secrets.compare_digest(claimed, _claimed_hash(payload, field_name)):
        _fail(f"{label}.{field_name} does not match canonical content")
    return claimed


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    try:
        return path.is_symlink() or bool(is_junction and is_junction(path))
    except OSError as exc:
        raise FixtureAuthorityError("cannot inspect fixture path identity") from exc


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left_text = os.path.normcase(str(left.resolve(strict=False)))
        right_text = os.path.normcase(str(right.resolve(strict=False)))
    except OSError as exc:
        raise FixtureAuthorityError(
            "cannot verify fixture/production path separation"
        ) from exc
    left_drive, _ = os.path.splitdrive(left_text)
    right_drive, _ = os.path.splitdrive(right_text)
    if left_drive and right_drive and left_drive != right_drive:
        return False
    try:
        common = os.path.normcase(os.path.commonpath((left_text, right_text)))
    except ValueError as exc:
        raise FixtureAuthorityError(
            "cannot verify fixture/production path separation"
        ) from exc
    return common in {left_text, right_text}


def _reject_production_path(path: Path) -> None:
    candidate = Path(path)
    if not candidate.is_absolute():
        _fail("fixture authority root must be an absolute external path")
    protected = {
        Path(config.PROJECT_ROOT),
        Path(config.CONTROL_ROOT),
        Path(config.CAPTURE_ROOT),
        Path(config.CAPTURE_TOKEN_PATH),
        Path(config.SHARED_WRITER_CLAIM_PATH),
        Path(config.OFFICIAL_T0_ARMING_ROOT),
        Path(config.EVENT_BOUND_PLAN_PROPOSAL_ROOT),
    }
    if any(_paths_overlap(candidate, root) for root in protected):
        _fail("production, control, capture, or checkout paths are forbidden")


def _same_stat(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )


def _read_canonical_file(path: Path, root: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    if _is_link_or_junction(path):
        _fail(f"{label} must not be a symlink or junction")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise FixtureAuthorityError(f"{label} is missing") from exc
    if resolved.parent != root or resolved.name != path.name or not resolved.is_file():
        _fail(f"{label} escapes the exact fixture layout")
    try:
        before = resolved.stat()
        if before.st_size <= 0 or before.st_size > _MAX_ROOT_ARTIFACT_BYTES:
            _fail(f"{label} has an invalid size")
        raw = resolved.read_bytes()
        after = resolved.stat()
    except OSError as exc:
        raise FixtureAuthorityError(f"{label} cannot be read") from exc
    if not _same_stat(before, after):
        _fail(f"{label} changed during exact readback")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureAuthorityError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        _fail(f"{label} must contain a JSON object")
    if raw != canonical_json_bytes(payload) + b"\n":
        _fail(f"{label} is not canonical JSON with one LF terminator")
    return raw, dict(payload)


def _read_layout(bundle_root: Path) -> tuple[Path, dict[str, bytes], dict[str, dict[str, Any]], Path]:
    supplied = Path(bundle_root)
    _reject_production_path(supplied)
    if _is_link_or_junction(supplied):
        _fail("fixture authority root must not be a symlink or junction")
    try:
        root = supplied.resolve(strict=True)
    except OSError as exc:
        raise FixtureAuthorityError("fixture authority root does not exist") from exc
    if not root.is_dir() or _is_link_or_junction(root):
        _fail("fixture authority root is not a plain directory")
    _reject_production_path(root)
    try:
        before_names = frozenset(entry.name for entry in root.iterdir())
    except OSError as exc:
        raise FixtureAuthorityError("fixture authority root cannot be enumerated") from exc
    if before_names != ROOT_ENTRIES:
        _fail(
            "fixture authority layout mismatch: "
            f"missing={sorted(ROOT_ENTRIES - before_names)}, "
            f"extra={sorted(before_names - ROOT_ENTRIES)}"
        )

    raw_by_name: dict[str, bytes] = {}
    parsed_by_name: dict[str, dict[str, Any]] = {}
    for name in sorted(ROOT_FILES):
        raw, payload = _read_canonical_file(root / name, root, name)
        raw_by_name[name] = raw
        parsed_by_name[name] = payload

    capture = root / "capture"
    if _is_link_or_junction(capture):
        _fail("capture must not be a symlink or junction")
    try:
        capture_resolved = capture.resolve(strict=True)
    except OSError as exc:
        raise FixtureAuthorityError("capture directory is missing") from exc
    if capture_resolved.parent != root or not capture_resolved.is_dir():
        _fail("capture directory escapes the exact fixture layout")
    try:
        after_names = frozenset(entry.name for entry in root.iterdir())
    except OSError as exc:
        raise FixtureAuthorityError("fixture authority root cannot be re-enumerated") from exc
    if after_names != before_names:
        _fail("fixture authority layout changed during exact readback")
    return root, raw_by_name, parsed_by_name, capture_resolved


def _validate_plan(payload: object) -> dict[str, Any]:
    plan = _require_exact_mapping(payload, _PLAN_FIELDS, "plan")
    if (
        plan.get("schema") != "premarket_perp_capture_planonly_v43_fixture_v1"
        or plan.get("status") != "FIXTURE_EVENT_BOUND_NO_CAPTURE_AUTHORITY"
        or plan.get("fixture_only") is not True
        or plan.get("production_capture_authorized") is not False
        or plan.get("market_data_capture_write_class_authorized") is not False
        or plan.get("public_data_only") is not True
        or plan.get("network_allowed") is not False
        or plan.get("orders_allowed") is not False
        or plan.get("private_api_allowed") is not False
        or plan.get("live_execution_allowed") is not False
        or plan.get("acceptance_capable") is not False
        or plan.get("capture_relative_path") != "capture"
        or plan.get("max_attempts") != 1
        or plan.get("t0_source_class") != "OFFICIAL_ANNOUNCEMENT"
        or plan.get("t0_precision_sec") != 1
    ):
        _fail("plan violates the fixture-only research boundary")
    _require_id(plan.get("plan_id"), "plan.plan_id")
    _require_sha256(plan.get("plan_hash"), "plan.plan_hash")
    _require_id(plan.get("event_id"), "plan.event_id")
    _require_sha256(plan.get("event_lineage_hash"), "plan.event_lineage_hash")
    _require_sha256(plan.get("official_record_hash"), "plan.official_record_hash")
    _require_id(plan.get("venue"), "plan.venue")
    if plan.get("venue") != "bybit":
        _fail("fixture authority package is bound to Bybit only")
    _require_id(plan.get("contract_id"), "plan.contract_id")
    if (
        isinstance(plan.get("official_spot_t0"), bool)
        or not isinstance(plan.get("official_spot_t0"), int)
        or int(plan["official_spot_t0"]) <= 0
    ):
        _fail("plan.official_spot_t0 must be a positive integer")
    source_url = plan.get("official_source_url")
    if not isinstance(source_url, str) or "\\" in source_url:
        _fail("plan.official_source_url must be a canonical official source")
    parsed_url = urllib.parse.urlsplit(source_url)
    try:
        explicit_port = parsed_url.port
    except ValueError as exc:
        raise FixtureAuthorityError("plan.official_source_url port is invalid") from exc
    official_hosts = set(config.OFFICIAL_ANNOUNCEMENT_HOSTS.get("bybit", ()))
    if (
        parsed_url.scheme.lower() != "https"
        or (parsed_url.hostname or "").lower() not in official_hosts
        or parsed_url.username is not None
        or parsed_url.password is not None
        or explicit_port is not None
        or parsed_url.fragment
    ):
        _fail("plan.official_source_url is not an approved Bybit announcement URL")
    _verify_claim(plan, "plan_content_hash", "plan")
    return plan


def _validate_artifact(
    payload: object,
    *,
    fields: frozenset[str],
    schema: str,
    status: str,
    hash_field: str,
    label: str,
) -> dict[str, Any]:
    artifact = _require_exact_mapping(payload, fields, label)
    if (
        artifact.get("schema") != schema
        or artifact.get("status") != status
        or artifact.get("fixture_only") is not True
    ):
        _fail(f"{label} schema, status, or fixture-only policy is invalid")
    _verify_claim(artifact, hash_field, label)
    return artifact


def _validate_policy_artifacts(parsed: Mapping[str, object]) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    plan = _validate_plan(parsed["plan.json"])
    arming = _validate_artifact(
        parsed["arming.json"],
        fields=_ARMING_FIELDS,
        schema="premarket_perp_v43_fixture_arming_v1",
        status="FIXTURE_ARMED_NO_CAPTURE_AUTHORITY",
        hash_field="arming_receipt_hash",
        label="arming",
    )
    proposal = _validate_artifact(
        parsed["proposal.json"],
        fields=_PROPOSAL_FIELDS,
        schema="premarket_perp_v43_fixture_proposal_v1",
        status="FIXTURE_PROPOSAL_ONLY",
        hash_field="proposal_hash",
        label="proposal",
    )
    lifecycle = _validate_artifact(
        parsed["lifecycle.json"],
        fields=_LIFECYCLE_FIELDS,
        schema="premarket_perp_v43_fixture_lifecycle_v1",
        status="FIXTURE_TERMINAL_LIFECYCLE",
        hash_field="lifecycle_hash",
        label="lifecycle",
    )
    approval = _validate_artifact(
        parsed["approval.json"],
        fields=_APPROVAL_FIELDS,
        schema="premarket_perp_v43_fixture_approval_v1",
        status="APPROVED_FIXTURE_REPLAY_ONLY",
        hash_field="approval_hash",
        label="approval",
    )
    if (
        approval.get("approval_scope") != "OFFLINE_FIXTURE_REPLAY_ONLY"
        or approval.get("one_shot") is not True
        or approval.get("public_data_only") is not True
        or approval.get("network_allowed") is not False
        or approval.get("orders_allowed") is not False
        or approval.get("private_api_allowed") is not False
        or approval.get("live_execution_allowed") is not False
        or approval.get("acceptance_capable") is not False
    ):
        _fail("approval violates the fixture-only research boundary")
    attempt = _validate_artifact(
        parsed["attempt.json"],
        fields=_ATTEMPT_FIELDS,
        schema="premarket_perp_v43_fixture_attempt_v1",
        status="CONSUMED_FIXTURE_ONLY",
        hash_field="attempt_hash",
        label="attempt",
    )
    if (
        attempt.get("attempt_number") != 1
        or attempt.get("max_attempts") != 1
        or attempt.get("fixture_token_consumed") is not True
    ):
        _fail("attempt must prove one consumed fixture-only token")
    _require_sha256(attempt.get("fixture_token_hash"), "attempt.fixture_token_hash")
    claim_terminal = _validate_artifact(
        parsed["claim-terminal.json"],
        fields=_CLAIM_TERMINAL_FIELDS,
        schema="premarket_perp_v43_fixture_claim_terminal_archive_v1",
        status="RELEASED",
        hash_field="claim_terminal_hash",
        label="claim-terminal",
    )
    if (
        claim_terminal.get("final_status") != "COMPLETED"
        or claim_terminal.get("released_after_terminal_record") is not True
    ):
        _fail("claim-terminal does not prove terminal-before-release ordering")
    authority = _require_exact_mapping(
        parsed["authority.json"], _AUTHORITY_FIELDS, "authority"
    )
    if (
        authority.get("schema")
        != "premarket_perp_v43_fixture_external_authority_v1"
        or authority.get("status")
        != "FIXTURE_AUTHORITY_SEALED_NO_CAPTURE_AUTHORITY"
        or authority.get("fixture_only") is not True
        or authority.get("production_authority") is not False
        or authority.get("capture_authorized") is not False
        or authority.get("capture_token_issued") is not False
        or authority.get("network_allowed") is not False
        or authority.get("orders_allowed") is not False
        or authority.get("acceptance_capable") is not False
        or authority.get("invariant_chain") != list(INVARIANT_CHAIN)
    ):
        _fail("authority violates the fixture-only invariant contract")
    _verify_claim(authority, "authority_hash", "authority")
    return (
        plan,
        arming,
        proposal,
        lifecycle,
        approval,
        attempt,
        claim_terminal,
        authority,
    )


def _validate_hash_maps(
    *,
    root: Path,
    raw: Mapping[str, bytes],
    capture: Path,
    artifacts: Mapping[str, Mapping[str, Any]],
    authority: Mapping[str, Any],
) -> dict[str, bytes]:
    raw_capture: dict[str, bytes] = {}
    for name in ("lineage.json", "manifest.json", "terminal-receipt.json"):
        path = capture / name
        if _is_link_or_junction(path):
            _fail(f"capture/{name} must not be a symlink or junction")
        try:
            resolved = path.resolve(strict=True)
            if resolved.parent != capture or not resolved.is_file():
                _fail(f"capture/{name} escapes the capture directory")
            raw_capture[name] = resolved.read_bytes()
        except OSError as exc:
            raise FixtureAuthorityError(f"capture/{name} cannot be read") from exc

    actual_sha = {
        "plan": _sha256(raw["plan.json"]),
        "arming": _sha256(raw["arming.json"]),
        "proposal": _sha256(raw["proposal.json"]),
        "lifecycle": _sha256(raw["lifecycle.json"]),
        "approval": _sha256(raw["approval.json"]),
        "attempt": _sha256(raw["attempt.json"]),
        "claim_terminal": _sha256(raw["claim-terminal.json"]),
        "capture_lineage": _sha256(raw_capture["lineage.json"]),
        "capture_manifest": _sha256(raw_capture["manifest.json"]),
        "capture_terminal_receipt": _sha256(raw_capture["terminal-receipt.json"]),
    }
    claimed_sha = _require_exact_mapping(
        authority.get("artifact_sha256"), _ARTIFACT_SHA_KEYS, "authority.artifact_sha256"
    )
    for name, actual in actual_sha.items():
        claimed = _require_sha256(
            claimed_sha.get(name), f"authority.artifact_sha256.{name}"
        )
        if not secrets.compare_digest(claimed, actual):
            _fail(f"authority artifact SHA is stale or mismatched: {name}")

    lineage = artifacts["capture_lineage"]
    manifest = artifacts["capture_manifest"]
    terminal_receipt = artifacts["capture_terminal_receipt"]
    actual_claims = {
        "plan_content_hash": artifacts["plan"]["plan_content_hash"],
        "arming_receipt_hash": artifacts["arming"]["arming_receipt_hash"],
        "proposal_hash": artifacts["proposal"]["proposal_hash"],
        "lifecycle_hash": artifacts["lifecycle"]["lifecycle_hash"],
        "approval_hash": artifacts["approval"]["approval_hash"],
        "attempt_hash": artifacts["attempt"]["attempt_hash"],
        "claim_terminal_hash": artifacts["claim_terminal"]["claim_terminal_hash"],
        "capture_lineage_hash": lineage.get("lineage_hash"),
        "capture_manifest_hash": manifest.get("manifest_hash"),
        "capture_terminal_receipt_hash": terminal_receipt.get("receipt_hash"),
    }
    claimed_hashes = _require_exact_mapping(
        authority.get("artifact_claim_hashes"),
        _ARTIFACT_CLAIM_KEYS,
        "authority.artifact_claim_hashes",
    )
    for name, actual in actual_claims.items():
        actual_hash = _require_sha256(actual, f"actual artifact claim {name}")
        claimed = _require_sha256(
            claimed_hashes.get(name), f"authority.artifact_claim_hashes.{name}"
        )
        if not secrets.compare_digest(claimed, actual_hash):
            _fail(f"authority artifact claim is stale or mismatched: {name}")
    return raw_capture


def _parse_capture_anchor(raw: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureAuthorityError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload) + b"\n":
        _fail(f"{label} is not canonical JSON")
    return dict(payload)


def _require_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        _fail(f"cross-binding mismatch: {label}")


def _validate_cross_bindings(
    *,
    expected_plan_file_sha256: str,
    raw: Mapping[str, bytes],
    raw_capture: Mapping[str, bytes],
    plan: Mapping[str, Any],
    arming: Mapping[str, Any],
    proposal: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
    approval: Mapping[str, Any],
    attempt: Mapping[str, Any],
    claim_terminal: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    actual_plan_file_sha = _sha256(raw["plan.json"])
    if not secrets.compare_digest(actual_plan_file_sha, expected_plan_file_sha256):
        _fail("external plan-file SHA trust anchor mismatch")
    _require_equal(authority.get("plan_file_sha256"), actual_plan_file_sha, "authority plan file")
    _require_equal(arming.get("plan_file_sha256"), actual_plan_file_sha, "arming plan file")

    lineage = _parse_capture_anchor(raw_capture["lineage.json"], "capture lineage")
    manifest = _parse_capture_anchor(raw_capture["manifest.json"], "capture manifest")
    terminal_receipt = _parse_capture_anchor(
        raw_capture["terminal-receipt.json"], "capture terminal receipt"
    )
    for field_name in ("plan_id", "plan_hash", "event_id", "event_lineage_hash"):
        reference = plan[field_name]
        for label, artifact in (
            ("arming", arming),
            ("proposal", proposal),
            ("lifecycle", lifecycle),
            ("approval", approval),
            ("attempt", attempt),
            ("claim-terminal", claim_terminal),
            ("authority", authority),
        ):
            _require_equal(artifact.get(field_name), reference, f"{label}.{field_name}")

    _require_equal(proposal.get("arming_receipt_hash"), arming["arming_receipt_hash"], "proposal->arming")
    _require_equal(lifecycle.get("proposal_hash"), proposal["proposal_hash"], "lifecycle->proposal")
    _require_equal(approval.get("proposal_hash"), proposal["proposal_hash"], "approval->proposal")
    _require_equal(approval.get("lifecycle_hash"), lifecycle["lifecycle_hash"], "approval->lifecycle")
    _require_equal(attempt.get("proposal_hash"), proposal["proposal_hash"], "attempt->proposal")
    _require_equal(attempt.get("lifecycle_hash"), lifecycle["lifecycle_hash"], "attempt->lifecycle")
    _require_equal(attempt.get("approval_hash"), approval["approval_hash"], "attempt->approval")
    _require_equal(claim_terminal.get("attempt_hash"), attempt["attempt_hash"], "claim-terminal->attempt")

    if not isinstance(lineage.get("plan"), dict) or not isinstance(lineage.get("event"), dict):
        _fail("capture lineage plan/event anchor is missing")
    capture_plan = lineage["plan"]
    event = lineage["event"]
    capture_arming = lineage.get("arming")
    capture_lifecycle = lineage.get("lifecycle")
    capture_claim = lineage.get("claim_release")
    if not all(isinstance(value, dict) for value in (capture_arming, capture_lifecycle, capture_claim)):
        _fail("capture lineage arming/lifecycle/claim anchor is missing")
    _require_equal(capture_plan.get("plan_id"), plan["plan_id"], "capture plan id")
    _require_equal(capture_plan.get("plan_hash"), plan["plan_hash"], "capture plan hash")
    _require_equal(event.get("event_id"), plan["event_id"], "capture event id")
    _require_equal(event.get("lineage_hash"), plan["event_lineage_hash"], "capture event lineage")
    _require_equal(event.get("venue"), plan["venue"], "capture venue")
    _require_equal(event.get("contract_id"), plan["contract_id"], "capture contract")
    _require_equal(event.get("official_spot_t0"), plan["official_spot_t0"], "capture t0")
    _require_equal(event.get("t0_source_class"), plan["t0_source_class"], "capture t0 class")
    _require_equal(event.get("t0_precision_sec"), plan["t0_precision_sec"], "capture t0 precision")
    _require_equal(event.get("official_record_hash"), plan["official_record_hash"], "official registry record")
    _require_equal(event.get("official_source_url"), plan["official_source_url"], "official source URL")
    _require_equal(authority.get("official_record_hash"), plan["official_record_hash"], "authority official record")

    _require_equal(arming.get("official_record_hash"), plan["official_record_hash"], "arming official record")
    _require_equal(arming.get("official_spot_t0"), plan["official_spot_t0"], "arming t0")
    _require_equal(arming.get("t0_source_class"), plan["t0_source_class"], "arming t0 class")
    _require_equal(arming.get("t0_precision_sec"), plan["t0_precision_sec"], "arming t0 precision")
    _require_equal(
        arming.get("capture_arming_receipt_hash"),
        capture_arming.get("arming_receipt_hash"),
        "capture arming receipt",
    )
    _require_equal(
        arming.get("capture_arming_lineage_hash"),
        capture_arming.get("lineage_hash"),
        "capture arming lineage",
    )
    _require_equal(lifecycle.get("contract_id"), plan["contract_id"], "lifecycle contract")
    _require_equal(lifecycle.get("official_spot_t0"), plan["official_spot_t0"], "lifecycle t0")
    for field_name in ("phase_at_entry", "terminal_phase", "transition_ts", "lifecycle_record_hash"):
        _require_equal(
            lifecycle.get(field_name), capture_lifecycle.get(field_name), f"capture lifecycle {field_name}"
        )
    _require_equal(
        lifecycle.get("capture_lifecycle_lineage_hash"),
        capture_lifecycle.get("lineage_hash"),
        "capture lifecycle lineage",
    )

    _require_equal(attempt.get("claim_id"), capture_claim.get("claim_id"), "attempt claim id")
    _require_equal(
        attempt.get("claim_record_hash"), capture_claim.get("claim_record_hash"), "attempt claim record"
    )
    _require_equal(claim_terminal.get("capture_id"), lineage.get("capture_id"), "claim capture id")
    for field_name in (
        "claim_id",
        "claim_record_hash",
        "capture_terminal_record_hash",
        "release_record_hash",
    ):
        _require_equal(
            claim_terminal.get(field_name), capture_claim.get(field_name), f"claim release {field_name}"
        )
    _require_equal(capture_claim.get("status"), "RELEASED", "capture claim status")
    _require_equal(
        capture_claim.get("released_after_capture_terminal_record"),
        True,
        "capture terminal-before-release",
    )

    manifest_raw_sha = _sha256(raw_capture["manifest.json"])
    terminal_raw_sha = _sha256(raw_capture["terminal-receipt.json"])
    _require_equal(claim_terminal.get("manifest_sha256"), manifest_raw_sha, "claim manifest SHA")
    _require_equal(claim_terminal.get("manifest_hash"), manifest.get("manifest_hash"), "claim manifest hash")
    _require_equal(
        claim_terminal.get("terminal_receipt_sha256"), terminal_raw_sha, "claim terminal receipt SHA"
    )
    _require_equal(
        claim_terminal.get("terminal_receipt_hash"),
        terminal_receipt.get("receipt_hash"),
        "claim terminal receipt hash",
    )
    _require_equal(manifest.get("capture_id"), lineage.get("capture_id"), "manifest capture id")
    _require_equal(terminal_receipt.get("capture_id"), lineage.get("capture_id"), "receipt capture id")
    _require_equal(authority.get("capture_id"), lineage.get("capture_id"), "authority capture id")
    _require_equal(authority.get("venue"), plan["venue"], "authority venue")
    _require_equal(authority.get("contract_id"), plan["contract_id"], "authority contract")
    _require_equal(authority.get("official_spot_t0"), plan["official_spot_t0"], "authority t0")
    return lineage, manifest, terminal_receipt


def _receipt_verification_hash(receipt: Mapping[str, Any]) -> str:
    material = copy.deepcopy(dict(receipt))
    material.pop("verification_hash", None)
    return _sha256(canonical_json_bytes(material))


def _build_receipt(
    *,
    plan: Mapping[str, Any],
    arming: Mapping[str, Any],
    proposal: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
    approval: Mapping[str, Any],
    attempt: Mapping[str, Any],
    claim_terminal: Mapping[str, Any],
    authority: Mapping[str, Any],
    lineage: Mapping[str, Any],
    raw_capture: Mapping[str, bytes],
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "status": RECEIPT_STATUS,
        "fixture_only": True,
        "fixture_external_chain_verified": True,
        "production_external_authority_verified": False,
        "capture_authorized": False,
        "capture_token_issued": False,
        "network_allowed": False,
        "orders_allowed": False,
        "acceptance_capable": False,
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "plan_file_sha256": authority["plan_file_sha256"],
        "event_id": plan["event_id"],
        "capture_id": lineage["capture_id"],
        "hashes": {
            "authority": authority["authority_hash"],
            "arming": arming["arming_receipt_hash"],
            "proposal": proposal["proposal_hash"],
            "lifecycle": lifecycle["lifecycle_hash"],
            "approval": approval["approval_hash"],
            "attempt": attempt["attempt_hash"],
            "claim_terminal": claim_terminal["claim_terminal_hash"],
            "manifest": _sha256(raw_capture["manifest.json"]),
            "terminal_receipt": _sha256(raw_capture["terminal-receipt.json"]),
            "lineage": _sha256(raw_capture["lineage.json"]),
        },
    }
    if frozenset(receipt["hashes"]) != _RECEIPT_HASH_KEYS:
        _fail("internal receipt hash map is incomplete")
    receipt["verification_hash"] = _receipt_verification_hash(receipt)
    return receipt


def verify_fixture_authority_bundle(
    bundle_root: Path,
    *,
    expected_plan_file_sha256: str,
) -> VerifiedFixtureHandoff:
    """Verify an external canonical fixture and issue one process-local handoff."""

    expected_plan_sha = _require_sha256(
        expected_plan_file_sha256, "expected_plan_file_sha256"
    )
    root, raw, parsed, capture = _read_layout(Path(bundle_root))
    (
        plan,
        arming,
        proposal,
        lifecycle,
        approval,
        attempt,
        claim_terminal,
        authority,
    ) = _validate_policy_artifacts(parsed)

    # Parse the three independent capture anchors before checking the authority's
    # raw and canonical hash maps.  The full L2 reader below repeats all of its own
    # exact-file, coverage, cost and temporal validation.
    capture_anchors = {
        "capture_lineage": _parse_capture_anchor(
            (capture / "lineage.json").read_bytes(), "capture lineage"
        ),
        "capture_manifest": _parse_capture_anchor(
            (capture / "manifest.json").read_bytes(), "capture manifest"
        ),
        "capture_terminal_receipt": _parse_capture_anchor(
            (capture / "terminal-receipt.json").read_bytes(),
            "capture terminal receipt",
        ),
    }
    artifacts: dict[str, Mapping[str, Any]] = {
        "plan": plan,
        "arming": arming,
        "proposal": proposal,
        "lifecycle": lifecycle,
        "approval": approval,
        "attempt": attempt,
        "claim_terminal": claim_terminal,
        **capture_anchors,
    }
    raw_capture = _validate_hash_maps(
        root=root,
        raw=raw,
        capture=capture,
        artifacts=artifacts,
        authority=authority,
    )
    lineage, _manifest, _terminal_receipt = _validate_cross_bindings(
        expected_plan_file_sha256=expected_plan_sha,
        raw=raw,
        raw_capture=raw_capture,
        plan=plan,
        arming=arming,
        proposal=proposal,
        lifecycle=lifecycle,
        approval=approval,
        attempt=attempt,
        claim_terminal=claim_terminal,
        authority=authority,
    )
    try:
        request = l2_evidence.inspect_candidate_execution_request(capture)
    except l2_evidence.L2EvidenceError as exc:
        raise FixtureAuthorityError(f"sealed L2 candidate failed: {exc}") from exc
    if (
        request.get("event", {}).get("event_id") != plan["event_id"]
        or request.get("capture_manifest_sha256")
        != _sha256(raw_capture["manifest.json"])
        or request.get("orders_created") != 0
        or request.get("private_api_used") is not False
        or request.get("live_execution") is not False
    ):
        _fail("sealed replay request crossed the fixture authority boundary")

    receipt = _build_receipt(
        plan=plan,
        arming=arming,
        proposal=proposal,
        lifecycle=lifecycle,
        approval=approval,
        attempt=attempt,
        claim_terminal=claim_terminal,
        authority=authority,
        lineage=lineage,
        raw_capture=raw_capture,
    )
    verification_hash = receipt["verification_hash"]
    while True:
        capability_id = secrets.token_hex(32)
        with _CAPABILITY_LOCK:
            if capability_id not in _CAPABILITIES:
                _CAPABILITIES[capability_id] = _CapabilityRecord(
                    request=copy.deepcopy(request),
                    receipt=copy.deepcopy(receipt),
                )
                break
    return VerifiedFixtureHandoff(
        schema=HANDOFF_SCHEMA,
        status=HANDOFF_STATUS,
        capability_id=capability_id,
        verification_hash=verification_hash,
    )


def _verify_record_receipt_hash(
    record: _CapabilityRecord,
    expected_verification_hash: str,
    label: str,
) -> None:
    receipt_hash = _receipt_verification_hash(record.receipt)
    if (
        not secrets.compare_digest(receipt_hash, expected_verification_hash)
        or not secrets.compare_digest(
            str(record.receipt.get("verification_hash", "")),
            expected_verification_hash,
        )
    ):
        _fail(f"{label} verification hash mismatch")


def consume_verified_replay_record(
    opaque: VerifiedReplayRecord,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Consume the callback-only record and reveal copies to that callback once."""

    if type(opaque) is not VerifiedReplayRecord:  # exact type is intentional
        _fail("verified replay record type is invalid")
    if (
        opaque.schema != REPLAY_RECORD_SCHEMA
        or opaque.status != REPLAY_RECORD_STATUS
        or not _is_sha256(opaque.record_id)
        or not _is_sha256(opaque.verification_hash)
    ):
        _fail("verified replay record identity is invalid")
    with _CAPABILITY_LOCK:
        record = _REPLAY_RECORDS.pop(opaque.record_id, None)
    if record is None:
        _fail("verified replay record is forged, stale, or already consumed")
    _verify_record_receipt_hash(
        record,
        opaque.verification_hash,
        "verified replay record",
    )
    return copy.deepcopy(record.request), copy.deepcopy(record.receipt)


def consume_fixture_handoff(handoff: VerifiedFixtureHandoff) -> dict[str, Any]:
    """Move a handoff into a second one-shot store, then invoke replay opaquely."""

    if type(handoff) is not VerifiedFixtureHandoff:  # exact type is intentional
        _fail("fixture handoff type is invalid")
    if (
        handoff.schema != HANDOFF_SCHEMA
        or handoff.status != HANDOFF_STATUS
        or not _is_sha256(handoff.capability_id)
        or not _is_sha256(handoff.verification_hash)
    ):
        _fail("fixture handoff identity is invalid")

    # Pop before any callback.  A missing callback or failed calculation consumes
    # the first capability and can never become an implicit retry.
    with _CAPABILITY_LOCK:
        record = _CAPABILITIES.pop(handoff.capability_id, None)
    if record is None:
        _fail("fixture handoff is forged, stale, or already consumed")
    _verify_record_receipt_hash(record, handoff.verification_hash, "fixture handoff")

    while True:
        record_id = secrets.token_hex(32)
        with _CAPABILITY_LOCK:
            if record_id not in _REPLAY_RECORDS:
                _REPLAY_RECORDS[record_id] = record
                break
    opaque = VerifiedReplayRecord(
        schema=REPLAY_RECORD_SCHEMA,
        status=REPLAY_RECORD_STATUS,
        record_id=record_id,
        verification_hash=handoff.verification_hash,
    )
    try:
        try:
            replay_module = importlib.import_module("v43_verified_replay")
            callback = getattr(replay_module, "_execute_verified_fixture_record")
        except (ImportError, AttributeError) as exc:
            raise FixtureAuthorityError("fixture replay callback is unavailable") from exc
        if not callable(callback):
            _fail("fixture replay callback is not callable")
        result = callback(opaque)
        if not isinstance(result, dict):
            _fail("fixture replay callback did not return an object")
        return copy.deepcopy(result)
    finally:
        # If a replaced or failed callback retained the opaque value without
        # consuming it, destroy the second capability before control returns.
        with _CAPABILITY_LOCK:
            _REPLAY_RECORDS.pop(record_id, None)
