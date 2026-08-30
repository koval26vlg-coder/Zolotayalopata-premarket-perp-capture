"""Fixture-only bridge from a process-issued v43 handoff to pure replay.

The public entry point never accepts a replay request.  The separate fixture
authority owns the sealed request and consumes its opaque capability exactly once,
then calls the private runner below in the same process.  The temporary synthetic
label is only an adapter for the existing v42 pure calculation engine; every returned
result is relabelled as non-production fixture evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

import execution_replay


FIXTURE_EVIDENCE_MODE = "FIXTURE_REHEARSAL_ONLY"
_REQUEST_SCHEMA = "premarket_perp_execution_replay_request_v1"
_ENVELOPE_SCHEMA = "premarket_perp_execution_evidence_envelope_v1"
_RECEIPT_SCHEMA = "premarket_perp_v43_fixture_external_authority_verification_v1"
_RECEIPT_STATUS = "FIXTURE_AUTHORITY_CHAIN_OK_NO_CAPTURE_AUTHORITY"
_HASH_FIELDS = frozenset(
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
_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "status",
        "fixture_only",
        "fixture_external_chain_verified",
        "production_external_authority_verified",
        "capture_authorized",
        "capture_token_issued",
        "network_allowed",
        "orders_allowed",
        "acceptance_capable",
        "plan_id",
        "plan_hash",
        "plan_file_sha256",
        "event_id",
        "capture_id",
        "hashes",
        "verification_hash",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class V43VerifiedReplayError(RuntimeError):
    """A fixture capability or its private replay payload failed closed."""


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _validate_authority_receipt(
    receipt: object,
    request: Mapping[str, Any],
) -> str:
    if not isinstance(receipt, Mapping) or frozenset(receipt) != _RECEIPT_FIELDS:
        raise V43VerifiedReplayError("FIXTURE_AUTHORITY_RECEIPT_FIELDS_INVALID")
    if (
        receipt.get("schema") != _RECEIPT_SCHEMA
        or receipt.get("status") != _RECEIPT_STATUS
        or receipt.get("fixture_only") is not True
        or receipt.get("fixture_external_chain_verified") is not True
        or receipt.get("production_external_authority_verified") is not False
        or receipt.get("capture_authorized") is not False
        or receipt.get("capture_token_issued") is not False
        or receipt.get("network_allowed") is not False
        or receipt.get("orders_allowed") is not False
        or receipt.get("acceptance_capable") is not False
    ):
        raise V43VerifiedReplayError("FIXTURE_AUTHORITY_RECEIPT_POLICY_INVALID")
    for field in ("plan_id", "event_id", "capture_id"):
        if not isinstance(receipt.get(field), str) or not str(receipt[field]):
            raise V43VerifiedReplayError(
                f"FIXTURE_AUTHORITY_RECEIPT_{field.upper()}_INVALID"
            )
    for field in ("plan_hash", "plan_file_sha256", "verification_hash"):
        if not _is_sha256(receipt.get(field)):
            raise V43VerifiedReplayError(
                f"FIXTURE_AUTHORITY_RECEIPT_{field.upper()}_INVALID"
            )
    hashes = receipt.get("hashes")
    if (
        not isinstance(hashes, Mapping)
        or frozenset(hashes) != _HASH_FIELDS
        or any(not _is_sha256(value) for value in hashes.values())
    ):
        raise V43VerifiedReplayError("FIXTURE_AUTHORITY_RECEIPT_HASHES_INVALID")
    material = copy.deepcopy(dict(receipt))
    claimed = material.pop("verification_hash")
    if claimed != _canonical_sha256(material):
        raise V43VerifiedReplayError("FIXTURE_AUTHORITY_RECEIPT_HASH_MISMATCH")

    event = request.get("event")
    loader = request.get("trusted_loader_verification")
    if not isinstance(event, Mapping) or receipt["event_id"] != event.get("event_id"):
        raise V43VerifiedReplayError("FIXTURE_AUTHORITY_EVENT_ID_MISMATCH")
    if (
        not isinstance(loader, Mapping)
        or receipt["capture_id"] != loader.get("capture_id")
    ):
        raise V43VerifiedReplayError("FIXTURE_AUTHORITY_CAPTURE_ID_MISMATCH")
    if hashes["manifest"] != request.get("capture_manifest_sha256"):
        raise V43VerifiedReplayError("FIXTURE_AUTHORITY_MANIFEST_MISMATCH")
    return str(claimed)


def _validate_sealed_request(request: object) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise V43VerifiedReplayError("SEALED_FIXTURE_REQUEST_INVALID")
    sealed = copy.deepcopy(dict(request))
    envelope = sealed.get("evidence_envelope")
    loader = sealed.get("trusted_loader_verification")
    manifest_hash = sealed.get("capture_manifest_sha256")
    if (
        sealed.get("schema") != _REQUEST_SCHEMA
        or sealed.get("sealed") is not True
        or sealed.get("evidence_class") != "SEALED_L2_CAPTURE"
        or not _is_sha256(manifest_hash)
        or not isinstance(envelope, Mapping)
        or envelope.get("schema") != _ENVELOPE_SCHEMA
        or envelope.get("sealed") is not True
        or envelope.get("evidence_class") != "SEALED_L2_CAPTURE"
        or envelope.get("capture_manifest_sha256") != manifest_hash
    ):
        raise V43VerifiedReplayError("SEALED_FIXTURE_REQUEST_BOUNDARY_INVALID")
    payload = copy.deepcopy(sealed)
    payload.pop("evidence_envelope", None)
    if envelope.get("payload_sha256") != execution_replay.canonical_result_hash(payload):
        raise V43VerifiedReplayError("SEALED_FIXTURE_REQUEST_HASH_MISMATCH")
    if (
        not isinstance(loader, Mapping)
        or loader.get("exact_readback") is not True
        or loader.get("gap_free") is not True
        or loader.get("acceptance_capable") is not False
        or loader.get("external_authority_verified") is not False
        or loader.get("trusted_replay_handoff") is not False
    ):
        raise V43VerifiedReplayError("SEALED_FIXTURE_LOADER_CHAIN_INVALID")
    return sealed


def _synthetic_calculation_input(sealed: Mapping[str, Any]) -> dict[str, Any]:
    calculation = copy.deepcopy(dict(sealed))
    for field in (
        "evidence_envelope",
        "sealed",
        "evidence_class",
        "capture_manifest_sha256",
        "trusted_loader_verification",
        "orders_created",
        "private_api_used",
        "live_execution",
    ):
        calculation.pop(field, None)
    event = calculation.get("event")
    if not isinstance(event, dict):
        raise V43VerifiedReplayError("SEALED_FIXTURE_EVENT_INVALID")
    event["evidence_class"] = "SYNTHETIC_OFFLINE_ONLY"
    return calculation


def _authority_runtime() -> Any:
    try:
        import v43_fixture_authority as authority
    except ImportError as exc:
        raise V43VerifiedReplayError("FIXTURE_AUTHORITY_RUNTIME_UNAVAILABLE") from exc
    return authority


def _execute_verified_fixture_record(replay_record: object) -> dict[str, Any]:
    """Consume one authority-owned opaque record before touching replay evidence."""

    authority = _authority_runtime()
    try:
        sealed_request, authority_receipt = authority.consume_verified_replay_record(
            replay_record
        )
    except authority.FixtureAuthorityError as exc:
        raise V43VerifiedReplayError(
            str(exc) or "VERIFIED_REPLAY_RECORD_REJECTED"
        ) from exc

    sealed = _validate_sealed_request(sealed_request)
    verification_hash = _validate_authority_receipt(authority_receipt, sealed)
    calculation = _synthetic_calculation_input(sealed)
    result = execution_replay.replay_fixed_long(calculation)
    if not isinstance(result, dict):
        raise V43VerifiedReplayError("FIXTURE_REPLAY_RESULT_INVALID")

    result = copy.deepcopy(result)
    result.pop("result_hash", None)
    if isinstance(sealed.get("event"), Mapping) and "event" in result:
        result["event"] = copy.deepcopy(dict(sealed["event"]))
    result.update(
        {
            "evidence_mode": FIXTURE_EVIDENCE_MODE,
            "source_evidence_class": "SEALED_L2_CAPTURE",
            "fixture_external_authority_verified": True,
            "fixture_authority_verification_hash": verification_hash,
            "production_external_authority_verified": False,
            "acceptance_capable": False,
            "capture_authorized": False,
            "capture_token_issued": False,
            "network_allowed": False,
            "orders_allowed": False,
            "orders_created": 0,
            "private_api_used": False,
            "live_execution": False,
        }
    )
    result["result_hash"] = execution_replay.canonical_result_hash(result)
    return result


def replay_verified_fixture(handoff: object) -> dict[str, Any]:
    """Consume one opaque process-issued fixture capability and replay it."""

    authority = _authority_runtime()
    try:
        result = authority.consume_fixture_handoff(handoff)
    except authority.FixtureAuthorityError as exc:
        raise V43VerifiedReplayError(str(exc) or "FIXTURE_HANDOFF_REJECTED") from exc
    if not isinstance(result, dict):
        raise V43VerifiedReplayError("FIXTURE_AUTHORITY_RESULT_INVALID")
    if (
        result.get("evidence_mode") != FIXTURE_EVIDENCE_MODE
        or result.get("acceptance_capable") is not False
        or result.get("production_external_authority_verified") is not False
        or result.get("capture_authorized") is not False
        or result.get("capture_token_issued") is not False
        or result.get("network_allowed") is not False
        or result.get("orders_allowed") is not False
        or result.get("orders_created") != 0
        or result.get("private_api_used") is not False
        or result.get("live_execution") is not False
    ):
        raise V43VerifiedReplayError("FIXTURE_AUTHORITY_RESULT_POLICY_INVALID")
    material = copy.deepcopy(result)
    claimed = material.pop("result_hash", None)
    if claimed != execution_replay.canonical_result_hash(material):
        raise V43VerifiedReplayError("FIXTURE_AUTHORITY_RESULT_HASH_MISMATCH")
    return copy.deepcopy(result)
