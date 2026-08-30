"""Adversarial RED contract for the future create-only v43 event binding.

The builder remains a pure, non-authoritative review helper: these tests do not
grant capture authority, mint a token, contact a venue, or write durable state.
They define the trust-boundary checks that must exist before its output can be
considered for a separately issued immutable v43 PlanOnly.
"""

from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from canonical_hash import canonical_hash  # noqa: E402
import event_bound_plan_proposal as proposal_builder  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import risk_gate  # noqa: E402
import v43_event_binding as binding  # noqa: E402


GENERATED_AT_UTC = "2033-05-18T03:33:20Z"
OFFICIAL_SPOT_T0 = 2_000_007_200


def _hashed(payload: dict[str, Any], field: str) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    result.pop(field, None)
    result[field] = canonical_hash(result)
    return result


def _event_anchor() -> dict[str, Any]:
    return {
        "episode_id": "ep-abc",
        "venue": "bybit",
        "listing_venue": "bybit",
        "premarket_contract_id": "ABCUSDT",
        "spot_symbol": "ABCUSDT",
        "official_spot_t0": OFFICIAL_SPOT_T0,
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


def _arming_receipt() -> dict[str, Any]:
    anchor = _event_anchor()
    arming_id = "arming-" + hashlib.sha256(b"ep-abc").hexdigest()[:32]
    receipt = {
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
    return _hashed(receipt, "receipt_hash")


def _proposal() -> dict[str, Any]:
    result = proposal_builder.build_event_bound_plan_proposal(
        _arming_receipt(), generated_at_utc="2033-05-18T03:31:00Z"
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


def _resign_proposal(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result["event_binding_hash"] = canonical_hash(
        {
            "arming_receipt_hash": result["arming_receipt"]["receipt_hash"],
            "event_anchor": result["event_anchor"],
        }
    )
    return _hashed(result, "proposal_hash")


def _lifecycle(proposal: dict[str, Any]) -> dict[str, Any]:
    result = {
        "schema": "premarket_perp_lifecycle_snapshot_v43_candidate",
        "received_at_utc": "2033-05-18T03:33:00Z",
        "proposal_hash": proposal["proposal_hash"],
        "event_binding_hash": proposal["event_binding_hash"],
        "venue": "bybit",
        "premarket_contract_id": "ABCUSDT",
        "spot_symbol": "ABCUSDT",
        "official_spot_t0": OFFICIAL_SPOT_T0,
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


def _approval(
    proposal: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    result = {
        "schema": "premarket_perp_capture_approval_v43_candidate",
        "approval_id": "approval-abc-0001",
        "approval_nonce": "e" * 64,
        "approval_mode": "EXPLICIT_USER_VISIBLE_ONE_SHOT_V43_REVIEW",
        "approved_by": "koval",
        "approved_at_utc": "2033-05-18T03:33:10Z",
        "expires_at_utc": "2033-05-18T03:34:10Z",
        "proposal_hash": proposal["proposal_hash"],
        "event_binding_hash": proposal["event_binding_hash"],
        "lifecycle_snapshot_hash": snapshot["snapshot_hash"],
        "approved_capture_attempts": 1,
        "approval_consumed": False,
        "public_data_only": True,
        "orders_allowed": False,
        "capture_token_issued": False,
    }
    return _hashed(result, "approval_hash")


def _resign_snapshot(
    value: dict[str, Any], proposal: dict[str, Any]
) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result["proposal_hash"] = proposal["proposal_hash"]
    result["event_binding_hash"] = proposal["event_binding_hash"]
    return _hashed(result, "snapshot_hash")


def _resign_approval(
    value: dict[str, Any],
    proposal: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result["proposal_hash"] = proposal["proposal_hash"]
    result["event_binding_hash"] = proposal["event_binding_hash"]
    result["lifecycle_snapshot_hash"] = snapshot["snapshot_hash"]
    return _hashed(result, "approval_hash")


class V43EventBindingSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proposal = _proposal()
        self.snapshot = _lifecycle(self.proposal)
        self.approval = _approval(self.proposal, self.snapshot)

        # Every adversarial test starts from material that the current create-only
        # builder accepts, and that still carries no capture authority.
        baseline = self._build(self.proposal, self.snapshot, self.approval)
        self.assertIs(baseline["capture_authorized"], False)
        self.assertIs(baseline["capture_token_issued"], False)
        self.assertIs(baseline["requires_trust_root_rebind"], True)

    @staticmethod
    def _build(
        proposal: dict[str, Any],
        snapshot: dict[str, Any],
        approval: dict[str, Any],
    ) -> dict[str, Any]:
        return binding.build_event_binding_candidate(
            proposal,
            snapshot,
            approval,
            generated_at_utc=GENERATED_AT_UTC,
        )

    def _snapshot_attack(
        self, mutate: Callable[[dict[str, Any]], None]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        snapshot = copy.deepcopy(self.snapshot)
        mutate(snapshot)
        snapshot = _resign_snapshot(snapshot, self.proposal)
        return snapshot, _approval(self.proposal, snapshot)

    def _proposal_attack(
        self, mutate: Callable[[dict[str, Any]], None]
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        proposal = copy.deepcopy(self.proposal)
        mutate(proposal)
        proposal = _resign_proposal(proposal)
        snapshot = _lifecycle(proposal)
        return proposal, snapshot, _approval(proposal, snapshot)

    def test_arbitrary_or_local_websocket_hosts_are_rejected(self) -> None:
        for host in ("attacker.example", "localhost", "127.0.0.1", "[::1]"):
            with self.subTest(host=host):
                snapshot, approval = self._snapshot_attack(
                    lambda value, host=host: value["ws_binding"].__setitem__(
                        "host", host
                    )
                )
                with self.assertRaises(binding.EventBindingError):
                    self._build(self.proposal, snapshot, approval)

    def test_websocket_path_query_is_rejected(self) -> None:
        snapshot, approval = self._snapshot_attack(
            lambda value: value["ws_binding"].__setitem__(
                "path", "/v5/public/linear?token=attacker-controlled"
            )
        )
        with self.assertRaises(binding.EventBindingError):
            self._build(self.proposal, snapshot, approval)

    def test_websocket_headers_are_rejected(self) -> None:
        snapshot, approval = self._snapshot_attack(
            lambda value: value["ws_binding"].__setitem__(
                "headers", {"Authorization": "attacker-controlled"}
            )
        )
        with self.assertRaises(binding.EventBindingError):
            self._build(self.proposal, snapshot, approval)

    def test_unrelated_or_incomplete_websocket_topics_are_rejected(self) -> None:
        attacks = (
            ["orderbook.50.BTCUSDT", "publicTrade.BTCUSDT"],
            ["orderbook.50.ABCUSDT"],
        )
        for channels in attacks:
            with self.subTest(channels=channels):
                snapshot, approval = self._snapshot_attack(
                    lambda value, channels=channels: value["ws_binding"].__setitem__(
                        "channels", channels
                    )
                )
                with self.assertRaises(binding.EventBindingError):
                    self._build(self.proposal, snapshot, approval)

    def test_self_rehashed_forged_active_plan_is_rejected(self) -> None:
        def forge(value: dict[str, Any]) -> None:
            value["supersedes_plan_id"] = "premarket_perp_capture_20260822_v999"
            value["supersedes_plan_hash"] = "e" * 64
            value["supersedes_plan_file_sha256"] = "f" * 64
            value["event_anchor"]["plan_id"] = value["supersedes_plan_id"]
            value["event_anchor"]["plan_hash"] = value["supersedes_plan_hash"]
            value["arming_receipt"]["plan_id"] = value["supersedes_plan_id"]
            value["arming_receipt"]["plan_hash"] = value["supersedes_plan_hash"]
            value["proposal_write_authority"]["plan_id"] = value[
                "supersedes_plan_id"
            ]
            value["proposal_write_authority"]["plan_hash"] = value[
                "supersedes_plan_hash"
            ]

        proposal, snapshot, approval = self._proposal_attack(forge)
        with self.assertRaises(binding.EventBindingError):
            self._build(proposal, snapshot, approval)

    def test_arming_summary_must_match_the_active_plan(self) -> None:
        proposal, snapshot, approval = self._proposal_attack(
            lambda value: value["arming_receipt"].__setitem__(
                "plan_hash", "f" * 64
            )
        )
        with self.assertRaises(binding.EventBindingError):
            self._build(proposal, snapshot, approval)

    def test_source_url_must_be_official_https_for_listing_venue(self) -> None:
        for source_url in (
            "file:///attacker/fake-announcement",
            "https://attacker.example/fake-announcement",
            "https://announcements.bybit.com:8443/en/article/abc",
        ):
            with self.subTest(source_url=source_url):
                proposal, snapshot, approval = self._proposal_attack(
                    lambda value, source_url=source_url: value[
                        "event_anchor"
                    ].__setitem__("official_source_url", source_url)
                )
                with self.assertRaises(binding.EventBindingError):
                    self._build(proposal, snapshot, approval)

    def test_source_url_path_must_be_canonical(self) -> None:
        for source_url in (
            "https://announcements.bybit.com/en/article/../../other",
            "https://announcements.bybit.com/en/article/%2e%2e/other",
            "https://announcements.bybit.com/en/article/abc\\@attacker.example",
        ):
            with self.subTest(source_url=source_url):
                proposal, snapshot, approval = self._proposal_attack(
                    lambda value, source_url=source_url: value[
                        "event_anchor"
                    ].__setitem__("official_source_url", source_url)
                )
                with self.assertRaises(binding.EventBindingError):
                    self._build(proposal, snapshot, approval)

    def test_capture_window_drift_is_rejected(self) -> None:
        attacks: tuple[tuple[str, Callable[[dict[str, Any]], None]], ...] = (
            (
                "declared_before",
                lambda value: value["capture_bounds"].__setitem__(
                    "window_before_sec", 1_799
                ),
            ),
            (
                "actual_before",
                lambda value: value["capture_bounds"].__setitem__(
                    "capture_start_ts", OFFICIAL_SPOT_T0 - 601
                ),
            ),
            (
                "actual_after",
                lambda value: value["capture_bounds"].__setitem__(
                    "capture_end_ts", OFFICIAL_SPOT_T0 + 1
                ),
            ),
        )
        for name, mutate in attacks:
            with self.subTest(attack=name):
                proposal, snapshot, approval = self._proposal_attack(mutate)
                with self.assertRaises(binding.EventBindingError):
                    self._build(proposal, snapshot, approval)

    def test_capture_caps_or_grace_drift_is_rejected(self) -> None:
        attacks = (
            ("max_runtime_sec", 99_999),
            ("max_requests", 20_001),
            ("max_events", 999),
            ("launch_early_grace_sec", 31),
            ("launch_late_grace_sec", 6),
        )
        for field, replacement in attacks:
            with self.subTest(field=field):
                proposal, snapshot, approval = self._proposal_attack(
                    lambda value, field=field, replacement=replacement: value[
                        "capture_bounds"
                    ].__setitem__(field, replacement)
                )
                with self.assertRaises(binding.EventBindingError):
                    self._build(proposal, snapshot, approval)

    def test_approval_cannot_predate_the_fresh_lifecycle_snapshot(self) -> None:
        approval = copy.deepcopy(self.approval)
        approval["approved_at_utc"] = "2000-01-01T00:00:00Z"
        approval = _resign_approval(approval, self.proposal, self.snapshot)
        with self.assertRaises(binding.EventBindingError):
            self._build(self.proposal, self.snapshot, approval)

    def test_explicitly_replayed_approval_is_rejected(self) -> None:
        first = self._build(self.proposal, self.snapshot, self.approval)
        approval = copy.deepcopy(self.approval)
        approval["replay_of_binding_hash"] = first["binding_hash"]
        approval = _resign_approval(approval, self.proposal, self.snapshot)
        with self.assertRaises(binding.EventBindingError):
            self._build(self.proposal, self.snapshot, approval)

    def test_unknown_fields_are_rejected_at_every_trust_boundary(self) -> None:
        proposal, snapshot, approval = self._proposal_attack(
            lambda value: value.__setitem__("debug_override", True)
        )
        with self.subTest(boundary="proposal"):
            with self.assertRaises(binding.EventBindingError):
                self._build(proposal, snapshot, approval)

        snapshot, approval = self._snapshot_attack(
            lambda value: value.__setitem__("debug_override", True)
        )
        with self.subTest(boundary="lifecycle"):
            with self.assertRaises(binding.EventBindingError):
                self._build(self.proposal, snapshot, approval)

        approval = copy.deepcopy(self.approval)
        approval["debug_override"] = True
        approval = _resign_approval(approval, self.proposal, self.snapshot)
        with self.subTest(boundary="approval"):
            with self.assertRaises(binding.EventBindingError):
                self._build(self.proposal, self.snapshot, approval)

    def test_incoherent_contract_spec_is_rejected(self) -> None:
        attacks: tuple[tuple[str, Callable[[dict[str, Any]], None]], ...] = (
            (
                "min_above_max",
                lambda value: value["contract_spec"].update(
                    {"min_qty": 2_000_000.0, "max_qty": 1_000_000.0}
                ),
            ),
            (
                "non_integral_funding_interval",
                lambda value: value["contract_spec"].__setitem__(
                    "funding_interval_sec", 3_600.5
                ),
            ),
            (
                "maintenance_margin_at_or_above_one",
                lambda value: value["contract_spec"].__setitem__(
                    "maintenance_margin_rate", 1.0
                ),
            ),
            (
                "quantity_bounds_not_step_aligned",
                lambda value: value["contract_spec"].update(
                    {"qty_step": 3.0, "min_qty": 1.0, "max_qty": 10.0}
                ),
            ),
        )
        for name, mutate in attacks:
            with self.subTest(attack=name):
                snapshot, approval = self._snapshot_attack(mutate)
                with self.assertRaises(binding.EventBindingError):
                    self._build(self.proposal, snapshot, approval)

    def test_contract_numeric_strings_are_rejected(self) -> None:
        for field in (
            "contract_size",
            "tick_size",
            "qty_step",
            "min_qty",
            "max_qty",
            "maintenance_margin_rate",
        ):
            with self.subTest(field=field):
                snapshot, approval = self._snapshot_attack(
                    lambda value, field=field: value["contract_spec"].__setitem__(
                        field, str(value["contract_spec"][field])
                    )
                )
                with self.assertRaises(binding.EventBindingError):
                    self._build(self.proposal, snapshot, approval)

    def test_multi_connection_venues_remain_fail_closed_in_single_binding_schema(self) -> None:
        for venue in ("okx", "gate"):
            with self.subTest(venue=venue):
                proposal, snapshot, approval = self._proposal_attack(
                    lambda value, venue=venue: value["event_anchor"].__setitem__(
                        "venue", venue
                    )
                )
                with self.assertRaises(binding.EventBindingError):
                    self._build(proposal, snapshot, approval)


if __name__ == "__main__":
    unittest.main()
