"""Run one deterministic, offline rehearsal without production writes or authority.

The rehearsal deliberately stops at an event-bound *proposal*.  It uses temporary
candidate, arming and proposal surfaces, an in-memory alert preview and synthetic
preflight receipts.  It cannot contact a venue, show a toast, mint a capture token,
change the trust root or place any kind of order.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import announcement_candidate_store as candidate_store
import announcement_discovery as discovery
import candidate_alert
from canonical_hash import canonical_hash
import event_bound_plan_proposal as proposal_writer
import event_registry as registry
import frozen_plan_bindings as trust_root
import official_attestation
import official_t0_arming as arming
import risk_gate


SCHEMA = "premarket_fixture_rehearsal_v1"
STATUS = "FIXTURE_REHEARSAL_COMPLETE_NO_CAPTURE"
FIXTURE_CONTRACT = "ABCUSDT"
FIXTURE_SPOT = "ABC-USDT"
FIXTURE_VENUE = "bybit"
FIXTURE_SOURCE_URL = "https://announcements.bybit.com/en/article/abc-usdt-spot-listing-fixture"  # risk-scan: allow https://announcements.bybit.com/en/article/abc-usdt-spot-listing-fixture


class FixtureRehearsalError(RuntimeError):
    """The temporary no-authority rehearsal violated its fixed contract."""


def _utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _preflight(*, write_class: str, run_id: str) -> dict[str, Any]:
    decisions = {
        "candidate_alert": (
            "ALLOW_CANDIDATE_ALERT",
            candidate_alert.ALERT_ACTION,
        ),
        "official_t0_arming": (
            "ALLOW_OFFICIAL_T0_ARMING",
            arming.ARMING_ACTION,
        ),
        "event_bound_plan_proposal": (
            "ALLOW_EVENT_BOUND_PLAN_PROPOSAL",
            "write one deterministic event-bound plan proposal from an arming receipt",
        ),
    }
    try:
        decision, action = decisions[write_class]
    except KeyError as exc:
        raise FixtureRehearsalError(
            f"fixture preflight does not permit {write_class}"
        ) from exc
    return {
        "schema": "premarket_write_preflight_v2",
        "ok": True,
        "verified": True,
        "decision": decision,
        "write_class": write_class,
        "run_id": run_id,
        "action": action,
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
        "resolved_paths_hash": canonical_hash(
            {"fixture_only": True, "write_class": write_class}
        ),
    }


def _target(*, episode_id: str, now_ts: int) -> dict[str, Any]:
    return {
        "episode_id": episode_id,
        "perpetual_venue": FIXTURE_VENUE,
        "premarket_contract_id": FIXTURE_CONTRACT,
        "lifecycle_generation": 0,
        "asset_class": registry.ASSET_CLASS_CRYPTO_TOKEN,
        "issuer_namespace": "crypto_asset",
        "issuer_id": "ABC",
        "asset_identity_hash": canonical_hash(
            {"asset_class": "CRYPTO_TOKEN", "issuer_id": "ABC"}
        ),
        "registry_sha256": canonical_hash({"fixture": "registry"}),
        "registry_tail_record_hash": canonical_hash({"fixture": "tail"}),
        "mutation_receipt_seq": 0,
        "mutation_receipt_hash": canonical_hash({"fixture": "mutation"}),
        "summary_content_hash": canonical_hash({"fixture": "summary"}),
        "registry_authority_state_hash": canonical_hash({"fixture": "authority"}),
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
        "metadata_refresh_received_at": _utc(now_ts),
    }


def _selector(target: Mapping[str, Any]):
    def select(**_: Any) -> dict[str, Any]:
        return {
            "status": "TARGETS_READY",
            "targets": [dict(target)],
            "capture_authorized": False,
        }

    return select


class _FixtureNotifier:
    """In-memory emulation of the sidecar contract; it never invokes Windows toast."""

    def __init__(self) -> None:
        self.calls = 0

    def preflight(self, _payload: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema": candidate_alert.ALERT_PREFLIGHT_SCHEMA,
            "status": "READY",
            "show_invoked": False,
        }

    def __call__(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.calls += 1
        # This is a temporary contract emulation, not an assertion about Windows
        # history.  The outer rehearsal receipt explicitly reports toast_invoked=false.
        return {
            "schema": candidate_alert.ALERT_RESULT_SCHEMA,
            "status": "WINDOWS_HISTORY_CONFIRMED",
            "notification_id": payload["notification_id"],
            "show_invoked": True,
            "tag": payload["tag"],
            "group": payload["group"],
        }


def _official_event(
    *, observation: Mapping[str, Any], target: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "episode_id": observation["episode_id"],
        "venue": observation["venue"],
        "listing_venue": observation["listing_venue"],
        "premarket_contract_id": observation["premarket_contract_id"],
        "spot_symbol": observation["spot_symbol"],
        "official_spot_t0": observation["official_spot_t0"],
        "t0_source_class": observation["t0_source_class"],
        "t0_precision_sec": observation["t0_precision_sec"],
        "official_record_hash": canonical_hash(observation),
        "official_source_url": observation["source_url"],
        "official_source_identity": observation["source_identity"],
        "registry_sha256": target["registry_sha256"],
        "registry_tail_record_hash": target["registry_tail_record_hash"],
        "mutation_receipt_seq": target["mutation_receipt_seq"],
        "mutation_receipt_hash": target["mutation_receipt_hash"],
        "summary_content_sha256": target["summary_content_hash"],
        "registry_authority_state_hash": target["registry_authority_state_hash"],
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
        "asset_class": observation["asset_class"],
        "issuer_namespace": observation["issuer_namespace"],
        "issuer_id": observation["issuer_id"],
        "asset_identity_hash": observation["asset_identity_hash"],
    }


def run_fixture_rehearsal(*, now_ts: int) -> dict[str, Any]:
    """Exercise candidate -> attestation -> arming -> proposal in a temp directory."""
    if isinstance(now_ts, bool) or not isinstance(now_ts, int) or now_ts <= 0:
        raise FixtureRehearsalError("now_ts must be a positive integer")
    risk_gate.load_and_verify_plan()
    capability = risk_gate.run_capability_scan()
    if capability.get("status") != "CAPABILITY_SCAN_CLEAN":
        raise FixtureRehearsalError("fixture capability preflight did not pass")
    temporary_path: Path | None = None
    semantic: dict[str, Any]
    with tempfile.TemporaryDirectory(prefix="zlp-premarket-rehearsal-") as raw_root:
        temporary_path = Path(raw_root)
        candidates_path = temporary_path / "candidates.jsonl"
        alerts_path = temporary_path / "alerts.jsonl"
        arming_root = temporary_path / "arming"
        proposal_root = temporary_path / "proposals"
        episode_id = registry.make_episode_id(FIXTURE_VENUE, FIXTURE_CONTRACT, 0)
        target = _target(episode_id=episode_id, now_ts=now_ts)

        raw_candidate = discovery.make_candidate(
            target=target,
            listing_venue=FIXTURE_VENUE,
            article={
                "article_id": "abc-fixture-listing",
                "title": "ABC (ABC) Will Be Listed on Bybit",
                "url": FIXTURE_SOURCE_URL,
                "published_at_ms": None,
                "source_page": 1,
                "source_payload_sha256": canonical_hash({"fixture": "article"}),
            },
            detected_at_utc=_utc(now_ts),
        )
        candidate_store.append_candidates(
            candidates_path, [raw_candidate], run_id="fixture-candidate"
        )
        review = candidate_alert.inspect_candidate_review_queue(
            now_ts=now_ts,
            candidate_store_path=candidates_path,
            alert_ledger_path=alerts_path,
            target_selector=_selector(target),
        )
        if review.get("status") != "CANDIDATES_READY_FOR_HUMAN_REVIEW":
            raise FixtureRehearsalError("fixture candidate did not reach human review")
        stored_candidate = candidate_store.load_verified_candidate_records(
            candidates_path
        )[0]
        alert_preview = candidate_alert._notification_payload(stored_candidate)
        if (
            alert_preview.get("capture_authorized") is not False
            or "official_spot_t0" in alert_preview
            or alerts_path.exists()
        ):
            raise FixtureRehearsalError("fixture alert preview crossed its authority")
        fixture_notifier = _FixtureNotifier()
        alert_result = candidate_alert.process_candidate_alerts(
            now_ts=now_ts,
            run_id="fixture-alert",
            candidate_store_path=candidates_path,
            alert_ledger_path=alerts_path,
            target_selector=_selector(target),
            preflight=_preflight,
            notifier=fixture_notifier,
        )
        if alert_result.get("status") != "CANDIDATE_ALERTS_HISTORY_CONFIRMED":
            raise FixtureRehearsalError("fixture alert contract did not complete")
        alert_ledger_records = len(
            alerts_path.read_text(encoding="utf-8").splitlines()
        )

        t0 = now_ts + 7200
        announced = _utc(t0)
        quoted_time = datetime.fromtimestamp(t0, timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        quote = f"Spot trading for {FIXTURE_SPOT} will start at {quoted_time}."
        identity = registry.AssetIdentity(
            asset_class=registry.ASSET_CLASS_CRYPTO_TOKEN,
            issuer_namespace="crypto_asset",
            issuer_id="ABC",
            evidence_class=registry.IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
        )
        observation = official_attestation.build_attestation(
            venue=FIXTURE_VENUE,
            listing_venue=FIXTURE_VENUE,
            spot_symbol=FIXTURE_SPOT,
            premarket_contract_id=FIXTURE_CONTRACT,
            lifecycle_generation=0,
            announced_utc=announced,
            announcement_url=FIXTURE_SOURCE_URL,
            quoted_sentence=quote,
            quoted_time_text=quoted_time,
            quoted_symbol_text=FIXTURE_SPOT,
            attested_by="fixture-operator",
            now_ts=now_ts,
            asset_identity=identity,
        )
        selected_event = _official_event(observation=observation, target=target)
        armed = arming.arm_official_t0(
            now_ts=now_ts,
            run_id="fixture-arm",
            episode_id=episode_id,
            expected_official_record_hash=selected_event["official_record_hash"],
            expected_official_t0=t0,
            expected_contract=FIXTURE_CONTRACT,
            expected_spot_symbol=FIXTURE_SPOT,
            armed_by="fixture-operator",
            acknowledge_no_capture_authority=True,
            arming_root=arming_root,
            event_selector=lambda **_: [selected_event],
            preflight=_preflight,
            clock=lambda: now_ts,
        )
        receipt = arming.load_arming_receipt(armed["receipt_path"])
        proposal_path = proposal_writer._write_event_bound_plan_proposal_to_roots(
            receipt,
            run_id="fixture-proposal",
            proposal_root=proposal_root,
            arming_root=arming_root,
            preflight=_preflight,
            clock=lambda: now_ts + 1,
        )
        proposed = json.loads(proposal_path.read_text(encoding="utf-8"))
        if proposed["arming_receipt"]["receipt_hash"] != armed["receipt_hash"]:
            raise FixtureRehearsalError("proposal is not bound to the arming receipt")
        if any(
            value is not False
            for value in (
                proposed["capture_authorized"],
                proposed["capture_token_issued"],
                proposed["trust_root_rebound"],
            )
        ):
            raise FixtureRehearsalError("fixture proposal gained forbidden authority")

        semantic = {
            "plan_id": trust_root.PLAN_ID,
            "plan_hash": trust_root.PLAN_HASH,
            "candidate_id": stored_candidate["candidate_id"],
            "alert_ledger_head_hash": alert_result["alert_ledger_head_hash"],
            "episode_id": episode_id,
            "official_record_hash": selected_event["official_record_hash"],
            "arming_receipt_hash": armed["receipt_hash"],
            "proposal_hash": proposed["proposal_hash"],
            "event_binding_hash": proposed["event_binding_hash"],
        }

    removed = temporary_path is not None and not temporary_path.exists()
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": STATUS,
        "stages": {
            "candidate_alert": "FIXTURE_ALERT_PREVIEW_VALIDATED_NO_TOAST",
            "official_attestation": "FIXTURE_ATTESTATION_VALIDATED_NO_WRITE",
            "official_t0_arming": "ARMED_NO_CAPTURE_AUTHORITY",
            "event_bound_proposal": "FIXTURE_PROPOSAL_VALIDATED_NO_AUTHORITY",
        },
        "lineage": semantic,
        "network_used": False,
        "toast_invoked": False,
        "production_writes": False,
        "temporary_workspace_removed": removed,
        "capture_authorized": False,
        "capture_token_issued": False,
        "orders_allowed": False,
        "trust_root_rebound": False,
        "simulated_notifier_calls": fixture_notifier.calls,
        "temporary_artifact_counts": {
            "alert_ledger_records": alert_ledger_records,
        },
    }
    result["rehearsal_hash"] = canonical_hash(result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one offline temporary pre-market arming rehearsal."
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--now-ts", type=int, default=2_000_000_000)
    args = parser.parse_args(argv)
    result = run_fixture_rehearsal(now_ts=args.now_ts)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(result["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
