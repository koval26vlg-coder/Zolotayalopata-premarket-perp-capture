"""Create-only event binding contract for the future immutable v43 PlanOnly."""

from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from canonical_hash import canonical_hash  # noqa: E402
import event_bound_plan_proposal as proposal_builder  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import risk_gate  # noqa: E402
import v43_event_binding as binding  # noqa: E402


def _hashed(payload: dict[str, object], field: str) -> dict[str, object]:
    result = copy.deepcopy(payload)
    result[field] = canonical_hash(result)
    return result


def proposal() -> dict[str, object]:
    anchor = {
        "episode_id": "ep-abc",
        "venue": "bybit",
        "listing_venue": "bybit",
        "premarket_contract_id": "ABCUSDT",
        "spot_symbol": "ABCUSDT",
        "official_spot_t0": 2_000_007_200,
        "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
        "t0_precision_sec": 1,
        "official_record_hash": "1" * 64,
        "official_source_url": "https://announcements.bybit.com/en/article/abc",
        "official_source_identity": "human_attestation:koval",
        "registry_sha256": "2" * 64,
        "registry_tail_record_hash": "3" * 64,
        "mutation_receipt_seq": 7,
        "mutation_receipt_hash": "4" * 64,
        "summary_content_sha256": "5" * 64,
        "registry_authority_state_hash": "6" * 64,
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
        "asset_class": "CRYPTO_TOKEN",
        "issuer_namespace": "crypto_asset",
        "issuer_id": "ABC",
        "asset_identity_hash": "8" * 64,
    }
    arming_id = "arming-" + hashlib.sha256(b"ep-abc").hexdigest()[:32]
    arming: dict[str, object] = {
        "schema": "premarket_official_t0_arming_receipt_v1",
        "record_type": "official_t0_arming_receipt",
        "arming_id": arming_id,
        "revision": 0,
        "supersedes_arming_receipt_hash": None,
        "status": "ARMED_NO_CAPTURE_AUTHORITY",
        "run_id": "arm-run-1",
        "armed_at_utc": "2033-05-18T03:30:00Z",
        "armed_by": "koval",
        "lead_sec_at_arming": 7_400,
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
        "resolved_paths_hash": canonical_hash(risk_gate.resolved_path_bindings()),
        "event_anchor": anchor,
        "capture_authorized": False,
        "capture_token_issued": False,
        "event_bound_plan_generated": False,
    }
    arming["receipt_hash"] = canonical_hash(arming)
    result = proposal_builder.build_event_bound_plan_proposal(
        arming, generated_at_utc="2033-05-18T03:31:00Z"
    )
    result.pop("proposal_hash")
    result["proposal_write_authority"] = {
        "run_id": "proposal-run-1",
        "decision": "ALLOW_EVENT_BOUND_PLAN_PROPOSAL",
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
        "resolved_paths_hash": canonical_hash(risk_gate.resolved_path_bindings()),
    }
    result["proposal_hash"] = canonical_hash(result)
    return result


def lifecycle(candidate: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": "premarket_perp_lifecycle_snapshot_v43_candidate",
        "received_at_utc": "2033-05-18T03:33:00Z",
        "proposal_hash": candidate["proposal_hash"],
        "event_binding_hash": candidate["event_binding_hash"],
        "venue": "bybit",
        "premarket_contract_id": "ABCUSDT",
        "spot_symbol": "ABCUSDT",
        "official_spot_t0": 2_000_007_200,
        "phase": "continuous",
        "tradable": True,
        "terminal": False,
        "transition_state": "spot_listing_pending",
        "raw_payload_sha256": "b" * 64,
        "request_identity_sha256": "c" * 64,
        "exchange_ts": 1_999_999_979,
        "received_ts": 1_999_999_980,
        "ws_binding": {
            "scheme": "wss",
            "host": "stream.bybit.com",
            "port": 443,
            "path": "/v5/public/linear",
            "channels": [
                "orderbook.50.ABCUSDT",
                "publicTrade.ABCUSDT",
                "tickers.ABCUSDT",
                "priceLimit.ABCUSDT",
            ],
        },
        "contract_spec": {
            "contract_size": 1.0,
            "tick_size": 0.0001,
            "qty_step": 1.0,
            "min_qty": 1.0,
            "max_qty": 1_000_000.0,
            "taker_fee_rate": 0.00055,
            "funding_interval_sec": 28_800,
            "maintenance_margin_rate": 0.01,
            "price_limit_model": "OBSERVED_PUBLIC_PRICE_LIMIT_CHANNEL",
            "source_sha256": "d" * 64,
        },
    }
    return _hashed(result, "snapshot_hash")


def approval(candidate: dict[str, object], snapshot: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": "premarket_perp_capture_approval_v43_candidate",
        "approval_id": "approval-abc-0001",
        "approval_nonce": "e" * 64,
        "approval_mode": "EXPLICIT_USER_VISIBLE_ONE_SHOT_V43_REVIEW",
        "approved_by": "koval",
        "approved_at_utc": "2033-05-18T03:33:10Z",
        "expires_at_utc": "2033-05-18T03:34:10Z",
        "proposal_hash": candidate["proposal_hash"],
        "event_binding_hash": candidate["event_binding_hash"],
        "lifecycle_snapshot_hash": snapshot["snapshot_hash"],
        "approved_capture_attempts": 1,
        "approval_consumed": False,
        "public_data_only": True,
        "orders_allowed": False,
        "capture_token_issued": False,
    }
    return _hashed(result, "approval_hash")


