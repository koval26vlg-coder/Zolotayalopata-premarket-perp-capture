"""Contract for a non-authorizing event-bound v37 plan proposal."""

from __future__ import annotations

import importlib
import importlib.util
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from canonical_hash import canonical_hash  # noqa: E402
import event_registry as registry  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402


def _load_module():
    spec = importlib.util.find_spec("event_bound_plan_proposal")
    if spec is None:
        raise AssertionError(
            "src/event_bound_plan_proposal.py is required by the active no-capture plan"
        )
    return importlib.import_module("event_bound_plan_proposal")


def arming_record() -> dict[str, object]:
    anchor = {
        "episode_id": "episode-abc",
        "venue": "bybit",
        "listing_venue": "kucoin",
        "premarket_contract_id": "ABCUSDT",
        "spot_symbol": "ABC-USDT",
        "official_spot_t0": 2_000_007_200,
        "t0_source_class": registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
        "t0_precision_sec": 1,
        "official_record_hash": "1" * 64,
        "official_source_url": "https://www.kucoin.com/announcement/en-abc-gets-listed",
        "official_source_identity": "human_attestation:koval",
        "registry_sha256": "2" * 64,
        "registry_tail_record_hash": "3" * 64,
        "mutation_receipt_seq": 7,
        "mutation_receipt_hash": "4" * 64,
        "summary_content_sha256": "5" * 64,
        "registry_authority_state_hash": "6" * 64,
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
        "asset_class": registry.ASSET_CLASS_CRYPTO_TOKEN,
        "issuer_namespace": "crypto_asset",
        "issuer_id": "ABC",
        "asset_identity_hash": "7" * 64,
    }
    record = {
        "schema": "premarket_official_t0_arming_receipt_v1",
        "record_type": "official_t0_arming_receipt",
        "arming_id": "arming-" + hashlib.sha256(b"episode-abc").hexdigest()[:32],
        "revision": 0,
        "supersedes_arming_receipt_hash": None,
        "status": "ARMED_NO_CAPTURE_AUTHORITY",
        "run_id": "arm-run-1",
        "armed_at_utc": "2033-05-18T03:33:20Z",
        "armed_by": "koval",
        "lead_sec_at_arming": 7200,
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
        "resolved_paths_hash": "8" * 64,
        "event_anchor": anchor,
        "capture_authorized": False,
        "capture_token_issued": False,
        "event_bound_plan_generated": False,
    }
    record["receipt_hash"] = canonical_hash(record)
    return record


def rehash(record: dict[str, object]) -> dict[str, object]:
    updated = dict(record)
    updated.pop("receipt_hash", None)
    updated["receipt_hash"] = canonical_hash(updated)
    return updated


def persist_arming(record: dict[str, object], root: Path) -> Path:
    stream = root / str(record["arming_id"])
    stream.mkdir(parents=True, exist_ok=True)
    path = stream / (
        f"{int(record['revision']):020d}-{record['receipt_hash']}.json"
    )
    path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    return path


class EventBoundProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_proposal_is_fixed_to_v37_but_cannot_activate_or_capture(self) -> None:
        proposal = self.module.build_event_bound_plan_proposal(
            arming_record(), generated_at_utc="2033-05-18T03:34:00Z"
        )

        self.assertEqual(proposal["schema"], "premarket_perp_event_bound_plan_proposal_v1")
        self.assertEqual(proposal["proposed_plan_schema"], "premarket_perp_capture_planonly_v37")
        self.assertEqual(proposal["proposed_plan_id"], "premarket_perp_capture_20260822_v37")
        self.assertEqual(
            proposal["proposed_plan_path"],
            "docs/plans/premarket-perp-capture-planonly-20260822-v37.json",
        )
        self.assertEqual(proposal["supersedes_plan_id"], trust_root.PLAN_ID)
        self.assertEqual(proposal["supersedes_plan_hash"], trust_root.PLAN_HASH)
        self.assertEqual(proposal["event_anchor"]["official_record_hash"], "1" * 64)
        self.assertIs(proposal["capture_authorized"], False)
        self.assertIs(proposal["trust_root_rebound"], False)
        self.assertIs(proposal["requires_explicit_user_capture_approval"], True)
        self.assertEqual(
            proposal["proposal_hash"],
            canonical_hash({k: v for k, v in proposal.items() if k != "proposal_hash"}),
        )

    def test_proxy_or_unarmed_input_is_rejected(self) -> None:
        bad = arming_record()
        bad["event_anchor"] = dict(bad["event_anchor"], t0_source_class="VENUE_INSTRUMENT_METADATA")
        bad = rehash(bad)
        with self.assertRaisesRegex(self.module.ProposalError, "OFFICIAL_ANNOUNCEMENT"):
            self.module.build_event_bound_plan_proposal(
                bad, generated_at_utc="2033-05-18T03:34:00Z"
            )

        bad = rehash(dict(arming_record(), status="PROPOSED"))
        with self.assertRaisesRegex(self.module.ProposalError, "armed"):
            self.module.build_event_bound_plan_proposal(
                bad, generated_at_utc="2033-05-18T03:34:00Z"
            )

    def test_write_is_create_only_and_never_overwrites_a_proposal(self) -> None:
        root = Path(tempfile.mkdtemp()) / "proposals"
        arming_root = Path(tempfile.mkdtemp()) / "arming"
        record = arming_record()
        persist_arming(record, arming_root)

        def allow(**kwargs: object) -> dict[str, object]:
            return {
                "schema": "premarket_write_preflight_v2",
                "ok": True,
                "verified": True,
                "decision": "ALLOW_EVENT_BOUND_PLAN_PROPOSAL",
                "write_class": "event_bound_plan_proposal",
                "run_id": kwargs["run_id"],
                "action": "write one deterministic event-bound plan proposal from an arming receipt",
                "plan_id": trust_root.PLAN_ID,
                "plan_hash": trust_root.PLAN_HASH,
                "resolved_paths_hash": "9" * 64,
            }

        with mock.patch.object(self.module.config, "EVENT_BOUND_PLAN_PROPOSAL_ROOT", root), \
             mock.patch.object(self.module.config, "OFFICIAL_T0_ARMING_ROOT", arming_root):
            first = self.module.write_event_bound_plan_proposal(
                record, run_id="proposal-run", preflight=allow,
                clock=lambda: 2_000_000_040,
            )
            with self.assertRaisesRegex(self.module.ProposalError, "already exists"):
                self.module.write_event_bound_plan_proposal(
                    record, run_id="proposal-run", preflight=allow,
                    clock=lambda: 2_000_000_040,
                )

        self.assertTrue(first.is_relative_to(root))
        payload = json.loads(first.read_text(encoding="utf-8"))
        self.assertIs(payload["capture_authorized"], False)
        self.assertEqual(payload["proposal_write_authority"]["run_id"], "proposal-run")

    def test_writer_rejects_bad_hash_and_blocked_preflight_without_creating_root(self) -> None:
        root = Path(tempfile.mkdtemp()) / "proposals"
        bad = dict(arming_record(), receipt_hash="0" * 64)
        with mock.patch.object(self.module.config, "EVENT_BOUND_PLAN_PROPOSAL_ROOT", root):
            with self.assertRaisesRegex(self.module.ProposalError, "receipt hash"):
                self.module.write_event_bound_plan_proposal(
                    bad, run_id="proposal-run",
                    preflight=lambda **_: {"ok": False}, clock=lambda: 2_000_000_040,
                )
            self.assertFalse(root.exists())

            with self.assertRaisesRegex(self.module.ProposalError, "preflight"):
                self.module.write_event_bound_plan_proposal(
                    arming_record(), run_id="proposal-run",
                    preflight=lambda **_: {"ok": False}, clock=lambda: 2_000_000_040,
                )
            self.assertFalse(root.exists())

    def test_writer_uses_fresh_clock_and_has_no_arbitrary_output_path(self) -> None:
        import inspect

        self.assertNotIn(
            "output_path",
            inspect.signature(self.module.write_event_bound_plan_proposal).parameters,
        )
        root = Path(tempfile.mkdtemp()) / "proposals"
        with mock.patch.object(self.module.config, "EVENT_BOUND_PLAN_PROPOSAL_ROOT", root):
            with self.assertRaisesRegex(self.module.ProposalError, "pre-listing capture window"):
                self.module.write_event_bound_plan_proposal(
                    arming_record(), run_id="proposal-run",
                    preflight=lambda **kwargs: {
                        "schema": "premarket_write_preflight_v2", "ok": True,
                        "verified": True, "decision": "ALLOW_EVENT_BOUND_PLAN_PROPOSAL",
                        "write_class": "event_bound_plan_proposal", "run_id": kwargs["run_id"],
                        "action": "write one deterministic event-bound plan proposal from an arming receipt",
                        "plan_id": trust_root.PLAN_ID, "plan_hash": trust_root.PLAN_HASH,
                        "resolved_paths_hash": "9" * 64,
                    },
                    clock=lambda: 2_000_005_401,
                )
            self.assertFalse(root.exists())

    def test_writer_rechecks_clock_after_commit_preflight(self) -> None:
        root = Path(tempfile.mkdtemp()) / "proposals"
        arming_root = Path(tempfile.mkdtemp()) / "arming"
        record = arming_record()
        persist_arming(record, arming_root)
        preflight_calls = 0

        def allow(**kwargs: object) -> dict[str, object]:
            nonlocal preflight_calls
            preflight_calls += 1
            return {
                "schema": "premarket_write_preflight_v2",
                "ok": True,
                "verified": True,
                "decision": "ALLOW_EVENT_BOUND_PLAN_PROPOSAL",
                "write_class": "event_bound_plan_proposal",
                "run_id": kwargs["run_id"],
                "action": "write one deterministic event-bound plan proposal from an arming receipt",
                "plan_id": trust_root.PLAN_ID,
                "plan_hash": trust_root.PLAN_HASH,
                "resolved_paths_hash": "9" * 64,
            }

        def boundary_clock() -> int:
            capture_start = 2_000_007_200 - 1800
            return capture_start if preflight_calls < 2 else capture_start + 1

        with mock.patch.object(self.module.config, "EVENT_BOUND_PLAN_PROPOSAL_ROOT", root), \
             mock.patch.object(self.module.config, "OFFICIAL_T0_ARMING_ROOT", arming_root):
            with self.assertRaisesRegex(self.module.ProposalError, "pre-listing capture window"):
                self.module.write_event_bound_plan_proposal(
                    record,
                    run_id="proposal-run",
                    preflight=allow,
                    clock=boundary_clock,
                )

        self.assertEqual(preflight_calls, 2)
        self.assertFalse(root.exists())

    def test_writer_refuses_a_valid_but_superseded_arming_receipt(self) -> None:
        root = Path(tempfile.mkdtemp()) / "proposals"
        arming_root = Path(tempfile.mkdtemp()) / "arming"
        first = arming_record()
        persist_arming(first, arming_root)
        second = dict(first)
        second["revision"] = 1
        second["supersedes_arming_receipt_hash"] = first["receipt_hash"]
        second["event_anchor"] = dict(
            first["event_anchor"],
            official_spot_t0=2_000_007_800,
            official_record_hash="a" * 64,
        )
        second["lead_sec_at_arming"] = 7800
        second = rehash(second)
        persist_arming(second, arming_root)

        def allow(**kwargs: object) -> dict[str, object]:
            return {
                "schema": "premarket_write_preflight_v2",
                "ok": True,
                "verified": True,
                "decision": "ALLOW_EVENT_BOUND_PLAN_PROPOSAL",
                "write_class": "event_bound_plan_proposal",
                "run_id": kwargs["run_id"],
                "action": "write one deterministic event-bound plan proposal from an arming receipt",
                "plan_id": trust_root.PLAN_ID,
                "plan_hash": trust_root.PLAN_HASH,
                "resolved_paths_hash": "9" * 64,
            }

        with mock.patch.object(self.module.config, "EVENT_BOUND_PLAN_PROPOSAL_ROOT", root), \
             mock.patch.object(self.module.config, "OFFICIAL_T0_ARMING_ROOT", arming_root):
            with self.assertRaisesRegex(self.module.ProposalError, "current arming head"):
                self.module.write_event_bound_plan_proposal(
                    first,
                    run_id="proposal-run",
                    preflight=allow,
                    clock=lambda: 2_000_000_040,
                )

        self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
