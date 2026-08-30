"""RED contract for a sealed official-t0 arming receipt with no capture authority."""

from __future__ import annotations

import importlib
import importlib.util
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import event_registry as registry  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
from canonical_hash import canonical_hash, canonical_json_bytes  # noqa: E402


def _load_arming_module():
    spec = importlib.util.find_spec("official_t0_arming")
    if spec is None:
        raise AssertionError("src/official_t0_arming.py is required by PlanOnly v35")
    return importlib.import_module("official_t0_arming")


def event(*, now_ts: int = 2_000_000_000) -> dict[str, object]:
    return {
        "episode_id": "episode-abc",
        "venue": "bybit",
        "listing_venue": "kucoin",
        "premarket_contract_id": "ABCUSDT",
        "spot_symbol": "ABC-USDT",
        "official_spot_t0": now_ts + 7200,
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


class OfficialT0ArmingTests(unittest.TestCase):
    NOW = 2_000_000_000

    def setUp(self) -> None:
        self.arming = _load_arming_module()
        self.root = Path(tempfile.mkdtemp()) / "official-t0-v1"

    @staticmethod
    def allow_preflight(**kwargs: object) -> dict[str, object]:
        return {
            "schema": "premarket_write_preflight_v2",
            "ok": True,
            "verified": True,
            "decision": "ALLOW_OFFICIAL_T0_ARMING",
            "write_class": "official_t0_arming",
            "run_id": kwargs["run_id"],
            "action": (
                "seal one human-attested exact seconds-grade official spot t0 "
                "as immutable no-capture arming evidence"
            ),
            "plan_id": trust_root.PLAN_ID,
            "plan_hash": trust_root.PLAN_HASH,
            "resolved_paths_hash": "8" * 64,
        }

    def invoke(
        self,
        selected: list[dict[str, object]],
        *,
        expected_current_arming_receipt_hash: str | None = None,
    ) -> dict[str, object]:
        return self.arming.arm_official_t0(
            now_ts=self.NOW,
            run_id="arm-run-1",
            episode_id="episode-abc",
            expected_official_record_hash="1" * 64,
            expected_official_t0=self.NOW + 7200,
            expected_contract="ABCUSDT",
            expected_spot_symbol="ABC-USDT",
            expected_current_arming_receipt_hash=(
                expected_current_arming_receipt_hash
            ),
            armed_by="koval",
            acknowledge_no_capture_authority=True,
            arming_root=self.root,
            event_selector=lambda **_: selected,
            preflight=self.allow_preflight,
        )

    def test_seconds_grade_official_event_is_sealed_without_capture_authority(self) -> None:
        result = self.invoke([event(now_ts=self.NOW)])
        receipts = sorted(self.root.rglob("*.json"))
        self.assertEqual(len(receipts), 1)
        record = self.arming.load_arming_receipt(receipts[0])

        self.assertEqual(result["status"], "ARMED_NO_CAPTURE_AUTHORITY")
        self.assertIs(result["capture_authorized"], False)
        self.assertIs(result["capture_token_issued"], False)
        self.assertIs(result["event_bound_plan_generated"], False)
        self.assertEqual(record["event_anchor"]["official_record_hash"], "1" * 64)
        self.assertEqual(record["event_anchor"]["official_spot_t0"], self.NOW + 7200)
        self.assertIs(record["capture_authorized"], False)
        self.assertIs(record["capture_token_issued"], False)
        self.assertNotIn("capture_token", record)

    def test_duplicate_arming_is_idempotent_and_append_only(self) -> None:
        first = self.invoke([event(now_ts=self.NOW)])
        second = self.invoke([event(now_ts=self.NOW)])

        self.assertEqual(first["status"], "ARMED_NO_CAPTURE_AUTHORITY")
        self.assertEqual(second["status"], "ALREADY_ARMED_NO_CAPTURE_AUTHORITY")
        self.assertEqual(len(list(self.root.rglob("*.json"))), 1)

    def test_proxy_or_wrong_official_record_never_arms(self) -> None:
        proxy = dict(event(now_ts=self.NOW), t0_source_class="VENUE_INSTRUMENT_METADATA")
        with self.assertRaisesRegex(self.arming.ArmingError, "OFFICIAL_ANNOUNCEMENT"):
            self.invoke([proxy])
        self.assertFalse(self.root.exists())

        with self.assertRaisesRegex(self.arming.ArmingError, "official record"):
            self.invoke([dict(event(now_ts=self.NOW), official_record_hash="a" * 64)])
        self.assertFalse(self.root.exists())

    def test_explicit_no_capture_acknowledgement_is_required(self) -> None:
        with self.assertRaisesRegex(self.arming.ArmingError, "acknowledge"):
            self.arming.arm_official_t0(
                now_ts=self.NOW,
                run_id="arm-run-1",
                episode_id="episode-abc",
                expected_official_record_hash="1" * 64,
                expected_official_t0=self.NOW + 7200,
                expected_contract="ABCUSDT",
                expected_spot_symbol="ABC-USDT",
                armed_by="koval",
                acknowledge_no_capture_authority=False,
                arming_root=self.root,
                event_selector=lambda **_: [event(now_ts=self.NOW)],
                preflight=self.allow_preflight,
            )
        self.assertFalse(self.root.exists())

    def test_t0_too_close_for_the_full_prelisting_window_is_refused(self) -> None:
        too_close = dict(event(now_ts=self.NOW), official_spot_t0=self.NOW + 60)
        with self.assertRaisesRegex(self.arming.ArmingError, "full pre-listing window"):
            self.invoke([too_close])
        self.assertFalse(self.root.exists())

    def test_fresh_commit_clock_cannot_backdate_the_full_window(self) -> None:
        boundary = dict(event(now_ts=self.NOW), official_spot_t0=self.NOW + 1800)
        with self.assertRaisesRegex(self.arming.ArmingError, "full pre-listing window"):
            self.arming.arm_official_t0(
                now_ts=self.NOW,
                run_id="arm-run-1",
                episode_id="episode-abc",
                expected_official_record_hash="1" * 64,
                expected_official_t0=self.NOW + 1800,
                expected_contract="ABCUSDT",
                expected_spot_symbol="ABC-USDT",
                armed_by="koval",
                acknowledge_no_capture_authority=True,
                arming_root=self.root,
                event_selector=lambda **_: [boundary],
                preflight=self.allow_preflight,
                clock=lambda: self.NOW + 1,
            )
        self.assertEqual(list(self.root.rglob("*.json")), [])

    def test_final_write_clock_rechecks_window_after_commit_preflight(self) -> None:
        boundary = dict(event(now_ts=self.NOW), official_spot_t0=self.NOW + 1800)
        preflight_calls = 0

        def allow_then_cross(**kwargs: object) -> dict[str, object]:
            nonlocal preflight_calls
            preflight_calls += 1
            return self.allow_preflight(**kwargs)

        def boundary_clock() -> int:
            return self.NOW if preflight_calls < 2 else self.NOW + 1

        with self.assertRaisesRegex(self.arming.ArmingError, "full pre-listing window"):
            self.arming.arm_official_t0(
                now_ts=self.NOW,
                run_id="arm-run-1",
                episode_id="episode-abc",
                expected_official_record_hash="1" * 64,
                expected_official_t0=self.NOW + 1800,
                expected_contract="ABCUSDT",
                expected_spot_symbol="ABC-USDT",
                armed_by="koval",
                acknowledge_no_capture_authority=True,
                arming_root=self.root,
                event_selector=lambda **_: [boundary],
                preflight=allow_then_cross,
                clock=boundary_clock,
            )

        self.assertEqual(preflight_calls, 2)
        self.assertEqual(list(self.root.rglob("*.json")), [])

    def test_authoritative_event_is_reselected_after_commit_preflight(self) -> None:
        selector_calls = 0

        def changing_selector(**_: object) -> list[dict[str, object]]:
            nonlocal selector_calls
            selector_calls += 1
            selected = event(now_ts=self.NOW)
            if selector_calls == 3:
                selected = dict(selected, registry_sha256="a" * 64)
            return [selected]

        with self.assertRaisesRegex(self.arming.ArmingError, "authority changed"):
            self.arming.arm_official_t0(
                now_ts=self.NOW,
                run_id="arm-run-1",
                episode_id="episode-abc",
                expected_official_record_hash="1" * 64,
                expected_official_t0=self.NOW + 7200,
                expected_contract="ABCUSDT",
                expected_spot_symbol="ABC-USDT",
                armed_by="koval",
                acknowledge_no_capture_authority=True,
                arming_root=self.root,
                event_selector=changing_selector,
                preflight=self.allow_preflight,
                clock=lambda: self.NOW,
            )

        self.assertEqual(selector_calls, 3)
        self.assertEqual(list(self.root.rglob("*.json")), [])

    def test_production_final_selector_receives_the_held_registry_lock_owner(self) -> None:
        owner = object()
        selector = mock.Mock(return_value=[event(now_ts=self.NOW)])
        registry_guard = mock.Mock(return_value=nullcontext(owner))

        with mock.patch.object(
            self.arming.registry, "events_for_arming", selector
        ), mock.patch.object(
            self.arming.registry, "registry_lock", registry_guard
        ):
            result = self.arming.arm_official_t0(
                now_ts=self.NOW,
                run_id="arm-run-1",
                episode_id="episode-abc",
                expected_official_record_hash="1" * 64,
                expected_official_t0=self.NOW + 7200,
                expected_contract="ABCUSDT",
                expected_spot_symbol="ABC-USDT",
                armed_by="koval",
                acknowledge_no_capture_authority=True,
                arming_root=self.root,
                preflight=self.allow_preflight,
                clock=lambda: self.NOW,
            )

        self.assertEqual(result["status"], "ARMED_NO_CAPTURE_AUTHORITY")
        self.assertEqual(selector.call_count, 3)
        self.assertNotIn("_registry_lock_owner", selector.call_args_list[0].kwargs)
        self.assertNotIn("_registry_lock_owner", selector.call_args_list[1].kwargs)
        self.assertIs(
            selector.call_args_list[2].kwargs["_registry_lock_owner"], owner
        )

    def test_preflight_is_rechecked_inside_lock_before_receipt_write(self) -> None:
        calls = 0

        def drifting_preflight(**kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            receipt = dict(self.allow_preflight(**kwargs))
            if calls == 2:
                receipt["resolved_paths_hash"] = "9" * 64
            return receipt

        with self.assertRaisesRegex(self.arming.ArmingError, "authority changed"):
            self.arming.arm_official_t0(
                now_ts=self.NOW,
                run_id="arm-run-1",
                episode_id="episode-abc",
                expected_official_record_hash="1" * 64,
                expected_official_t0=self.NOW + 7200,
                expected_contract="ABCUSDT",
                expected_spot_symbol="ABC-USDT",
                armed_by="koval",
                acknowledge_no_capture_authority=True,
                arming_root=self.root,
                event_selector=lambda **_: [event(now_ts=self.NOW)],
                preflight=drifting_preflight,
                clock=lambda: self.NOW,
            )
        self.assertEqual(calls, 2)
        self.assertEqual(list(self.root.rglob("*.json")), [])

    def test_registry_lineage_change_creates_a_new_arming_revision(self) -> None:
        first = self.invoke([event(now_ts=self.NOW)])
        revised = dict(
            event(now_ts=self.NOW),
            registry_sha256="a" * 64,
            registry_tail_record_hash="b" * 64,
            mutation_receipt_seq=8,
            mutation_receipt_hash="c" * 64,
            summary_content_sha256="d" * 64,
            registry_authority_state_hash="e" * 64,
        )
        second = self.invoke(
            [revised],
            expected_current_arming_receipt_hash=str(first["receipt_hash"]),
        )

        self.assertEqual(first["revision"], 0)
        self.assertEqual(second["status"], "ARMED_NO_CAPTURE_AUTHORITY")
        self.assertEqual(second["revision"], 1)
        self.assertEqual(len(list(self.root.rglob("*.json"))), 2)

    def test_revision_requires_compare_and_swap_against_current_receipt(self) -> None:
        first = self.invoke([event(now_ts=self.NOW)])
        revised = dict(event(now_ts=self.NOW), registry_sha256="a" * 64)

        with self.assertRaisesRegex(self.arming.ArmingError, "current arming receipt"):
            self.invoke([revised])
        with self.assertRaisesRegex(self.arming.ArmingError, "current arming receipt"):
            self.invoke(
                [revised],
                expected_current_arming_receipt_hash="0" * 64,
            )

        current = self.arming.load_current_arming(
            "episode-abc", arming_root=self.root
        )
        self.assertEqual(current["receipt_hash"], first["receipt_hash"])
        self.assertEqual(len(list(self.root.rglob("*.json"))), 1)

    def test_historical_retired_plan_receipt_remains_readable(self) -> None:
        self.invoke([event(now_ts=self.NOW)])
        current_path = next(self.root.rglob("*.json"))
        record = json.loads(current_path.read_text(encoding="utf-8"))
        retired = trust_root.RETIRED_PLANS[-1]
        record["plan_id"] = retired["plan_id"]
        record["plan_hash"] = retired["plan_hash"]
        record["event_anchor"]["plan_id"] = retired["plan_id"]
        record["event_anchor"]["plan_hash"] = retired["plan_hash"]
        record.pop("receipt_hash")
        record["receipt_hash"] = canonical_hash(record)
        historical = Path(tempfile.mkdtemp()) / "historical.json"
        historical.write_text(json.dumps(record) + "\n", encoding="utf-8")

        loaded = self.arming.load_arming_receipt(historical)
        self.assertEqual(loaded["plan_id"], retired["plan_id"])

    def test_receipt_and_anchor_plan_identities_must_match(self) -> None:
        self.invoke([event(now_ts=self.NOW)])
        current_path = next(self.root.rglob("*.json"))
        record = json.loads(current_path.read_text(encoding="utf-8"))
        retired = trust_root.RETIRED_PLANS[-1]
        record["plan_id"] = retired["plan_id"]
        record["plan_hash"] = retired["plan_hash"]
        record.pop("receipt_hash")
        record["receipt_hash"] = canonical_hash(record)

        with self.assertRaisesRegex(self.arming.ArmingError, "identity differ"):
            self.arming.validate_arming_receipt(record)

    def test_receipt_intrinsically_binds_arming_id_and_recorded_lead(self) -> None:
        self.invoke([event(now_ts=self.NOW)])
        current_path = next(self.root.rglob("*.json"))
        original = json.loads(current_path.read_text(encoding="utf-8"))

        bad_id = dict(original, arming_id="arming-abc")
        bad_id.pop("receipt_hash")
        bad_id["receipt_hash"] = canonical_hash(bad_id)
        with self.assertRaisesRegex(self.arming.ArmingError, "arming_id"):
            self.arming.validate_arming_receipt(bad_id)

        bad_lead = dict(original, lead_sec_at_arming=7201)
        bad_lead.pop("receipt_hash")
        bad_lead["receipt_hash"] = canonical_hash(bad_lead)
        with self.assertRaisesRegex(self.arming.ArmingError, "lead_sec_at_arming"):
            self.arming.validate_arming_receipt(bad_lead)

    def test_dead_same_host_lock_is_archived_once_and_reacquired(self) -> None:
        self.root.mkdir(parents=True)
        lock_path = self.root / ".official-t0-arming.lock"
        stale = {
            "schema": "premarket_official_t0_arming_lock_v1",
            "run_id": "crashed-arm-run",
            "owner_pid": 424242,
            "owner_host": self.arming.socket.gethostname(),
            "nonce": "a" * 64,
        }
        lock_path.write_bytes(canonical_json_bytes(stale) + b"\n")

        with mock.patch.object(
            self.arming, "process_is_alive", return_value=False, create=True
        ):
            result = self.invoke([event(now_ts=self.NOW)])

        archive_root = self.root.parent / f"{self.root.name}.lock-archive"
        archived = sorted(archive_root.glob("*.json"))
        self.assertEqual(result["status"], "ARMED_NO_CAPTURE_AUTHORITY")
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text(encoding="utf-8")), stale)
        self.assertFalse(lock_path.exists())
        self.assertEqual(len(list(self.root.rglob("*.json"))), 1)

    def test_crash_after_stale_lock_archive_resumes_without_losing_evidence(self) -> None:
        self.root.mkdir(parents=True)
        lock_path = self.root / ".official-t0-arming.lock"
        stale = {
            "schema": "premarket_official_t0_arming_lock_v1",
            "run_id": "crashed-after-archive",
            "owner_pid": 424242,
            "owner_host": self.arming.socket.gethostname(),
            "nonce": "e" * 64,
        }
        raw = canonical_json_bytes(stale) + b"\n"
        lock_path.write_bytes(raw)
        archive_root = self.root.parent / f"{self.root.name}.lock-archive"
        archive_root.mkdir(parents=True)
        archive_path = archive_root / f"{hashlib.sha256(raw).hexdigest()}.json"
        archive_path.hardlink_to(lock_path)

        with mock.patch.object(
            self.arming, "process_is_alive", return_value=False, create=True
        ):
            result = self.invoke([event(now_ts=self.NOW)])

        self.assertEqual(result["status"], "ARMED_NO_CAPTURE_AUTHORITY")
        self.assertEqual(archive_path.read_bytes(), raw)
        self.assertFalse(lock_path.exists())

    def test_uncertain_foreign_or_malformed_lock_is_never_recovered(self) -> None:
        cases = (
            (
                {
                    "schema": "premarket_official_t0_arming_lock_v1",
                    "run_id": "unknown-owner",
                    "owner_pid": 424242,
                    "owner_host": self.arming.socket.gethostname(),
                    "nonce": "b" * 64,
                },
                None,
            ),
            (
                {
                    "schema": "premarket_official_t0_arming_lock_v1",
                    "run_id": "foreign-owner",
                    "owner_pid": 424242,
                    "owner_host": "different-host.invalid",
                    "nonce": "c" * 64,
                },
                False,
            ),
        )
        for index, (payload, liveness) in enumerate(cases):
            with self.subTest(index=index):
                root = Path(tempfile.mkdtemp()) / "official-t0-v1"
                root.mkdir(parents=True)
                lock_path = root / ".official-t0-arming.lock"
                lock_path.write_bytes(canonical_json_bytes(payload) + b"\n")
                with mock.patch.object(
                    self.arming, "process_is_alive", return_value=liveness, create=True
                ):
                    with self.assertRaisesRegex(self.arming.ArmingError, "LOCKED"):
                        with self.arming._arming_lock(root, run_id="new-run"):
                            self.fail("an uncertain or foreign lock must not be acquired")
                self.assertTrue(lock_path.exists())

        malformed_root = Path(tempfile.mkdtemp()) / "official-t0-v1"
        malformed_root.mkdir(parents=True)
        malformed_lock = malformed_root / ".official-t0-arming.lock"
        malformed_lock.write_bytes(b'{"schema":"wrong"}\n')
        with self.assertRaisesRegex(self.arming.ArmingError, "LOCKED"):
            with self.arming._arming_lock(malformed_root, run_id="new-run"):
                self.fail("a malformed lock must not be acquired")
        self.assertEqual(malformed_lock.read_bytes(), b'{"schema":"wrong"}\n')

    def test_release_never_deletes_a_replacement_lock(self) -> None:
        replacement = {
            "schema": "premarket_official_t0_arming_lock_v1",
            "run_id": "replacement-run",
            "owner_pid": 31337,
            "owner_host": self.arming.socket.gethostname(),
            "nonce": "d" * 64,
        }
        lock_path = self.root / ".official-t0-arming.lock"

        with self.assertRaisesRegex(self.arming.ArmingError, "ownership"):
            with self.arming._arming_lock(self.root, run_id="original-run"):
                lock_path.write_bytes(canonical_json_bytes(replacement) + b"\n")

        self.assertTrue(lock_path.exists())
        self.assertEqual(json.loads(lock_path.read_text(encoding="utf-8")), replacement)


class ArmingSelectorSurfaceTests(unittest.TestCase):
    def test_registry_exposes_a_distinct_early_arming_selector(self) -> None:
        self.assertTrue(callable(getattr(registry, "events_for_arming", None)))
        self.assertIsNot(registry.events_for_arming, registry.events_for_capture)


if __name__ == "__main__":
    unittest.main()