class V43EventBindingTests(unittest.TestCase):
    def test_missing_event_material_never_issues_placeholder_v43(self) -> None:
        result = binding.issue_readiness(
            proposal=None, lifecycle_snapshot=None, approval_receipt=None
        )
        self.assertEqual(result["status"], "NOT_ISSUED_EVENT_REQUIRED")
        self.assertEqual(
            result["missing"],
            ["approval_receipt", "lifecycle_snapshot", "proposal"],
        )
        self.assertIs(result["capture_authorized"], False)

    def test_valid_material_builds_one_event_create_only_binding(self) -> None:
        candidate = proposal()
        snapshot = lifecycle(candidate)
        permit = approval(candidate, snapshot)

        result = binding.build_event_binding_candidate(
            candidate,
            snapshot,
            permit,
            generated_at_utc="2033-05-18T03:33:20Z",
        )

        self.assertEqual(result["schema"], "premarket_perp_v43_event_binding_candidate_v1")
        self.assertEqual(result["episode_id"], "ep-abc")
        self.assertEqual(result["premarket_contract_id"], "ABCUSDT")
        self.assertEqual(result["official_spot_t0"], 2_000_007_200)
        self.assertEqual(result["approved_capture_attempts"], 1)
        self.assertEqual(result["capture_start_ts"], 2_000_005_400)
        self.assertEqual(result["capture_end_ts"], 2_000_008_100)
        self.assertIs(result["capture_authorized"], False)
        self.assertIs(result["capture_token_issued"], False)
        self.assertIs(result["requires_trust_root_rebind"], True)
        self.assertIs(result["external_authority_verified"], False)
        self.assertIs(result["issuable"], False)
        self.assertIs(result["one_writer_required"], True)
        self.assertIs(result["one_writer_verified"], False)
        self.assertIs(result["one_attempt_requested"], True)
        self.assertIs(result["one_attempt_consumed"], False)
        self.assertNotIn("one_writer", result)
        self.assertNotIn("one_attempt", result)
        self.assertEqual(
            result["binding_hash"],
            canonical_hash({k: v for k, v in result.items() if k != "binding_hash"}),
        )

    def test_proxy_terminal_stale_or_mismatched_material_is_rejected(self) -> None:
        candidate = proposal()
        snapshot = lifecycle(candidate)
        permit = approval(candidate, snapshot)

        proxy = copy.deepcopy(candidate)
        proxy["event_anchor"]["t0_source_class"] = "VENUE_INSTRUMENT_METADATA"
        proxy.pop("proposal_hash")
        proxy["proposal_hash"] = canonical_hash(proxy)
        with self.assertRaisesRegex(binding.EventBindingError, "OFFICIAL_ANNOUNCEMENT"):
            binding.build_event_binding_candidate(
                proxy, snapshot, permit, generated_at_utc="2033-05-18T03:33:20Z"
            )

        terminal = copy.deepcopy(snapshot)
        terminal["terminal"] = True
        terminal.pop("snapshot_hash")
        terminal["snapshot_hash"] = canonical_hash(terminal)
        with self.assertRaisesRegex(binding.EventBindingError, "terminal"):
            binding.build_event_binding_candidate(
                candidate,
                terminal,
                approval(candidate, terminal),
                generated_at_utc="2033-05-18T03:33:20Z",
            )

        with self.assertRaisesRegex(binding.EventBindingError, "stale"):
            binding.build_event_binding_candidate(
                candidate,
                snapshot,
                permit,
                generated_at_utc="2033-05-18T03:35:00Z",
            )

        mismatch = copy.deepcopy(snapshot)
        mismatch["premarket_contract_id"] = "OTHERUSDT"
        mismatch.pop("snapshot_hash")
        mismatch["snapshot_hash"] = canonical_hash(mismatch)
        with self.assertRaisesRegex(binding.EventBindingError, "contract"):
            binding.build_event_binding_candidate(
                candidate,
                mismatch,
                approval(candidate, mismatch),
                generated_at_utc="2033-05-18T03:33:20Z",
            )

    def test_late_activation_or_missing_cost_risk_inputs_is_rejected(self) -> None:
        candidate = proposal()
        snapshot = lifecycle(candidate)
        permit = approval(candidate, snapshot)
        with self.assertRaisesRegex(binding.EventBindingError, "activation lead"):
            binding.build_event_binding_candidate(
                candidate,
                snapshot,
                permit,
                generated_at_utc="2033-05-18T05:28:30Z",
            )

        incomplete = copy.deepcopy(snapshot)
        del incomplete["contract_spec"]["maintenance_margin_rate"]
        incomplete.pop("snapshot_hash")
        incomplete["snapshot_hash"] = canonical_hash(incomplete)
        with self.assertRaisesRegex(binding.EventBindingError, "maintenance_margin_rate"):
            binding.build_event_binding_candidate(
                candidate,
                incomplete,
                approval(candidate, incomplete),
                generated_at_utc="2033-05-18T03:33:20Z",
            )

    def test_approval_is_exact_single_attempt_and_never_self_authorizes(self) -> None:
        candidate = proposal()
        snapshot = lifecycle(candidate)
        permit = approval(candidate, snapshot)
        permit["approved_capture_attempts"] = 2
        permit.pop("approval_hash")
        permit["approval_hash"] = canonical_hash(permit)
        with self.assertRaisesRegex(binding.EventBindingError, "exactly one"):
            binding.build_event_binding_candidate(
                candidate,
                snapshot,
                permit,
                generated_at_utc="2033-05-18T03:33:20Z",
            )

        permit = approval(candidate, snapshot)
        permit["capture_token_issued"] = True
        permit.pop("approval_hash")
        permit["approval_hash"] = canonical_hash(permit)
        with self.assertRaisesRegex(binding.EventBindingError, "token"):
            binding.build_event_binding_candidate(
                candidate,
                snapshot,
                permit,
                generated_at_utc="2033-05-18T03:33:20Z",
            )


if __name__ == "__main__":
    unittest.main()
