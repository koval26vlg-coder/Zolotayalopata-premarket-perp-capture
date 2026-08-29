"""v29 recovery and seconds-grade official-anchor contract."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import event_registry as registry  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import plan_builder  # noqa: E402
import project_config as config  # noqa: E402
import registry_quarantine as quarantine  # noqa: E402
import risk_gate  # noqa: E402


V28_RELATIVE_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v28.json"
V28_PLAN_HASH = "141ab762953a21985eb6678c3c4bafb6247eadf7bef1073cc9626ee89d404d80"
V28_FILE_SHA256 = "b59162ee152bf1fc2301921925267731ba1c1f2f9c3d92fe4093354d55797d92"
V29_STATUS = "REGISTRY_RECOVERY_SECONDS_GRADE_OFFICIAL_ANCHOR_NO_CAPTURE"


class V29ImmutablePlanTests(unittest.TestCase):
    def test_v28_bytes_are_preserved_and_frozen_v29_supersedes_exact_identity(self) -> None:
        v28_path = ROOT / V28_RELATIVE_PATH
        self.assertEqual(hashlib.sha256(v28_path.read_bytes()).hexdigest(), V28_FILE_SHA256)
        self.assertEqual(
            json.loads(v28_path.read_text(encoding="utf-8"))["plan_hash"],
            V28_PLAN_HASH,
        )
        self.assertEqual(config.V28_PLAN_PATH, v28_path)
        self.assertEqual(config.V29_PLAN_PATH.name, "premarket-perp-capture-planonly-20260822-v29.json")

        plan = json.loads(config.V29_PLAN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(plan["schema"], "premarket_perp_capture_planonly_v29")
        self.assertEqual(plan["plan_id"], "premarket_perp_capture_20260822_v29")
        self.assertEqual(plan["supersedes_plan_id"], "premarket_perp_capture_20260822_v28")
        self.assertEqual(plan["supersedes_plan_hash"], V28_PLAN_HASH)
        self.assertEqual(plan["supersedes_plan_path"], V28_RELATIVE_PATH)
        self.assertEqual(plan["status"], V29_STATUS)
        self.assertFalse(plan["activation_gate"]["capture_authorized"])
        self.assertFalse(plan["acceptance_policy"]["acceptance_capable"])

    def test_v29_preregisters_conditional_seconds_grade_authority(self) -> None:
        plan = json.loads(config.V29_PLAN_PATH.read_text(encoding="utf-8"))
        registry_contract = plan["event_registry"]
        readiness = registry_contract["seconds_grade_readiness"]
        self.assertEqual(readiness["required_precision_sec_lte"], 1)
        self.assertTrue(readiness["current_official_producer_capable"])
        self.assertEqual(
            readiness["current_result"],
            "CANDIDATE_ONLY_WHEN_VERBATIM_SOURCE_EXPLICITLY_STATES_SECONDS",
        )
        precision = registry_contract["official_attestation"]["precision_policy"]
        self.assertEqual(
            registry_contract["official_attestation"]["schema"],
            "premarket_perp_official_attestation_v2",
        )
        self.assertEqual(
            precision["derivation"], "VERBATIM_QUOTED_TIME_TEXT_GRANULARITY"
        )
        self.assertEqual(precision["accepted_precision_sec"], [1, 60])
        self.assertEqual(precision["capture_candidate_max_precision_sec"], 1)
        verifier = registry_contract["candidate_verifier"]
        self.assertEqual(verifier["mode"], "READ_ONLY_NO_TOKEN_NO_CLAIM_NO_NETWORK")
        self.assertEqual(verifier["precision_sec_lte"], 1)
        self.assertEqual(verifier["asset_class"], "CRYPTO_TOKEN")
        self.assertEqual(verifier["source_class"], "OFFICIAL_ANNOUNCEMENT")
        self.assertEqual(verifier["cli"], "event_registry.py --candidate-status")

    def test_v29_and_v30_are_retired_beneath_the_current_trust_root(self) -> None:
        self.assertEqual(trust_root.PLAN_ID, plan_builder.PLAN_ID)
        plan = risk_gate.load_and_verify_plan()
        self.assertEqual(plan["plan_hash"], trust_root.PLAN_HASH)
        self.assertEqual(
            hashlib.sha256(config.PLAN_PATH.read_bytes()).hexdigest(),
            trust_root.PLAN_FILE_SHA256,
        )
        retired = {
            str(item["path"]).replace("\\", "/"): item
            for item in trust_root.RETIRED_PLANS
        }
        self.assertIn(V28_RELATIVE_PATH, retired)
        self.assertEqual(retired[V28_RELATIVE_PATH]["plan_hash"], V28_PLAN_HASH)
        self.assertEqual(
            retired[V28_RELATIVE_PATH]["plan_file_sha256"], V28_FILE_SHA256
        )
        v29_relative = "docs/plans/premarket-perp-capture-planonly-20260822-v29.json"
        self.assertIn(v29_relative, retired)
        self.assertEqual(retired[v29_relative]["plan_hash"], "63f4173a4d3662e6eed15f9ba1f372c8771f635b84291ed2439e076d6975a8d5")
        self.assertEqual(
            retired[v29_relative]["plan_file_sha256"],
            "7c93aebec952ec1d52def42ce5ac4165b6b3c8c608436ed702f50dbfb012b822",
        )
        v30_relative = "docs/plans/premarket-perp-capture-planonly-20260822-v30.json"
        self.assertIn(v30_relative, retired)
        self.assertEqual(
            retired[v30_relative]["plan_hash"],
            "32877c7c731bdf63167b20827f373726e34e1fbc1bcd61db26d6975444067ab5",
        )

    def test_operator_docs_identify_v34_as_active_and_v33_as_preserved(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("premarket_perp_capture_20260822_v34", readme)
        self.assertIn("v33 сохранён byte-identical", readme)
        self.assertIn("активный immutable план — v34", agents)
        self.assertIn("v30–v33 сохранены byte-identical", readme)


class SecondsGradeAdmissionTests(unittest.TestCase):
    def _official(self, precision_sec: int) -> dict[str, object]:
        identity = registry.AssetIdentity(
            asset_class=registry.ASSET_CLASS_CRYPTO_TOKEN,
            issuer_namespace="venue:bybit",
            issuer_id="NEW",
            evidence_class=registry.IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
        )
        episode_id = registry.make_episode_id("bybit", "NEWUSDT", 1)
        observation = registry.make_timestamp_observation(
            episode_id=episode_id,
            venue="bybit",
            premarket_contract_id="NEWUSDT",
            spot_symbol="NEWUSDT",
            timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            timestamp_ts=1_800_003_600,
            instrument_role="spot",
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="human_attestation:reviewer",
            source_url="https://announcements.bybit.com/en-US/article/new",
            received_at_utc="2027-01-15T03:00:00Z",
            precision_sec=precision_sec,
            caveats=("OFFICIAL_T0_READ_BY_A_PERSON_FROM_ANNOUNCEMENT_PROSE",),
            lifecycle_generation=1,
            asset_identity=identity,
        )
        observation["listing_venue"] = "bybit"
        return observation

    def test_minute_precision_is_descriptive_before_token_claim_or_network(self) -> None:
        observation = self._official(60)
        self.assertFalse(observation["capture_eligible"])
        self.assertEqual(observation["evidence_use"], "DESCRIPTIVE_ONLY")

    def test_explicit_second_precision_can_become_an_anchor(self) -> None:
        observation = self._official(1)
        self.assertTrue(observation["capture_eligible"])
        self.assertEqual(observation["evidence_use"], "ACCEPTANCE_ANCHOR")

    def test_precision_factory_rejects_coercible_or_nonpositive_values(self) -> None:
        for value in (True, False, "1", 1.0, 1.9, 0, -1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    registry.EventRegistryError, "positive integer"
                ):
                    self._official(value)  # type: ignore[arg-type]

    def test_equal_t0_prefers_seconds_grade_independent_of_append_order(self) -> None:
        metadata = registry.make_timestamp_observation(
            episode_id=registry.make_episode_id("bybit", "NEWUSDT", 1),
            venue="bybit",
            premarket_contract_id="NEWUSDT",
            spot_symbol="NEWUSDT",
            timestamp_kind=registry.TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
            timestamp_ts=1_800_000_000,
            instrument_role="premarket_perp",
            source_class=registry.SOURCE_VENUE_INSTRUMENT_METADATA,
            source_identity="bybit:instrument_metadata:launchTime",
            source_url="https://api.bybit.com/v5/market/instruments-info",
            received_at_utc="2027-01-15T02:00:00Z",
            precision_sec=1,
            lifecycle_generation=1,
            asset_identity=registry.AssetIdentity(
                asset_class=registry.ASSET_CLASS_CRYPTO_TOKEN,
                issuer_namespace="venue:bybit",
                issuer_id="NEW",
                evidence_class=registry.IDENTITY_EVIDENCE_VENUE_EXPLICIT_METADATA,
            ),
        )
        minute = self._official(60)
        minute["source_identity"] = "human_attestation:minute"
        minute["stream_id"] = registry._stream_id(
            episode_id=str(minute["episode_id"]),
            timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            instrument_role="spot",
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="human_attestation:minute",
        )
        second = self._official(1)
        second["source_identity"] = "human_attestation:second"
        second["stream_id"] = registry._stream_id(
            episode_id=str(second["episode_id"]),
            timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            instrument_role="spot",
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="human_attestation:second",
        )

        first = registry.materialize_episodes(
            registry.build_stream_revisions([], [metadata, minute, second])
        )[0]
        reversed_order = registry.materialize_episodes(
            registry.build_stream_revisions([], [metadata, second, minute])
        )[0]

        self.assertEqual(first["t0_precision_sec"], 1)
        self.assertEqual(reversed_order["t0_precision_sec"], 1)
        self.assertTrue(first["capture_eligible"])
        self.assertTrue(reversed_order["capture_eligible"])

    def test_candidate_verifier_reports_every_fail_closed_boundary(self) -> None:
        episode = {
            "episode_id": "episode",
            "venue": "bybit",
            "premarket_contract_id": "NEWUSDT",
            "spot_symbol": "NEWUSDT",
            "lifecycle_generation": 1,
            "official_spot_t0": 1_800_003_600,
            "t0_source_class": registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            "t0_precision_sec": 60,
            "asset_class": registry.ASSET_CLASS_UNCLASSIFIED,
            "asset_identity_conflict": False,
            "official_conflict": False,
            "capture_eligible": False,
            "evidence_use": "DESCRIPTIVE_ONLY",
            "official_t0_provenance": {
                "received_at_utc": "2027-01-15T03:00:00Z",
            },
        }
        reasons = registry.capture_candidate_problems(
            episode,
            active_generation=2,
            metadata_refresh_received_at_utc="2027-01-14T00:00:00Z",
            now_ts=1_800_001_800,
            horizon_sec=24 * 3600,
        )
        self.assertIn("ASSET_CLASS_NOT_CRYPTO_TOKEN", reasons)
        self.assertIn("OFFICIAL_T0_PRECISION_GT_ONE_SECOND", reasons)
        self.assertIn("LIFECYCLE_GENERATION_NOT_CURRENT", reasons)
        self.assertIn("METADATA_REFRESH_STALE_OR_FUTURE", reasons)
        self.assertIn("NOT_CAPTURE_ELIGIBLE", reasons)

        invalid_precision = dict(episode, t0_precision_sec=True)
        invalid_reasons = registry.capture_candidate_problems(
            invalid_precision,
            active_generation=2,
            metadata_refresh_received_at_utc="2027-01-14T00:00:00Z",
            now_ts=1_800_001_800,
            horizon_sec=24 * 3600,
        )
        self.assertIn("OFFICIAL_T0_PRECISION_INVALID", invalid_reasons)

    def test_seconds_grade_candidate_has_no_readiness_problems(self) -> None:
        t0 = 1_800_003_600
        now = t0 - config.CAPTURE_WINDOW_BEFORE_SEC
        episode = {
            "episode_id": "episode",
            "venue": "bybit",
            "premarket_contract_id": "NEWUSDT",
            "spot_symbol": "NEWUSDT",
            "lifecycle_generation": 1,
            "official_spot_t0": t0,
            "t0_source_class": registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            "t0_precision_sec": 1,
            "asset_class": registry.ASSET_CLASS_CRYPTO_TOKEN,
            "asset_identity_conflict": False,
            "official_conflict": False,
            "capture_eligible": True,
            "evidence_use": "ACCEPTANCE_ANCHOR",
            "official_t0_provenance": {
                "received_at_utc": "2027-01-15T03:00:00Z",
            },
        }
        metadata_received = registry.datetime.fromtimestamp(
            now - 30, registry.timezone.utc
        ).isoformat().replace("+00:00", "Z")
        self.assertEqual(
            registry.capture_candidate_problems(
                episode,
                active_generation=1,
                metadata_refresh_received_at_utc=metadata_received,
                now_ts=now,
                horizon_sec=24 * 3600,
            ),
            [],
        )

    def test_candidate_status_never_bypasses_authoritative_selector_failures(self) -> None:
        verified = {
            "status": "REGISTRY_OK",
            "summary_required": True,
            "summary_verified": True,
            "problems": [],
        }
        with mock.patch.object(
            registry, "_verify_registry_snapshot", return_value=([], verified)
        ), mock.patch.object(
            registry,
            "events_for_capture",
            side_effect=registry.EventRegistryError(
                "ACTIVE_PLAN_MUTATION_RECEIPT_MISMATCH"
            ),
        ):
            result = registry.inspect_capture_candidates(now_ts=1_800_000_000)

        self.assertEqual(result["status"], "CAPTURE_AUTHORITY_NOT_READY")
        self.assertFalse(result["capture_authorized"])
        self.assertEqual(result["candidates"], [])
        self.assertIn(
            "ACTIVE_PLAN_MUTATION_RECEIPT_MISMATCH", result["authority_problem"]
        )

    def test_invalid_registry_candidate_status_is_read_only_and_fail_closed(self) -> None:
        invalid = {
            "status": "REGISTRY_PROBLEMS",
            "summary_required": True,
            "summary_verified": False,
            "problems": ["summary contract mismatch"],
        }
        with mock.patch.object(
            registry, "_verify_registry_snapshot", return_value=([], invalid)
        ), mock.patch.object(
            registry, "events_for_capture"
        ) as selector, mock.patch.object(
            registry, "materialize_episodes"
        ) as materialize:
            result = registry.inspect_capture_candidates(now_ts=1_800_000_000)

        self.assertEqual(result["status"], "REGISTRY_RECOVERY_REQUIRED")
        self.assertFalse(result["capture_authorized"])
        self.assertEqual(result["candidates"], [])
        selector.assert_not_called()
        materialize.assert_not_called()


class HistoricalQuarantineStatusTests(unittest.TestCase):
    def test_completed_v2_archives_remain_completed_after_tombstone_cleanup(self) -> None:
        for transaction_id in (
            "20260823T212914Z-bc6c206eb6-8fb85904",
            "20260824T063525Z-7bc78caf9a-ae4ed943",
        ):
            with self.subTest(transaction_id=transaction_id):
                status = quarantine.quarantine_transaction_status(transaction_id)
                self.assertEqual(status["status"], "COMPLETED", status["problems"])
                self.assertEqual(status["state"], quarantine.STATE_LOCK_RELEASED)


if __name__ == "__main__":
    unittest.main()
