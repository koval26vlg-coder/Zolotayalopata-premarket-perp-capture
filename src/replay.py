"""Causal, descriptive replay over captured public order-book observations.

A REST response becomes available only at ``received_ts``.  Consequently a target is
represented by the first valid response received at or after it, provided that response
arrived within one declared order-book cadence.  A pre-target response is never a
fallback, and two observations around a missing target do not bound the unobserved
market path.

The output is a gross top-of-book markout: entry uses the observed ask and exit uses the
observed bid.  It is descriptive public-data research only; it says nothing about an
order outcome, costs, or a strategy decision.  The module opens no socket, takes no
claim, and writes no project artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import capture as capture_runtime
import event_registry
import project_config as config
import risk_gate
from canonical_hash import canonical_hash


REPLAY_SCHEMA = "premarket_perp_replay_v2"
CAPTURE_SCHEMA = "premarket_perp_capture_v1"
SAMPLE_SCHEMA = "premarket_perp_capture_sample_v1"
RECEIPT_SCHEMA = "premarket_perp_capture_receipt_v1"
PRODUCTION_EVIDENCE_MODE = "PRODUCTION_VERIFIED"
SYNTHETIC_EVIDENCE_MODE = "SYNTHETIC_DESCRIPTIVE_ONLY"
SYNTHETIC_EVIDENCE_CLASSES = frozenset({
    "SYNTHETIC_OFFLINE_ONLY",
    "SYNTHETIC_OFFLINE_FIXTURE_ONLY",
})
PRODUCTION_EVIDENCE_CLASSES = frozenset({
    "CAUSAL_REPLAY_INPUT_READY",
    "DESCRIPTIVE_ONLY",
})
SAFE_CAPTURE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

REQUIRED_LINEAGE_HASH_FIELDS = (
    "official_record_hash",
    "registry_sha256",
    "registry_tail_record_hash",
    "mutation_receipt_hash",
    "summary_content_sha256",
    "registry_authority_state_hash",
    "asset_identity_hash",
    "plan_hash",
)
REQUIRED_LINEAGE_IDENTITY_FIELDS = (
    "episode_id",
    "venue",
    "listing_venue",
    "premarket_contract_id",
    "spot_symbol",
    "t0_source_class",
    "asset_class",
    "issuer_namespace",
    "issuer_id",
    "plan_id",
    "official_source_url",
    "official_source_identity",
)
RECEIPT_LINEAGE_DUPLICATES = config.CAPTURE_LINEAGE_FIELDS
DEFAULT_HORIZONS_SEC = config.PRIMARY_EXIT_OFFSETS_SEC

# How long before t0 the entry is priced. The capture window opens 30 minutes early;
# the descriptive entry target is one minute before t0.
DEFAULT_ENTRY_LEAD_SEC = config.PRIMARY_ENTRY_LEAD_SEC


class ReplayError(RuntimeError):
    pass


# ------------------------------------------------------------------ reading a capture


@dataclass(frozen=True)
class ReplayEvidence:
    manifest: dict[str, Any]
    samples: tuple[dict[str, Any], ...]
    receipt: dict[str, Any] | None
    evidence_mode: str
    production_verified: bool


def _read_json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ReplayError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise ReplayError(f"{label} is not a JSON object")
    return dict(value)


def _require_hash(value: Any, label: str) -> str:
    digest = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ReplayError(f"{label} is missing or is not a lowercase sha256")
    return digest


def _safe_capture_id(value: Any) -> str:
    capture_id = str(value or "")
    if not SAFE_CAPTURE_ID.fullmatch(capture_id):
        raise ReplayError("capture_id is missing or is not a safe path component")
    return capture_id


def _read_capture_bytes(
    capture_dir: Path,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], bytes, bytes]:
    """Read immutable-looking bytes without assigning them production authority."""
    manifest_path = capture_dir / "manifest.json"
    samples_path = capture_dir / "samples.jsonl"
    if not manifest_path.is_file():
        raise ReplayError(f"no manifest in {capture_dir}")
    if not samples_path.is_file():
        raise ReplayError(f"no samples in {capture_dir}")

    try:
        manifest_raw = manifest_path.read_bytes()
        samples_raw = samples_path.read_bytes()
    except OSError as exc:
        raise ReplayError(f"capture artifacts are unreadable in {capture_dir}") from exc
    manifest = _read_json_object(manifest_raw, "capture manifest")
    if manifest.get("schema") != CAPTURE_SCHEMA:
        raise ReplayError("capture manifest schema mismatch")
    capture_id = _safe_capture_id(manifest.get("capture_id"))

    digest = hashlib.sha256(samples_raw).hexdigest()
    recorded = _require_hash(manifest.get("output_sha256"), "manifest output_sha256")
    if digest != recorded:
        raise ReplayError(
            "samples do not match the manifest that authenticates them: "
            f"file {digest[:16]}, manifest {recorded[:16]}"
        )

    try:
        lines = samples_raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ReplayError("capture samples are not UTF-8 JSONL") from exc
    samples: list[dict[str, Any]] = []
    for row_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        sample = _read_json_object(line.encode("utf-8"), f"sample row {row_number}")
        if sample.get("schema") != SAMPLE_SCHEMA:
            raise ReplayError(f"sample row {row_number} schema mismatch")
        if sample.get("capture_id") != capture_id:
            raise ReplayError(f"sample row {row_number} capture_id mismatch")
        samples.append(sample)
    return manifest, tuple(samples), manifest_raw, samples_raw


def _strict_production_directory(capture_dir: Path) -> tuple[Path, Path]:
    try:
        root = config.CAPTURE_ROOT.resolve(strict=True)
        resolved = capture_dir.resolve(strict=True)
    except OSError as exc:
        raise ReplayError("production capture path or capture root is unavailable") from exc
    if not root.is_dir() or not resolved.is_dir():
        raise ReplayError("production capture path and capture root must be directories")
    if resolved == root or not resolved.is_relative_to(root):
        raise ReplayError("production capture_dir must be a strict descendant of CAPTURE_ROOT")
    return resolved, root


def _is_within_production_root(capture_dir: Path) -> bool:
    try:
        root = config.CAPTURE_ROOT.resolve(strict=True)
        resolved = capture_dir.resolve(strict=True)
    except OSError:
        return False
    return resolved == root or resolved.is_relative_to(root)


def _read_fixed_receipt(capture_id: str) -> tuple[dict[str, Any], bytes]:
    try:
        evidence_root = config.EVIDENCE_DIR.resolve(strict=True)
    except OSError as exc:
        raise ReplayError("production evidence directory is unavailable") from exc
    receipt_path = config.EVIDENCE_DIR / f"{capture_id}.json"
    if not receipt_path.is_file():
        raise ReplayError(f"immutable production receipt is missing: {receipt_path}")
    try:
        resolved_receipt = receipt_path.resolve(strict=True)
    except OSError as exc:
        raise ReplayError(f"immutable production receipt is unreadable: {receipt_path}") from exc
    if resolved_receipt.parent != evidence_root:
        raise ReplayError("immutable production receipt escaped EVIDENCE_DIR")
    try:
        raw = resolved_receipt.read_bytes()
    except OSError as exc:
        raise ReplayError(f"immutable production receipt is unreadable: {receipt_path}") from exc
    receipt = _read_json_object(raw, "capture receipt")
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise ReplayError("capture receipt schema mismatch")
    recorded_hash = _require_hash(receipt.get("receipt_hash"), "receipt_hash")
    material = dict(receipt)
    material.pop("receipt_hash", None)
    if canonical_hash(material) != recorded_hash:
        raise ReplayError("capture receipt_hash does not match its canonical content")
    return receipt, raw


def _validate_implementation(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReplayError("implementation binding is missing or malformed")
    implementation = dict(value)
    files = implementation.get("files")
    if not isinstance(files, list) or not files:
        raise ReplayError("implementation binding must contain at least one file")
    seen_paths: set[str] = set()
    for index, item in enumerate(files):
        if not isinstance(item, Mapping):
            raise ReplayError(f"implementation file binding {index} is malformed")
        path = str(item.get("repo_path") or "")
        if not path or path in seen_paths:
            raise ReplayError("implementation file paths are missing or duplicated")
        seen_paths.add(path)
        _require_hash(item.get("sha256"), f"implementation sha256 for {path}")
    return implementation


def _validate_production_samples(
    manifest: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Rebind every sample and recompute the capture's causal readiness."""
    venue = str(manifest.get("venue") or "")
    symbol = str(manifest.get("symbol") or "")
    t0_ts = manifest.get("t0_ts")
    precision = manifest.get("t0_precision_sec")
    if not venue or not symbol:
        raise ReplayError("production manifest venue or symbol is missing")
    if isinstance(t0_ts, bool) or not isinstance(t0_ts, (int, float)):
        raise ReplayError("production manifest t0_ts is invalid")
    if isinstance(precision, bool) or not isinstance(precision, int) or precision <= 0:
        raise ReplayError("production manifest t0_precision_sec is invalid")
    probes = {probe.probe: probe for probe in capture_runtime.probes_for(venue)}
    if not probes:
        raise ReplayError("production manifest venue has no bound probes")

    for row_number, sample in enumerate(samples, start=1):
        expected_surface = {
            "venue": venue,
            "symbol": symbol,
            "t0_ts": t0_ts,
        }
        for field, expected in expected_surface.items():
            if sample.get(field) != expected:
                raise ReplayError(f"sample row {row_number} {field} mismatch")
        probe_name = str(sample.get("probe") or "")
        probe = probes.get(probe_name)
        if probe is None:
            raise ReplayError(f"sample row {row_number} probe is not bound to venue")
        expected_request = capture_runtime.request_identity_for(probe, symbol)
        for field, expected in expected_request.items():
            if sample.get(field) != expected:
                raise ReplayError(
                    f"sample row {row_number} {field} request binding mismatch"
                )
        try:
            request_ts = float(sample["request_ts"])
            received_ts = float(sample["received_ts"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReplayError(
                f"sample row {row_number} request/received clock is invalid"
            ) from exc
        if (
            not math.isfinite(request_ts)
            or not math.isfinite(received_ts)
            or received_ts < request_ts
        ):
            raise ReplayError(
                f"sample row {row_number} request/received clock is noncausal"
            )
        if "offset_sec" in sample:
            try:
                offset = float(sample["offset_sec"])
            except (TypeError, ValueError) as exc:
                raise ReplayError(f"sample row {row_number} offset_sec is invalid") from exc
            if not math.isfinite(offset) or abs(offset - (request_ts - float(t0_ts))) > 0.0011:
                raise ReplayError(f"sample row {row_number} offset_sec mismatch")
        if sample.get("error"):
            continue
        if "payload" not in sample:
            raise ReplayError(f"sample row {row_number} successful payload is missing")
        try:
            exchange_ts = capture_runtime.validate_probe_payload(
                venue,
                probe_name,
                sample["payload"],
                symbol,
                received_ts=received_ts,
                request_url=sample.get("request_url"),
                request_params=sample.get("request_params"),
                request_identity_sha256=sample.get("request_identity_sha256"),
            )
            recorded_exchange_ts = float(sample["exchange_ts"])
        except Exception as exc:
            raise ReplayError(
                f"sample row {row_number} market payload binding is invalid"
            ) from exc
        if (
            not math.isfinite(recorded_exchange_ts)
            or abs(recorded_exchange_ts - exchange_ts) > 0.0011
        ):
            raise ReplayError(f"sample row {row_number} exchange_ts mismatch")

    recomputed = capture_runtime.replay_readiness(
        samples,
        t0_ts=float(t0_ts),
        t0_precision_sec=int(precision),
        required_probes=capture_runtime.required_replay_probes_for(venue),
    )
    if manifest.get("replay_readiness") != recomputed:
        raise ReplayError("manifest replay_readiness does not match raw samples")
    expected_classification = capture_runtime.capture_evidence_classification(recomputed)
    for field, expected in expected_classification.items():
        if manifest.get(field) != expected:
            raise ReplayError(
                f"manifest {field} does not match recomputed replay readiness"
            )
    return recomputed


def _validate_production_authority(
    *,
    capture_dir: Path,
    manifest: Mapping[str, Any],
    manifest_raw: bytes,
    samples: Sequence[Mapping[str, Any]],
    samples_raw: bytes,
) -> dict[str, Any]:
    capture_id = _safe_capture_id(manifest.get("capture_id"))
    if capture_dir.name != capture_id:
        raise ReplayError("production capture directory name and capture_id mismatch")
    if manifest.get("acceptance_capable") is not False:
        raise ReplayError("production capture acceptance_capable must be false")
    if manifest.get("evidence_class") not in PRODUCTION_EVIDENCE_CLASSES:
        raise ReplayError("production capture evidence_class mismatch")

    receipt, _receipt_raw = _read_fixed_receipt(capture_id)
    if receipt.get("capture_id") != capture_id:
        raise ReplayError("receipt and manifest capture_id mismatch")
    if receipt.get("manifest_sha256") != hashlib.sha256(manifest_raw).hexdigest():
        raise ReplayError("receipt manifest_sha256 does not match raw manifest bytes")
    samples_sha256 = hashlib.sha256(samples_raw).hexdigest()
    if receipt.get("output_sha256") != samples_sha256:
        raise ReplayError("receipt output_sha256 does not match raw samples bytes")
    if manifest.get("output_sha256") != samples_sha256:
        raise ReplayError("manifest output_sha256 does not match raw samples bytes")

    for field in (
        "venue",
        "symbol",
        "t0_ts",
        "t0_source_class",
        "t0_precision_sec",
        "evidence_class",
        "acceptance_capable",
    ):
        if receipt.get(field) != manifest.get(field):
            raise ReplayError(f"receipt and manifest {field} mismatch")

    recomputed_readiness = _validate_production_samples(manifest, samples)
    if receipt.get("replay_readiness") != recomputed_readiness:
        raise ReplayError("receipt replay_readiness does not match raw samples")

    manifest_lineage = manifest.get("lineage")
    receipt_lineage = receipt.get("lineage")
    if not isinstance(manifest_lineage, Mapping) or not isinstance(receipt_lineage, Mapping):
        raise ReplayError("manifest or receipt lineage is missing")
    manifest_lineage = dict(manifest_lineage)
    receipt_lineage = dict(receipt_lineage)
    if receipt_lineage != manifest_lineage:
        raise ReplayError("receipt and manifest lineage mismatch")

    for field in REQUIRED_LINEAGE_HASH_FIELDS:
        _require_hash(manifest_lineage.get(field), f"lineage {field}")
    for field in REQUIRED_LINEAGE_IDENTITY_FIELDS:
        if not str(manifest_lineage.get(field) or "").strip():
            raise ReplayError(f"lineage {field} is missing")
    mutation_seq = manifest_lineage.get("mutation_receipt_seq")
    if isinstance(mutation_seq, bool) or not isinstance(mutation_seq, int) or mutation_seq < 0:
        raise ReplayError("lineage mutation_receipt_seq is missing or invalid")
    if manifest_lineage.get("asset_class") != "CRYPTO_TOKEN":
        raise ReplayError("lineage asset_class must be CRYPTO_TOKEN")
    if manifest_lineage.get("t0_source_class") != "OFFICIAL_ANNOUNCEMENT":
        raise ReplayError("lineage t0_source_class must be OFFICIAL_ANNOUNCEMENT")
    official_t0 = manifest_lineage.get("official_spot_t0")
    if isinstance(official_t0, bool) or not isinstance(official_t0, int):
        raise ReplayError("lineage official_spot_t0 is missing or invalid")
    if official_t0 <= 0:
        raise ReplayError("lineage official_spot_t0 is missing or invalid")

    expected_surface = {
        "venue": manifest.get("venue"),
        "premarket_contract_id": manifest.get("symbol"),
        "official_spot_t0": manifest.get("t0_ts"),
        "t0_source_class": manifest.get("t0_source_class"),
        "t0_precision_sec": manifest.get("t0_precision_sec"),
    }
    for field, expected in expected_surface.items():
        if manifest_lineage.get(field) != expected:
            raise ReplayError(f"manifest and lineage {field} mismatch")

    for field in RECEIPT_LINEAGE_DUPLICATES:
        if receipt.get(field) != manifest_lineage.get(field):
            raise ReplayError(f"receipt duplicate lineage {field} mismatch")

    plan_id = str(manifest.get("plan_id") or "")
    plan_hash = _require_hash(manifest.get("plan_hash"), "manifest plan_hash")
    if not plan_id:
        raise ReplayError("manifest plan_id is missing")
    if manifest_lineage.get("plan_id") != plan_id:
        raise ReplayError("manifest plan_id and lineage mismatch")
    if manifest_lineage.get("plan_hash") != plan_hash:
        raise ReplayError("manifest plan_hash and lineage mismatch")
    if receipt.get("plan_id") != plan_id or receipt.get("plan_hash") != plan_hash:
        raise ReplayError("receipt and manifest plan identity mismatch")

    implementation = _validate_implementation(manifest.get("implementation"))
    if receipt.get("implementation") != implementation:
        raise ReplayError("receipt and manifest implementation binding mismatch")
    for row_number, sample in enumerate(samples, start=1):
        optional_bindings = {
            "plan_id": plan_id,
            "plan_hash": plan_hash,
            "implementation": implementation,
        }
        for field, expected in optional_bindings.items():
            if field in sample and sample.get(field) != expected:
                raise ReplayError(f"sample row {row_number} {field} binding mismatch")

    plan_verifier = getattr(risk_gate, "verify_plan_identity", None)
    if not callable(plan_verifier):
        raise ReplayError("risk gate plan identity verifier is unavailable")
    try:
        plan_report = plan_verifier(
            plan_id=plan_id,
            plan_hash=plan_hash,
            implementation=implementation,
            required_write_class="market_data_capture",
        )
    except Exception as exc:  # fail closed across verifier implementations
        raise ReplayError("risk gate plan identity verification failed") from exc
    if (
        not isinstance(plan_report, Mapping)
        or plan_report.get("ok") is not True
        or plan_report.get("status") != "PLAN_IDENTITY_OK"
    ):
        raise ReplayError("risk gate plan identity verification was not successful")
    if (
        plan_report.get("evidence_origin_capture_authorized") is not True
        or plan_report.get("evidence_origin_write_class") != "market_data_capture"
    ):
        raise ReplayError(
            "selected historical PlanOnly had no market-data capture authority"
        )
    origin_root_raw = plan_report.get("evidence_origin_capture_root")
    if not isinstance(origin_root_raw, str) or not origin_root_raw.strip():
        raise ReplayError("selected historical PlanOnly carries no capture root")
    # Judged before resolve(): on a foreign OS resolve() would silently prepend the
    # working directory to a Windows path and call the result absolute.
    if not config.path_is_absolute(origin_root_raw):
        raise ReplayError("selected historical PlanOnly capture root is not absolute")
    origin_root = Path(origin_root_raw).resolve(strict=False)
    try:
        relative_to_origin = capture_dir.resolve(strict=False).relative_to(origin_root)
    except ValueError as exc:
        raise ReplayError(
            "capture is outside the selected historical PlanOnly capture root"
        ) from exc
    if not relative_to_origin.parts:
        raise ReplayError(
            "capture must be a strict descendant of the historical PlanOnly capture root"
        )

    combined_evidence = dict(manifest_lineage)
    combined_evidence["capture_id"] = capture_id
    try:
        lineage_report = event_registry.verify_capture_lineage(combined_evidence)
    except Exception as exc:  # the registry owns the semantic lineage rules
        raise ReplayError("registry capture lineage verification failed") from exc
    if (
        not isinstance(lineage_report, Mapping)
        or lineage_report.get("ok") is not True
        or lineage_report.get("status") != "CAPTURE_LINEAGE_OK"
    ):
        raise ReplayError("registry capture lineage verification was not successful")

    return receipt


def load_replay_evidence(
    capture_dir: Path,
    *,
    evidence_mode: str = PRODUCTION_EVIDENCE_MODE,
) -> ReplayEvidence:
    """Load either verified production evidence or an explicitly synthetic fixture."""
    capture_dir = Path(capture_dir)
    if evidence_mode == PRODUCTION_EVIDENCE_MODE:
        capture_dir, _root = _strict_production_directory(capture_dir)
        manifest, samples, manifest_raw, samples_raw = _read_capture_bytes(capture_dir)
        receipt = _validate_production_authority(
            capture_dir=capture_dir,
            manifest=manifest,
            manifest_raw=manifest_raw,
            samples=samples,
            samples_raw=samples_raw,
        )
        return ReplayEvidence(
            manifest=manifest,
            samples=samples,
            receipt=receipt,
            evidence_mode=evidence_mode,
            production_verified=True,
        )
    if evidence_mode == SYNTHETIC_EVIDENCE_MODE:
        if _is_within_production_root(capture_dir):
            raise ReplayError(
                "a capture under CAPTURE_ROOT cannot be downgraded to synthetic evidence"
            )
        manifest, samples, _manifest_raw, _samples_raw = _read_capture_bytes(capture_dir)
        if manifest.get("evidence_class") not in SYNTHETIC_EVIDENCE_CLASSES:
            raise ReplayError(
                "synthetic replay requires an explicit synthetic evidence_class"
            )
        if manifest.get("acceptance_capable") is not False:
            raise ReplayError("synthetic replay acceptance_capable must be false")
        return ReplayEvidence(
            manifest=manifest,
            samples=samples,
            receipt=None,
            evidence_mode=evidence_mode,
            production_verified=False,
        )
    raise ReplayError(f"unknown replay evidence_mode: {evidence_mode}")


def load_capture(
    capture_dir: Path,
    *,
    evidence_mode: str = PRODUCTION_EVIDENCE_MODE,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compatibility view over the strict replay-evidence loader."""
    evidence = load_replay_evidence(capture_dir, evidence_mode=evidence_mode)
    return evidence.manifest, list(evidence.samples)


# --------------------------------------------------------------------- book extraction


def _as_pair(row: Any) -> tuple[float, float] | None:
    """A depth level, in whichever shape the venue writes it."""
    if isinstance(row, Mapping):
        price, size = row.get("p"), row.get("s")
    elif isinstance(row, (list, tuple)) and len(row) >= 2:
        price, size = row[0], row[1]
    else:
        return None
    try:
        return float(price), float(size)
    except (TypeError, ValueError):
        return None


def top_of_book(venue: str, payload: Any) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Best bid and best ask, each as (price, visible size)."""
    if venue == "bybit":
        result = (payload or {}).get("result") if isinstance(payload, Mapping) else None
        bids, asks = (result or {}).get("b"), (result or {}).get("a")
    elif venue == "okx":
        rows = (payload or {}).get("data") if isinstance(payload, Mapping) else None
        row = rows[0] if isinstance(rows, list) and rows else {}
        bids, asks = row.get("bids"), row.get("asks")
    elif venue == "gate":
        bids = payload.get("bids") if isinstance(payload, Mapping) else None
        asks = payload.get("asks") if isinstance(payload, Mapping) else None
    else:
        raise ReplayError(f"no book layout known for venue: {venue}")

    if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
        return None
    best_bid, best_ask = _as_pair(bids[0]), _as_pair(asks[0])
    if best_bid is None or best_ask is None:
        return None
    if best_bid[0] <= 0 or best_ask[0] <= 0 or best_ask[0] < best_bid[0]:
        # A crossed or nonsensical book is not a price; dropping it silently would let
        # it set a bound.
        return None
    return best_bid, best_ask


@dataclass(frozen=True)
class BookSample:
    source_row_index: int
    request_ts: float
    received_ts: float
    exchange_ts: float
    bid_px: float
    bid_sz: float
    ask_px: float
    ask_sz: float


def book_series(samples: Iterable[Mapping[str, Any]], venue: str) -> list[BookSample]:
    """Return structurally usable books ordered by when the response was received."""
    series: list[BookSample] = []
    max_staleness = float(config.MAX_SAMPLE_STALENESS_SEC["orderbook"])
    max_future_skew = float(config.MAX_EXCHANGE_FUTURE_SKEW_SEC)
    for source_row_index, sample in enumerate(samples):
        if sample.get("probe") != "orderbook" or sample.get("error"):
            continue
        try:
            request_ts = float(sample["request_ts"])
            received_ts = float(sample["received_ts"])
            exchange_ts = float(sample["exchange_ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (
            request_ts, received_ts, exchange_ts
        )):
            continue
        if received_ts < request_ts:
            continue
        exchange_age_sec = received_ts - exchange_ts
        if exchange_age_sec > max_staleness + 1e-9:
            continue
        if exchange_ts - received_ts > max_future_skew + 1e-9:
            continue
        book = top_of_book(venue, sample.get("payload"))
        if book is None:
            continue
        (bid_px, bid_sz), (ask_px, ask_sz) = book
        series.append(
            BookSample(
                source_row_index=source_row_index,
                request_ts=request_ts,
                received_ts=received_ts,
                exchange_ts=exchange_ts,
                bid_px=bid_px,
                bid_sz=bid_sz,
                ask_px=ask_px,
                ask_sz=ask_sz,
            )
        )
    series.sort(key=lambda item: (item.received_ts, item.source_row_index))
    return series


@dataclass(frozen=True)
class CausalBookObservation:
    status: str
    target_ts: float
    side: str
    max_lag_sec: float
    source_row_index: int | None = None
    price: float | None = None
    visible_size: float | None = None
    request_ts: float | None = None
    received_ts: float | None = None
    exchange_ts: float | None = None
    selection_lag_sec: float | None = None
    exchange_age_sec: float | None = None

    @property
    def observed(self) -> bool:
        return self.status == "OBSERVED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "observed": self.observed,
            "target_ts": self.target_ts,
            "side": self.side,
            "max_lag_sec": self.max_lag_sec,
            "source_row_index": self.source_row_index,
            "price": self.price,
            "visible_size": self.visible_size,
            "request_ts": self.request_ts,
            "received_ts": self.received_ts,
            "exchange_ts": self.exchange_ts,
            "selection_lag_sec": self.selection_lag_sec,
            "exchange_age_sec": self.exchange_age_sec,
        }


def first_causal_book(
    series: Sequence[BookSample],
    *,
    target_ts: float,
    side: str,
    max_lag_sec: float,
) -> CausalBookObservation:
    """Select the first valid book received at/after target within one cadence."""
    if side not in {"bid", "ask"}:
        raise ReplayError(f"unknown book side: {side}")
    if not math.isfinite(float(target_ts)):
        raise ReplayError("target_ts must be finite")
    if not math.isfinite(float(max_lag_sec)) or float(max_lag_sec) < 0:
        raise ReplayError("max_lag_sec must be finite and non-negative")

    candidate = min(
        (item for item in series if item.received_ts >= float(target_ts)),
        key=lambda item: (item.received_ts, item.source_row_index),
        default=None,
    )
    if candidate is None:
        return CausalBookObservation(
            status="NO_SAMPLE_AT_OR_AFTER_TARGET",
            target_ts=float(target_ts),
            side=side,
            max_lag_sec=float(max_lag_sec),
        )

    lag = candidate.received_ts - float(target_ts)
    if lag > float(max_lag_sec) + 1e-9:
        return CausalBookObservation(
            status="SAMPLE_AFTER_CADENCE",
            target_ts=float(target_ts),
            side=side,
            max_lag_sec=float(max_lag_sec),
            source_row_index=candidate.source_row_index,
            request_ts=candidate.request_ts,
            received_ts=candidate.received_ts,
            exchange_ts=candidate.exchange_ts,
            selection_lag_sec=round(lag, 6),
            exchange_age_sec=round(candidate.received_ts - candidate.exchange_ts, 6),
        )

    price = candidate.bid_px if side == "bid" else candidate.ask_px
    visible_size = candidate.bid_sz if side == "bid" else candidate.ask_sz
    return CausalBookObservation(
        status="OBSERVED",
        target_ts=float(target_ts),
        side=side,
        max_lag_sec=float(max_lag_sec),
        source_row_index=candidate.source_row_index,
        price=price,
        visible_size=visible_size,
        request_ts=candidate.request_ts,
        received_ts=candidate.received_ts,
        exchange_ts=candidate.exchange_ts,
        selection_lag_sec=round(lag, 6),
        exchange_age_sec=round(candidate.received_ts - candidate.exchange_ts, 6),
    )


def gross_bbo_return(
    entry: CausalBookObservation, exit_: CausalBookObservation
) -> dict[str, Any]:
    """Gross bid/ask markout using only two causally observed point prices."""
    if not entry.observed or not exit_.observed:
        return {"value": None, "computable": False}
    if entry.price is None or entry.price <= 0 or exit_.price is None:
        return {"value": None, "computable": False}
    return {
        "value": round(exit_.price / entry.price - 1.0, 6),
        "computable": True,
    }


def _orderbook_cadence(offset_sec: float) -> float:
    if abs(float(offset_sec)) <= float(config.BURST_HALF_WIDTH_SEC):
        return float(config.BURST_CADENCE_SEC["orderbook"])
    return float(config.PROBE_CADENCE_SEC["orderbook"])


# ----------------------------------------------------------------------------- replay


def replay_capture(
    capture_dir: Path,
    *,
    horizons_sec: Sequence[int] = DEFAULT_HORIZONS_SEC,
    entry_lead_sec: int = DEFAULT_ENTRY_LEAD_SEC,
    evidence_mode: str = PRODUCTION_EVIDENCE_MODE,
) -> dict[str, Any]:
    if isinstance(entry_lead_sec, bool) or not isinstance(entry_lead_sec, int):
        raise ReplayError("entry lead must be a positive integer number of seconds")
    if entry_lead_sec <= 0:
        raise ReplayError("entry lead must be positive")
    requested_horizons = tuple(horizons_sec)
    if not requested_horizons:
        raise ReplayError("replay horizons must not be empty")
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in requested_horizons
    ):
        raise ReplayError("replay horizons must be integer seconds")
    if any(value < 0 for value in requested_horizons):
        raise ReplayError("replay horizons must be non-negative")
    if len(set(requested_horizons)) != len(requested_horizons):
        raise ReplayError("replay horizons must not contain duplicates")
    if evidence_mode == PRODUCTION_EVIDENCE_MODE:
        if requested_horizons != tuple(config.PRIMARY_EXIT_OFFSETS_SEC):
            raise ReplayError(
                "production replay requires the exact preregistered exit horizons"
            )
        if entry_lead_sec != config.PRIMARY_ENTRY_LEAD_SEC:
            raise ReplayError(
                "production replay requires the exact preregistered entry lead"
            )
    evidence = load_replay_evidence(capture_dir, evidence_mode=evidence_mode)
    manifest = evidence.manifest
    samples = evidence.samples
    venue = str(manifest.get("venue") or "")
    t0_ts = float(manifest.get("t0_ts") or 0)
    if not venue or not t0_ts:
        raise ReplayError("manifest carries no venue or t0")

    series = book_series(samples, venue)
    entry_offset = -float(entry_lead_sec)
    entry = first_causal_book(
        series,
        target_ts=t0_ts + entry_offset,
        side="ask",
        max_lag_sec=_orderbook_cadence(entry_offset),
    )

    horizon_results: list[dict[str, Any]] = []
    for offset in requested_horizons:
        at_ts = t0_ts + offset
        exit_observation = first_causal_book(
            series,
            target_ts=at_ts,
            side="bid",
            max_lag_sec=_orderbook_cadence(float(offset)),
        )
        horizon_results.append({
            "offset_sec": offset,
            "exit_observation": exit_observation.as_dict(),
            "gross_bbo_return": gross_bbo_return(entry, exit_observation),
        })

    observed_offsets = [
        int(horizon["offset_sec"])
        for horizon in horizon_results
        if horizon["exit_observation"]["observed"]
    ]
    missing_offsets = [
        int(horizon["offset_sec"])
        for horizon in horizon_results
        if not horizon["exit_observation"]["observed"]
    ]
    sealed_capture_ready = bool(
        isinstance(manifest.get("replay_readiness"), Mapping)
        and manifest["replay_readiness"].get("ready") is True
    )
    causal_ready = sealed_capture_ready and entry.observed and not missing_offsets
    return {
        "schema": REPLAY_SCHEMA,
        "capture_id": manifest.get("capture_id"),
        "venue": venue,
        "symbol": manifest.get("symbol"),
        "t0_ts": int(t0_ts),
        "t0_source_class": manifest.get("t0_source_class"),
        "book_samples_used": len(series),
        "entry": {
            "target_offset_sec": -entry_lead_sec,
            "observation": entry.as_dict(),
        },
        "horizons": horizon_results,
        "horizons_observed": len(observed_offsets),
        "horizons_requested": len(requested_horizons),
        "causal_replay_readiness": {
            "ready": causal_ready,
            "sealed_capture_ready": sealed_capture_ready,
            "entry_observed": entry.observed,
            "observed_exit_offsets_sec": observed_offsets,
            "missing_exit_offsets_sec": missing_offsets,
        },
        "method": (
            "first valid top-of-book response received at or after each target within "
            "one declared orderbook cadence; entry uses ask and exit uses bid; gross "
            "BBO markout only"
        ),
        "research_classification": "DESCRIPTIVE_ONLY",
        "evidence_verification": {
            "mode": evidence.evidence_mode,
            "production_verified": evidence.production_verified,
            "nonacceptance_only": True,
            "receipt_hash": (
                evidence.receipt.get("receipt_hash") if evidence.receipt else None
            ),
        },
        "capture_replay_readiness": manifest.get("replay_readiness"),
    }


def format_report(report: Mapping[str, Any]) -> str:
    lines = [
        f"replay {report['capture_id']}  {report['venue']} {report['symbol']}",
        f"  book samples used: {report['book_samples_used']}",
    ]
    entry = report["entry"]["observation"]
    if entry["observed"]:
        lines.append(
            f"  entry ask at received_ts={entry['received_ts']}: {entry['price']} "
            f"(lag {entry['selection_lag_sec']}s)"
        )
    else:
        lines.append(f"  entry: not observed - {entry['status']}")
    for horizon in report["horizons"]:
        markout = horizon["gross_bbo_return"]
        observation = horizon["exit_observation"]
        if markout["computable"]:
            lines.append(
                f"  t0+{horizon['offset_sec']:>3}s  gross BBO return "
                f"{markout['value']:+.4%} at received_ts={observation['received_ts']}"
            )
        else:
            lines.append(
                f"  t0+{horizon['offset_sec']:>3}s  not computable - "
                f"{observation['status']}"
            )
    lines.append("  descriptive top-of-book observations only")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay the exit hypothesis over a capture already on disk.",
    )
    parser.add_argument("--replay", metavar="CAPTURE_DIR", default="")
    parser.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS_SEC))
    parser.add_argument("--entry-lead-sec", type=int, default=DEFAULT_ENTRY_LEAD_SEC)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--list", action="store_true",
                        help="list captures available to replay")
    args = parser.parse_args(argv)

    if args.list:
        root = config.CAPTURE_ROOT
        captures = sorted(p.name for p in root.glob("*/") if (p / "manifest.json").is_file()) \
            if root.is_dir() else []
        print(json.dumps({"capture_root": str(root), "captures": captures},
                         ensure_ascii=False))
        return 0

    if not args.replay:
        raise SystemExit("no action requested")
    horizons = tuple(int(part) for part in args.horizons.split(",") if part.strip())
    report = replay_capture(
        Path(args.replay), horizons_sec=horizons, entry_lead_sec=args.entry_lead_sec
    )
    print(json.dumps(report, ensure_ascii=False) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
