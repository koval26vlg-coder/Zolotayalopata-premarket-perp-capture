"""Fail-closed loader for a future event-bound v43 L2 capture bundle.

This module is intentionally additive and has no capture, network, scheduling, or
exchange-execution capability.  It reads one fixed directory layout, verifies the
sealed public-market evidence and constructs the already-preregistered offline
``execution_replay`` request.  Binding it into an active PlanOnly remains a separate
event-specific activation step.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

from canonical_hash import canonical_json_bytes
from execution_replay import EXPECTED_MODEL, canonical_result_hash


EVIDENCE_FILE_NAMES = (
    "raw-frames.jsonl",
    "normalized-depth.json",
    "contract-cost.json",
    "funding.json",
    "mark-index.json",
    "lineage.json",
)
MANIFEST_FILE_NAME = "manifest.json"
RECEIPT_FILE_NAME = "terminal-receipt.json"
CAPTURE_FILE_NAMES = frozenset(
    (*EVIDENCE_FILE_NAMES, MANIFEST_FILE_NAME, RECEIPT_FILE_NAME)
)
REQUIRED_OFFSETS = (-60, 0, 5, 15, 60)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


class L2EvidenceError(RuntimeError):
    """The supplied directory cannot prove a complete trusted replay input."""


def _fail(message: str) -> None:
    raise L2EvidenceError(message)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _require_sha256(value: object, label: str) -> str:
    if not _is_sha256(value):
        _fail(f"{label} is not a lowercase SHA-256")
    return str(value)


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or _CANONICAL_ID_RE.fullmatch(value) is None:
        _fail(f"{label} is missing or non-canonical")
    return value


def _finite(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        _fail(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        _fail(f"{label} must be a finite number")
    if not math.isfinite(number) or (positive and number <= 0):
        _fail(f"{label} must be a finite{' positive' if positive else ''} number")
    return number


def _require_int(value: object, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{label} must be an integer")
    if positive and value <= 0:
        _fail(f"{label} must be positive")
    return value


def _require_exact_keys(
    value: object,
    expected: set[str] | frozenset[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    actual = frozenset(value)
    expected = frozenset(expected)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        _fail(f"{label} fields mismatch: missing={missing}, extra={extra}")
    return dict(value)


def _canonical_claim(payload: Mapping[str, Any], field: str) -> str:
    material = copy.deepcopy(dict(payload))
    material.pop(field, None)
    return _sha256(canonical_json_bytes(material))


def _verify_claim(payload: Mapping[str, Any], field: str, label: str) -> str:
    claimed = _require_sha256(payload.get(field), f"{label}.{field}")
    if claimed != _canonical_claim(payload, field):
        _fail(f"{label}.{field} does not match canonical content")
    return claimed


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    try:
        return path.is_symlink() or bool(is_junction and is_junction(path))
    except OSError as exc:
        raise L2EvidenceError(f"cannot inspect path identity: {path.name}") from exc


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


def _read_fixed_directory(capture_dir: Path) -> tuple[Path, dict[str, bytes]]:
    supplied = Path(capture_dir)
    if _is_link_or_junction(supplied):
        _fail("capture directory must not be a symlink or junction")
    try:
        root = supplied.resolve(strict=True)
    except OSError as exc:
        raise L2EvidenceError("capture directory does not exist") from exc
    if not root.is_dir() or _is_link_or_junction(root):
        _fail("capture path is not a plain directory")
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise L2EvidenceError("capture directory cannot be enumerated") from exc
    names = frozenset(path.name for path in entries)
    if names != CAPTURE_FILE_NAMES:
        _fail(
            "capture directory layout mismatch: "
            f"missing={sorted(CAPTURE_FILE_NAMES - names)}, "
            f"extra={sorted(names - CAPTURE_FILE_NAMES)}"
        )

    raw_by_name: dict[str, bytes] = {}
    for name in sorted(CAPTURE_FILE_NAMES):
        path = root / name
        if _is_link_or_junction(path):
            _fail(f"bound file is a symlink or junction: {name}")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise L2EvidenceError(f"bound file is missing: {name}") from exc
        if resolved.parent != root or resolved.name != name:
            _fail(f"bound file escapes capture directory: {name}")
        try:
            before = path.stat()
            if not path.is_file():
                _fail(f"bound path is not a regular file: {name}")
            raw = path.read_bytes()
            after = path.stat()
        except OSError as exc:
            raise L2EvidenceError(f"bound file cannot be read: {name}") from exc
        if not _same_stat(before, after):
            _fail(f"bound file changed during exact readback: {name}")
        try:
            if path.resolve(strict=True) != resolved:
                _fail(f"bound file identity changed during exact readback: {name}")
        except OSError as exc:
            raise L2EvidenceError(
                f"bound file identity cannot be confirmed: {name}"
            ) from exc
        raw_by_name[name] = raw
    return root, raw_by_name


def _parse_canonical_json(raw: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise L2EvidenceError(f"{name} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        _fail(f"{name} must contain one JSON object")
    if canonical_json_bytes(value) + b"\n" != raw:
        _fail(f"{name} failed canonical exact readback")
    return dict(value)


def _parse_raw_tape(raw: bytes) -> list[dict[str, Any]]:
    if not raw or not raw.endswith(b"\n"):
        _fail("raw-frames.jsonl must be non-empty and newline terminated")
    lines = raw.splitlines(keepends=True)
    frames: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if line in (b"", b"\n", b"\r\n"):
            _fail(f"raw frame line {number} is empty")
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise L2EvidenceError(f"raw frame line {number} is invalid") from exc
        if not isinstance(value, dict) or canonical_json_bytes(value) + b"\n" != line:
            _fail(f"raw frame line {number} failed canonical exact readback")
        frames.append(dict(value))
    return frames


def _validate_lineage(lineage: dict[str, Any]) -> dict[str, Any]:
    lineage = _require_exact_keys(
        lineage,
        {
            "schema",
            "capture_id",
            "plan",
            "event",
            "arming",
            "lifecycle",
            "claim_release",
            "lineage_hash",
        },
        "lineage",
    )
    if lineage["schema"] != "premarket_perp_l2_lineage_bundle_v43":
        _fail("lineage schema mismatch")
    capture_id = _require_text(lineage["capture_id"], "lineage.capture_id")
    bundle_hash = _verify_claim(lineage, "lineage_hash", "lineage")

    plan = _require_exact_keys(
        lineage["plan"],
        {
            "schema",
            "plan_id",
            "plan_hash",
            "capture_runtime_sha256",
            "loader_runtime_sha256",
            "lineage_hash",
        },
        "lineage.plan",
    )
    if plan["schema"] != "premarket_perp_l2_plan_lineage_v43":
        _fail("plan lineage schema mismatch")
    plan_id = _require_text(plan["plan_id"], "lineage.plan.plan_id")
    if "v43" not in plan_id.lower():
        _fail("plan lineage is not event-bound v43")
    plan_hash = _require_sha256(plan["plan_hash"], "lineage.plan.plan_hash")
    _require_sha256(
        plan["capture_runtime_sha256"], "lineage.plan.capture_runtime_sha256"
    )
    _require_sha256(
        plan["loader_runtime_sha256"], "lineage.plan.loader_runtime_sha256"
    )
    plan_lineage_hash = _verify_claim(plan, "lineage_hash", "lineage.plan")

    event = _require_exact_keys(
        lineage["event"],
        {
            "schema",
            "event_id",
            "venue",
            "contract_id",
            "official_spot_t0",
            "t0_source_class",
            "t0_precision_sec",
            "official_record_hash",
            "official_source_url",
            "plan_hash",
            "lineage_hash",
        },
        "lineage.event",
    )
    if event["schema"] != "premarket_perp_l2_event_lineage_v43":
        _fail("event lineage schema mismatch")
    event_id = _require_text(event["event_id"], "lineage.event.event_id")
    venue = _require_text(event["venue"], "lineage.event.venue")
    if venue not in {"bybit", "okx", "gate"}:
        _fail("event venue is outside the preregistered set")
    contract_id = _require_text(event["contract_id"], "lineage.event.contract_id")
    official_t0 = _require_int(
        event["official_spot_t0"], "lineage.event.official_spot_t0", positive=True
    )
    if event["t0_source_class"] != "OFFICIAL_ANNOUNCEMENT":
        _fail("event t0 is not official announcement evidence")
    if event["t0_precision_sec"] != 1:
        _fail("event t0 is not seconds-grade")
    _require_sha256(
        event["official_record_hash"], "lineage.event.official_record_hash"
    )
    source_url = event["official_source_url"]
    if not isinstance(source_url, str):
        _fail("event official source URL is missing")
    parsed = urllib.parse.urlsplit(source_url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        _fail("event official source URL is not canonical HTTPS")
    if event["plan_hash"] != plan_hash:
        _fail("event and plan lineage hash identity mismatch")
    event_lineage_hash = _verify_claim(event, "lineage_hash", "lineage.event")

    arming = _require_exact_keys(
        lineage["arming"],
        {
            "schema",
            "status",
            "arming_receipt_hash",
            "event_lineage_hash",
            "plan_hash",
            "official_spot_t0",
            "lineage_hash",
        },
        "lineage.arming",
    )
    if (
        arming["schema"] != "premarket_perp_l2_arming_lineage_v43"
        or arming["status"] != "ARMED"
    ):
        _fail("arming lineage is not ARMED v43 evidence")
    _require_sha256(
        arming["arming_receipt_hash"], "lineage.arming.arming_receipt_hash"
    )
    if (
        arming["event_lineage_hash"] != event_lineage_hash
        or arming["plan_hash"] != plan_hash
        or arming["official_spot_t0"] != official_t0
    ):
        _fail("arming lineage does not bind the event and plan")
    arming_lineage_hash = _verify_claim(arming, "lineage_hash", "lineage.arming")

    lifecycle = _require_exact_keys(
        lineage["lifecycle"],
        {
            "schema",
            "lifecycle_record_hash",
            "event_lineage_hash",
            "contract_id",
            "phase_at_entry",
            "terminal_phase",
            "transition_ts",
            "lineage_hash",
        },
        "lineage.lifecycle",
    )
    if lifecycle["schema"] != "premarket_perp_l2_lifecycle_lineage_v43":
        _fail("lifecycle lineage schema mismatch")
    _require_sha256(
        lifecycle["lifecycle_record_hash"],
        "lineage.lifecycle.lifecycle_record_hash",
    )
    if (
        lifecycle["event_lineage_hash"] != event_lineage_hash
        or lifecycle["contract_id"] != contract_id
    ):
        _fail("lifecycle lineage does not bind the event contract")
    if lifecycle["phase_at_entry"] not in {"CALL_AUCTION", "CONTINUOUS"}:
        _fail("lifecycle phase at entry is not tradable")
    if lifecycle["terminal_phase"] not in {
        "TRANSITIONED",
        "CANCELLED",
        "DELISTED",
        "EXPIRED",
    }:
        _fail("lifecycle terminal phase is invalid")
    transition_ts = lifecycle["transition_ts"]
    if transition_ts is not None:
        _finite(transition_ts, "lineage.lifecycle.transition_ts")
    lifecycle_lineage_hash = _verify_claim(
        lifecycle, "lineage_hash", "lineage.lifecycle"
    )

    claim_release = _require_exact_keys(
        lineage["claim_release"],
        {
            "schema",
            "status",
            "claim_id",
            "claim_record_hash",
            "capture_terminal_record_hash",
            "release_record_hash",
            "released_after_capture_terminal_record",
            "capture_id",
            "event_lineage_hash",
            "plan_hash",
            "lineage_hash",
        },
        "lineage.claim_release",
    )
    if (
        claim_release["schema"]
        != "premarket_perp_l2_claim_release_lineage_v43"
        or claim_release["status"] != "RELEASED"
        or claim_release["released_after_capture_terminal_record"] is not True
    ):
        _fail("claim-release lineage is not terminal and released")
    _require_text(claim_release["claim_id"], "lineage.claim_release.claim_id")
    for field in (
        "claim_record_hash",
        "capture_terminal_record_hash",
        "release_record_hash",
    ):
        _require_sha256(
            claim_release[field], f"lineage.claim_release.{field}"
        )
    if (
        claim_release["capture_id"] != capture_id
        or claim_release["event_lineage_hash"] != event_lineage_hash
        or claim_release["plan_hash"] != plan_hash
    ):
        _fail("claim-release lineage does not bind capture, event and plan")
    claim_release_lineage_hash = _verify_claim(
        claim_release, "lineage_hash", "lineage.claim_release"
    )

    return {
        "capture_id": capture_id,
        "plan_id": plan_id,
        "plan_hash": plan_hash,
        "event_id": event_id,
        "venue": venue,
        "contract_id": contract_id,
        "official_spot_t0": official_t0,
        "event": event,
        "hashes": {
            "bundle": bundle_hash,
            "plan": plan_lineage_hash,
            "event": event_lineage_hash,
            "arming": arming_lineage_hash,
            "lifecycle": lifecycle_lineage_hash,
            "claim_release": claim_release_lineage_hash,
        },
    }


def _validate_manifest(
    manifest: dict[str, Any],
    raw_by_name: Mapping[str, bytes],
    lineage: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = _require_exact_keys(
        manifest,
        {
            "schema",
            "capture_id",
            "status",
            "evidence_class",
            "acceptance_capable",
            "file_sha256",
            "lineage_hashes",
            "coverage",
            "manifest_hash",
        },
        "manifest",
    )
    if manifest["schema"] != "premarket_perp_l2_capture_manifest_v43":
        _fail("manifest schema mismatch")
    if manifest["capture_id"] != lineage["capture_id"]:
        _fail("manifest capture_id does not match lineage")
    if (
        manifest["status"] != "COMPLETED"
        or manifest["evidence_class"] != "SEALED_L2_CAPTURE"
        or manifest["acceptance_capable"] is not False
    ):
        _fail("manifest is not completed non-acceptance L2 evidence")
    manifest_hash = _verify_claim(manifest, "manifest_hash", "manifest")

    file_hashes = _require_exact_keys(
        manifest["file_sha256"], set(EVIDENCE_FILE_NAMES), "manifest.file_sha256"
    )
    for name in EVIDENCE_FILE_NAMES:
        claimed = _require_sha256(
            file_hashes[name], f"manifest.file_sha256.{name}"
        )
        if claimed != _sha256(raw_by_name[name]):
            _fail(f"manifest file hash mismatch: {name}")

    lineage_hashes = _require_exact_keys(
        manifest["lineage_hashes"],
        {"bundle", "plan", "event", "arming", "lifecycle", "claim_release"},
        "manifest.lineage_hashes",
    )
    if lineage_hashes != lineage["hashes"]:
        _fail("manifest lineage hashes do not match verified lineage")
    return {
        "hash": manifest_hash,
        "coverage": manifest["coverage"],
        "file_hashes": file_hashes,
        "lineage_hashes": lineage_hashes,
    }


def _validate_receipt(
    receipt: dict[str, Any],
    manifest_raw: bytes,
    manifest_verified: Mapping[str, Any],
    lineage: Mapping[str, Any],
) -> str:
    receipt = _require_exact_keys(
        receipt,
        {
            "schema",
            "capture_id",
            "status",
            "manifest_sha256",
            "manifest_hash",
            "lineage_hashes",
            "claim_released",
            "orders_created",
            "private_api_used",
            "live_execution",
            "receipt_hash",
        },
        "terminal receipt",
    )
    if receipt["schema"] != "premarket_perp_l2_terminal_receipt_v43":
        _fail("terminal receipt schema mismatch")
    if receipt["capture_id"] != lineage["capture_id"]:
        _fail("terminal receipt capture_id mismatch")
    if receipt["status"] != "COMPLETED" or receipt["claim_released"] is not True:
        _fail("terminal receipt is not COMPLETED with a released claim")
    if (
        receipt["orders_created"] != 0
        or receipt["private_api_used"] is not False
        or receipt["live_execution"] is not False
    ):
        _fail("terminal receipt violates the research-only boundary")
    if receipt["manifest_sha256"] != _sha256(manifest_raw):
        _fail("terminal receipt manifest raw hash mismatch")
    if receipt["manifest_hash"] != manifest_verified["hash"]:
        _fail("terminal receipt manifest canonical hash mismatch")
    if receipt["lineage_hashes"] != lineage["hashes"]:
        _fail("terminal receipt lineage hashes mismatch")
    return _verify_claim(receipt, "receipt_hash", "terminal receipt")


def _validate_levels(value: object, label: str, *, descending: bool) -> list[list[float]]:
    if not isinstance(value, list) or not value:
        _fail(f"{label} must be a non-empty depth side")
    normalized: list[list[float]] = []
    for index, level in enumerate(value):
        if not isinstance(level, list) or len(level) != 2:
            _fail(f"{label}[{index}] must contain price and size")
        price = _finite(level[0], f"{label}[{index}].price", positive=True)
        size = _finite(level[1], f"{label}[{index}].size", positive=True)
        normalized.append([price, size])
    for left, right in zip(normalized, normalized[1:]):
        if (descending and left[0] < right[0]) or (
            not descending and left[0] > right[0]
        ):
            _fail(f"{label} is not price sorted")
    return normalized


def _validate_raw_frames(
    frames: list[dict[str, Any]], lineage: Mapping[str, Any]
) -> dict[int, dict[str, Any]]:
    expected_keys = {
        "schema",
        "capture_id",
        "event_lineage_hash",
        "sequence",
        "venue",
        "contract_id",
        "channel",
        "request_ts",
        "received_ts",
        "exchange_ts",
        "payload",
        "frame_hash",
    }
    indexed: dict[int, dict[str, Any]] = {}
    for row_number, raw in enumerate(frames, start=1):
        frame = _require_exact_keys(raw, expected_keys, f"raw frame {row_number}")
        if frame["schema"] != "premarket_perp_l2_raw_frame_v43":
            _fail(f"raw frame {row_number} schema mismatch")
        if (
            frame["capture_id"] != lineage["capture_id"]
            or frame["event_lineage_hash"] != lineage["hashes"]["event"]
            or frame["venue"] != lineage["venue"]
            or frame["contract_id"] != lineage["contract_id"]
            or frame["channel"] != "depth"
        ):
            _fail(f"raw frame {row_number} lineage mismatch")
        sequence = _require_int(frame["sequence"], f"raw frame {row_number}.sequence", positive=True)
        if sequence in indexed:
            _fail("raw frame sequence is duplicated")
        request_ts = _finite(frame["request_ts"], f"raw frame {row_number}.request_ts")
        received_ts = _finite(frame["received_ts"], f"raw frame {row_number}.received_ts")
        exchange_ts = _finite(frame["exchange_ts"], f"raw frame {row_number}.exchange_ts")
        if received_ts < request_ts or exchange_ts > received_ts + 2.0:
            _fail(f"raw frame {row_number} clock is non-causal")
        payload = _require_exact_keys(
            frame["payload"], {"bids", "asks"}, f"raw frame {row_number}.payload"
        )
        bids = _validate_levels(
            payload["bids"], f"raw frame {row_number}.bids", descending=True
        )
        asks = _validate_levels(
            payload["asks"], f"raw frame {row_number}.asks", descending=False
        )
        if bids[0][0] >= asks[0][0]:
            _fail(f"raw frame {row_number} book is crossed")
        _verify_claim(frame, "frame_hash", f"raw frame {row_number}")
        indexed[sequence] = frame
    expected = list(range(1, len(frames) + 1))
    if sorted(indexed) != expected:
        _fail("raw frame sequence is not contiguous from one")
    return indexed


def _validate_normalized_depth(
    payload: dict[str, Any],
    lineage: Mapping[str, Any],
    raw_frames: Mapping[int, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    payload = _require_exact_keys(
        payload,
        {"schema", "capture_id", "event_lineage_hash", "normalizer_sha256", "rows"},
        "normalized depth",
    )
    if payload["schema"] != "premarket_perp_l2_normalized_depth_v43":
        _fail("normalized depth schema mismatch")
    if (
        payload["capture_id"] != lineage["capture_id"]
        or payload["event_lineage_hash"] != lineage["hashes"]["event"]
    ):
        _fail("normalized depth lineage mismatch")
    _require_sha256(payload["normalizer_sha256"], "normalized depth normalizer hash")
    if not isinstance(payload["rows"], list) or not payload["rows"]:
        _fail("normalized depth rows are missing")
    expected_keys = {
        "schema",
        "snapshot_id",
        "capture_id",
        "event_lineage_hash",
        "venue",
        "contract_id",
        "source_frame_sequence",
        "source_frame_hash",
        "request_ts",
        "received_ts",
        "exchange_ts",
        "bids",
        "asks",
    }
    by_id: dict[str, dict[str, Any]] = {}
    seen_sources: set[int] = set()
    rows: list[dict[str, Any]] = []
    for row_number, raw in enumerate(payload["rows"], start=1):
        row = _require_exact_keys(raw, expected_keys, f"depth row {row_number}")
        if row["schema"] != "premarket_perp_depth_snapshot_v1":
            _fail(f"depth row {row_number} schema mismatch")
        snapshot_id = _require_text(row["snapshot_id"], f"depth row {row_number}.snapshot_id")
        if snapshot_id in by_id:
            _fail("normalized depth snapshot_id is duplicated")
        if (
            row["capture_id"] != lineage["capture_id"]
            or row["event_lineage_hash"] != lineage["hashes"]["event"]
            or row["venue"] != lineage["venue"]
            or row["contract_id"] != lineage["contract_id"]
        ):
            _fail(f"depth row {row_number} lineage mismatch")
        source_sequence = _require_int(
            row["source_frame_sequence"],
            f"depth row {row_number}.source_frame_sequence",
            positive=True,
        )
        source = raw_frames.get(source_sequence)
        if source is None or source_sequence in seen_sources:
            _fail(f"depth row {row_number} source frame is missing or reused")
        seen_sources.add(source_sequence)
        if row["source_frame_hash"] != source["frame_hash"]:
            _fail(f"depth row {row_number} source frame hash mismatch")
        for field in ("request_ts", "received_ts", "exchange_ts"):
            _finite(row[field], f"depth row {row_number}.{field}")
            if float(row[field]) != float(source[field]):
                _fail(f"depth row {row_number} {field} differs from raw frame")
        bids = _validate_levels(row["bids"], f"depth row {row_number}.bids", descending=True)
        asks = _validate_levels(row["asks"], f"depth row {row_number}.asks", descending=False)
        if bids[0][0] >= asks[0][0]:
            _fail(f"depth row {row_number} book is crossed")
        if row["bids"] != source["payload"]["bids"] or row["asks"] != source["payload"]["asks"]:
            _fail(f"depth row {row_number} normalization differs from raw frame")
        by_id[snapshot_id] = row
        rows.append(row)
    if seen_sources != set(raw_frames):
        _fail("not every raw L2 frame has exactly one normalized depth row")
    rows.sort(key=lambda item: (float(item["received_ts"]), item["snapshot_id"]))
    return rows, by_id


def _validate_coverage(
    coverage: object,
    lineage: Mapping[str, Any],
    raw_frames: Mapping[int, dict[str, Any]],
    depth_by_id: Mapping[str, dict[str, Any]],
) -> None:
    coverage = _require_exact_keys(
        coverage,
        {
            "status",
            "gap_free",
            "sequence_start",
            "sequence_end",
            "missing_sequences",
            "points",
        },
        "manifest.coverage",
    )
    if coverage["status"] != "COMPLETE" or coverage["gap_free"] is not True:
        _fail("capture coverage is not COMPLETE and gap-free")
    if (
        coverage["sequence_start"] != 1
        or coverage["sequence_end"] != len(raw_frames)
        or coverage["missing_sequences"] != []
    ):
        _fail("capture coverage sequence declaration is not gap-free")
    points = coverage["points"]
    if not isinstance(points, list) or len(points) != len(REQUIRED_OFFSETS):
        _fail("capture coverage does not contain entry plus all fixed exits")
    seen: dict[int, str] = {}
    for index, raw in enumerate(points):
        point = _require_exact_keys(
            raw, {"role", "offset_sec", "snapshot_id", "gap_free"}, f"coverage point {index}"
        )
        offset = _require_int(point["offset_sec"], f"coverage point {index}.offset_sec")
        expected_role = "entry" if offset == -60 else "exit"
        if offset not in REQUIRED_OFFSETS or point["role"] != expected_role or point["gap_free"] is not True:
            _fail(f"coverage point {index} is not a fixed gap-free entry/exit")
        snapshot_id = _require_text(point["snapshot_id"], f"coverage point {index}.snapshot_id")
        if offset in seen or snapshot_id not in depth_by_id:
            _fail(f"coverage point {index} is duplicated or lacks normalized depth")
        row = depth_by_id[snapshot_id]
        target = float(lineage["official_spot_t0"] + offset)
        eligible = target + float(EXPECTED_MODEL["latency_ms"]) / 1000.0
        deadline = eligible + float(EXPECTED_MODEL["order_ttl_ms"]) / 1000.0
        received = float(row["received_ts"])
        if received < eligible or received > deadline:
            _fail(f"coverage point {offset} has no causal depth inside fixed TTL")
        seen[offset] = snapshot_id
    if tuple(sorted(seen)) != tuple(sorted(REQUIRED_OFFSETS)):
        _fail("capture coverage fixed offsets mismatch")


def _validate_contract_cost(
    payload: dict[str, Any], lineage: Mapping[str, Any]
) -> tuple[dict[str, Any], float]:
    payload = _require_exact_keys(
        payload,
        {
            "schema",
            "capture_id",
            "event_lineage_hash",
            "taker_fee_bps",
            "fee_evidence",
            "contract_spec",
        },
        "contract-cost",
    )
    if payload["schema"] != "premarket_perp_l2_contract_cost_v43":
        _fail("contract-cost schema mismatch")
    if (
        payload["capture_id"] != lineage["capture_id"]
        or payload["event_lineage_hash"] != lineage["hashes"]["event"]
    ):
        _fail("contract-cost lineage mismatch")
    fee = _finite(payload["taker_fee_bps"], "contract-cost.taker_fee_bps")
    if fee < 0:
        _fail("contract-cost.taker_fee_bps must be non-negative")
    fee_evidence = _require_exact_keys(
        payload["fee_evidence"],
        {"source_class", "observed_ts", "raw_sha256"},
        "contract-cost.fee_evidence",
    )
    if fee_evidence["source_class"] != "VENUE_PUBLIC_FEE_SCHEDULE":
        _fail("fee evidence is not a public venue schedule")
    observed_ts = _finite(
        fee_evidence["observed_ts"], "contract-cost.fee_evidence.observed_ts"
    )
    if observed_ts > float(lineage["official_spot_t0"]):
        _fail("fee evidence was received after the event")
    _require_sha256(
        fee_evidence["raw_sha256"], "contract-cost.fee_evidence.raw_sha256"
    )

    spec = _require_exact_keys(
        payload["contract_spec"],
        {
            "schema",
            "venue",
            "contract_id",
            "base_currency",
            "quote_currency",
            "settle_currency",
            "size_unit",
            "base_per_size_unit",
            "price_tick",
            "quantity_step_size_units",
            "min_quantity_size_units",
            "min_notional_usdt",
            "maintenance_margin_rate",
            "price_limit_low",
            "price_limit_high",
            "received_ts",
            "raw_sha256",
        },
        "contract-cost.contract_spec",
    )
    if spec["schema"] != "premarket_perp_contract_spec_v1":
        _fail("contract spec schema mismatch")
    if spec["venue"] != lineage["venue"] or spec["contract_id"] != lineage["contract_id"]:
        _fail("contract spec does not bind the event venue and contract")
    for field in ("base_currency", "quote_currency", "settle_currency", "size_unit"):
        _require_text(spec[field], f"contract spec {field}")
    for field in (
        "base_per_size_unit",
        "price_tick",
        "quantity_step_size_units",
        "min_quantity_size_units",
        "min_notional_usdt",
        "price_limit_low",
        "price_limit_high",
    ):
        _finite(spec[field], f"contract spec {field}", positive=True)
    maintenance = _finite(
        spec["maintenance_margin_rate"], "contract spec maintenance_margin_rate"
    )
    if maintenance < 0 or maintenance >= 1:
        _fail("contract spec maintenance margin rate is outside [0, 1)")
    if float(spec["price_limit_low"]) >= float(spec["price_limit_high"]):
        _fail("contract spec price limits are invalid")
    received_ts = _finite(spec["received_ts"], "contract spec received_ts")
    if received_ts > float(lineage["official_spot_t0"] - 60):
        _fail("contract spec was not causally available for the fixed entry")
    _require_sha256(spec["raw_sha256"], "contract spec raw_sha256")
    return spec, fee


def _validate_funding(
    payload: dict[str, Any], lineage: Mapping[str, Any]
) -> list[dict[str, Any]]:
    payload = _require_exact_keys(
        payload,
        {
            "schema",
            "capture_id",
            "event_lineage_hash",
            "coverage_start_ts",
            "coverage_end_ts",
            "schedule_status",
            "source_raw_sha256",
            "rows",
        },
        "funding",
    )
    if payload["schema"] != "premarket_perp_l2_funding_v43":
        _fail("funding schema mismatch")
    if (
        payload["capture_id"] != lineage["capture_id"]
        or payload["event_lineage_hash"] != lineage["hashes"]["event"]
        or payload["schedule_status"] != "VERIFIED_COMPLETE"
    ):
        _fail("funding coverage or lineage is incomplete")
    start = _finite(payload["coverage_start_ts"], "funding.coverage_start_ts")
    end = _finite(payload["coverage_end_ts"], "funding.coverage_end_ts")
    required_start = float(lineage["official_spot_t0"] - 60)
    required_end = float(lineage["official_spot_t0"] + 60.7)
    if start > required_start or end < required_end or end < start:
        _fail("funding evidence does not cover the entire paper position window")
    _require_sha256(payload["source_raw_sha256"], "funding.source_raw_sha256")
    if not isinstance(payload["rows"], list):
        _fail("funding.rows must be a list")
    rows: list[dict[str, Any]] = []
    ids: set[str] = set()
    settlements: set[float] = set()
    for index, raw in enumerate(payload["rows"]):
        row = _require_exact_keys(
            raw,
            {
                "schema",
                "observation_id",
                "settlement_ts",
                "received_ts",
                "rate",
                "settlement_mark_price",
            },
            f"funding row {index}",
        )
        if row["schema"] != "premarket_perp_funding_settlement_v1":
            _fail(f"funding row {index} schema mismatch")
        observation_id = _require_text(
            row["observation_id"], f"funding row {index}.observation_id"
        )
        settlement_ts = _finite(row["settlement_ts"], f"funding row {index}.settlement_ts")
        received_ts = _finite(row["received_ts"], f"funding row {index}.received_ts")
        _finite(row["rate"], f"funding row {index}.rate")
        _finite(
            row["settlement_mark_price"],
            f"funding row {index}.settlement_mark_price",
            positive=True,
        )
        if observation_id in ids or settlement_ts in settlements or received_ts < settlement_ts:
            _fail(f"funding row {index} is duplicated or non-causal")
        if settlement_ts < start or settlement_ts > end:
            _fail(f"funding row {index} is outside declared coverage")
        ids.add(observation_id)
        settlements.add(settlement_ts)
        rows.append(row)
    rows.sort(key=lambda item: (float(item["settlement_ts"]), item["observation_id"]))
    return rows


def _validate_mark_index(
    payload: dict[str, Any], lineage: Mapping[str, Any]
) -> list[dict[str, Any]]:
    payload = _require_exact_keys(
        payload,
        {
            "schema",
            "capture_id",
            "event_lineage_hash",
            "coverage_start_ts",
            "coverage_end_ts",
            "gap_free",
            "source_raw_sha256",
            "required_offsets_sec",
            "rows",
        },
        "mark-index",
    )
    if payload["schema"] != "premarket_perp_l2_mark_index_v43":
        _fail("mark-index schema mismatch")
    if (
        payload["capture_id"] != lineage["capture_id"]
        or payload["event_lineage_hash"] != lineage["hashes"]["event"]
        or payload["gap_free"] is not True
        or payload["required_offsets_sec"] != list(REQUIRED_OFFSETS)
    ):
        _fail("mark-index coverage or lineage is incomplete")
    start = _finite(payload["coverage_start_ts"], "mark-index.coverage_start_ts")
    end = _finite(payload["coverage_end_ts"], "mark-index.coverage_end_ts")
    required_start = float(lineage["official_spot_t0"] - 60)
    required_end = float(lineage["official_spot_t0"] + 60.7)
    if start > required_start or end < required_end or end < start:
        _fail("mark-index evidence does not cover the entire paper position window")
    _require_sha256(payload["source_raw_sha256"], "mark-index.source_raw_sha256")
    if not isinstance(payload["rows"], list) or not payload["rows"]:
        _fail("mark-index.rows are missing")
    expected_keys = {
        "schema",
        "observation_id",
        "received_ts",
        "exchange_ts",
        "mark_price",
        "index_price",
    }
    rows: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw in enumerate(payload["rows"]):
        row = _require_exact_keys(raw, expected_keys, f"mark-index row {index}")
        if row["schema"] != "premarket_perp_mark_index_observation_v1":
            _fail(f"mark-index row {index} schema mismatch")
        observation_id = _require_text(
            row["observation_id"], f"mark-index row {index}.observation_id"
        )
        received = _finite(row["received_ts"], f"mark-index row {index}.received_ts")
        exchange = _finite(row["exchange_ts"], f"mark-index row {index}.exchange_ts")
        _finite(row["mark_price"], f"mark-index row {index}.mark_price", positive=True)
        _finite(row["index_price"], f"mark-index row {index}.index_price", positive=True)
        if observation_id in ids or exchange > received + 2.0:
            _fail(f"mark-index row {index} is duplicated or non-causal")
        ids.add(observation_id)
        rows.append(row)
    for offset in REQUIRED_OFFSETS:
        target = float(lineage["official_spot_t0"] + offset)
        eligible = target + float(EXPECTED_MODEL["latency_ms"]) / 1000.0
        deadline = eligible + float(EXPECTED_MODEL["order_ttl_ms"]) / 1000.0
        if not any(eligible <= float(row["received_ts"]) <= deadline for row in rows):
            _fail(f"mark-index evidence lacks causal coverage at offset {offset}")
    rows.sort(key=lambda item: (float(item["received_ts"]), item["observation_id"]))
    return rows


def inspect_candidate_execution_request(capture_dir: Path) -> dict[str, Any]:
    """Verify one candidate bundle's internal chain without promoting authority.

    The sole caller-controlled value is the directory path.  Evidence rows, fee,
    contract risk parameters and model constants are loaded only from verified files
    or the already-registered execution model.  The result is intentionally *not* a
    trusted handoff: the PlanOnly, registry/arming head, production capture path and
    claim-release archive still need independent event-specific verification.
    """

    _root, raw_by_name = _read_fixed_directory(Path(capture_dir))
    parsed = {
        name: _parse_canonical_json(raw_by_name[name], name)
        for name in EVIDENCE_FILE_NAMES
        if name != "raw-frames.jsonl"
    }
    lineage = _validate_lineage(parsed["lineage.json"])
    manifest = _parse_canonical_json(raw_by_name[MANIFEST_FILE_NAME], MANIFEST_FILE_NAME)
    manifest_verified = _validate_manifest(manifest, raw_by_name, lineage)
    receipt = _parse_canonical_json(raw_by_name[RECEIPT_FILE_NAME], RECEIPT_FILE_NAME)
    receipt_hash = _validate_receipt(
        receipt,
        raw_by_name[MANIFEST_FILE_NAME],
        manifest_verified,
        lineage,
    )

    frames = _parse_raw_tape(raw_by_name["raw-frames.jsonl"])
    raw_frames = _validate_raw_frames(frames, lineage)
    depth_rows, depth_by_id = _validate_normalized_depth(
        parsed["normalized-depth.json"], lineage, raw_frames
    )
    _validate_coverage(
        manifest_verified["coverage"], lineage, raw_frames, depth_by_id
    )
    contract_spec, taker_fee_bps = _validate_contract_cost(
        parsed["contract-cost.json"], lineage
    )
    funding_rows = _validate_funding(parsed["funding.json"], lineage)
    mark_rows = _validate_mark_index(parsed["mark-index.json"], lineage)

    event_source = lineage["event"]
    request: dict[str, Any] = {
        "schema": "premarket_perp_execution_replay_request_v1",
        "sealed": True,
        "evidence_class": "SEALED_L2_CAPTURE",
        "capture_manifest_sha256": _sha256(raw_by_name[MANIFEST_FILE_NAME]),
        "event": {
            "event_id": lineage["event_id"],
            "venue": lineage["venue"],
            "contract_id": lineage["contract_id"],
            "official_spot_t0": lineage["official_spot_t0"],
            "t0_source_class": event_source["t0_source_class"],
            "t0_precision_sec": event_source["t0_precision_sec"],
            "official_record_hash": event_source["official_record_hash"],
            "official_source_url": event_source["official_source_url"],
            "evidence_class": "SEALED_L2_CAPTURE",
        },
        "model": {**copy.deepcopy(EXPECTED_MODEL), "taker_fee_bps": taker_fee_bps},
        "contract_spec": copy.deepcopy(contract_spec),
        "depth_snapshots": copy.deepcopy(depth_rows),
        "funding_observations": copy.deepcopy(funding_rows),
        "mark_index_observations": copy.deepcopy(mark_rows),
        "trusted_loader_verification": {
            "schema": "premarket_perp_l2_loader_verification_v43",
            "status": "INTERNAL_CHAIN_ONLY_NOT_TRUSTED",
            "capture_id": lineage["capture_id"],
            "manifest_hash": manifest_verified["hash"],
            "terminal_receipt_hash": receipt_hash,
            "raw_frames_sha256": manifest_verified["file_hashes"]["raw-frames.jsonl"],
            "normalized_depth_sha256": manifest_verified["file_hashes"]["normalized-depth.json"],
            "lineage_hashes": copy.deepcopy(lineage["hashes"]),
            "exact_readback": True,
            "gap_free": True,
            "acceptance_capable": False,
            "external_authority_verified": False,
            "trusted_replay_handoff": False,
        },
        "orders_created": 0,
        "private_api_used": False,
        "live_execution": False,
    }
    request["evidence_envelope"] = {
        "schema": "premarket_perp_execution_evidence_envelope_v1",
        "sealed": True,
        "evidence_class": "SEALED_L2_CAPTURE",
        "capture_manifest_sha256": request["capture_manifest_sha256"],
        "payload_sha256": canonical_result_hash(request),
    }
    return request


def load_verified_execution_request(capture_dir: Path) -> dict[str, Any]:
    """Fail closed until event-specific external authority is implemented.

    Parsing the bundle first keeps corruption diagnostics useful, but no internally
    self-consistent directory can become trusted evidence on its own.  A future v43
    release must replace this terminal refusal with checks against the active
    event-bound PlanOnly, historical registry prefix, durable arming/proposal heads,
    fixed production path, independent terminal receipt and claim-release archive,
    then perform replay inside that verified boundary.
    """

    inspect_candidate_execution_request(capture_dir)
    _fail("EXTERNAL_V43_AUTHORITY_VERIFIER_REQUIRED")


def verify_l2_capture_bundle(capture_dir: Path) -> dict[str, Any]:
    """Compatibility name; it retains the same production fail-closed boundary."""

    return load_verified_execution_request(capture_dir)
