"""Activation-hardening regressions for official t0 registry writes.

These tests deliberately describe the v6 contract before production implements it.
They use only local temporary registries: no adapter refresh and no network access.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import event_registry as registry  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import official_attestation as attestation  # noqa: E402
import project_config as config  # noqa: E402
import risk_gate  # noqa: E402


ANNOUNCED_UTC = "2027-01-15T04:00:00Z"
QUOTED_TIME = "Jan 15, 2027, 4:00AM UTC"
QUOTED_SYMBOL = "KII/USDT"
QUOTE = f"Spot trading for {QUOTED_SYMBOL} will start on {QUOTED_TIME}."
SOURCE_URL = (
    "https://announcements.bybit.com/en-US/article/bybit-to-list-kii-on-spot/"
)
CONTRACT_ID = "KIIUSDT"
SPOT_SYMBOL = "KIIUSDT"
GENERATION = 0
RECEIVED_AT = "2027-01-08T04:00:00Z"
T0_TS = 1_800_000_000  # Replaced below from the strict production parser.


def _t0_ts() -> int:
    return attestation.parse_announced_utc(ANNOUNCED_UTC)


def _build_attestation(**overrides):
    fields = {
        "venue": "bybit",
        "spot_symbol": SPOT_SYMBOL,
        "premarket_contract_id": CONTRACT_ID,
        "lifecycle_generation": GENERATION,
        "announced_utc": ANNOUNCED_UTC,
        "announcement_url": SOURCE_URL,
        "quoted_sentence": QUOTE,
        "quoted_time_text": QUOTED_TIME,
        "quoted_symbol_text": QUOTED_SYMBOL,
        "attested_by": "registry-v6-test",
        "now_ts": _t0_ts() - 7 * 24 * 3600,
        "asset_identity": registry.AssetIdentity(
            asset_class=registry.ASSET_CLASS_CRYPTO_TOKEN,
            issuer_namespace="crypto_asset",
            issuer_id="KII",
            evidence_class=registry.IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
        ),
    }
    fields.update(overrides)
    return attestation.build_attestation(**fields)


def _metadata_observation() -> dict:
    episode_id = registry.make_episode_id("bybit", CONTRACT_ID, GENERATION)
    observation = registry.make_timestamp_observation(
        episode_id=episode_id,
        venue="bybit",
        premarket_contract_id=CONTRACT_ID,
        spot_symbol=SPOT_SYMBOL,
        timestamp_kind=registry.TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
        timestamp_ts=_t0_ts() - 8 * 24 * 3600,
        instrument_role="premarket_perp",
        source_class=registry.SOURCE_VENUE_INSTRUMENT_METADATA,
        source_identity="bybit:instrument_metadata:launchTime",
        source_url="https://api.bybit.com/v5/market/instruments-info",
        received_at_utc=RECEIVED_AT,
        precision_sec=1,
        asset_identity=registry.AssetIdentity(
            asset_class=registry.ASSET_CLASS_CRYPTO_TOKEN,
            issuer_namespace="crypto_asset",
            issuer_id="KII",
            evidence_class=registry.IDENTITY_EVIDENCE_VENUE_EXPLICIT_METADATA,
        ),
    )
    observation["lifecycle_generation"] = GENERATION
    return observation


def _official_observation(
    *,
    episode_id: str | None = None,
    spot_symbol: str = SPOT_SYMBOL,
    source_url: str = SOURCE_URL,
    received_at_utc: str = RECEIVED_AT,
    attestation_schema: str | None = attestation.ATTESTATION_SCHEMA,
) -> dict:
    identity = episode_id or registry.make_episode_id(
        "bybit", CONTRACT_ID, GENERATION
    )
    observation = registry.make_timestamp_observation(
        episode_id=identity,
        venue="bybit",
        premarket_contract_id=CONTRACT_ID,
        spot_symbol=spot_symbol,
        timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
        timestamp_ts=_t0_ts(),
        instrument_role="spot",
        source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
        source_identity="human_attestation:registry-v6-test",
        source_url=source_url,
        received_at_utc=received_at_utc,
        precision_sec=60,
        caveats=("OFFICIAL_T0_READ_BY_A_PERSON_FROM_ANNOUNCEMENT_PROSE",),
        asset_identity=registry.AssetIdentity(
            asset_class=registry.ASSET_CLASS_CRYPTO_TOKEN,
            issuer_namespace="crypto_asset",
            issuer_id="KII",
            evidence_class=registry.IDENTITY_EVIDENCE_OFFICIAL_ATTESTATION,
        ),
    )
    observation["lifecycle_generation"] = GENERATION
    evidence = {
        "attested_by": "registry-v6-test",
        "announced_utc": ANNOUNCED_UTC,
        "quoted_sentence": QUOTE,
        "quoted_time_text": QUOTED_TIME,
        "quoted_symbol_text": QUOTED_SYMBOL,
        "announcement_url": source_url,
        "lead_sec_at_attestation": 7 * 24 * 3600,
    }
    if attestation_schema is not None:
        evidence["schema"] = attestation_schema
    observation["attestation"] = evidence
    return observation


def _records(*, official: dict | None = None) -> list[dict]:
    return registry.build_stream_revisions(
        [], [_metadata_observation(), official or _official_observation()]
    )


def _write_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in records),
        encoding="utf-8",
    )


def _write_refresh_summary(
    path: Path,
    records: list[dict],
    *,
    last_complete_metadata_refresh_received_at: str | None = None,
) -> None:
    receipt = registry.verify_registry(path)
    if receipt["status"] != "REGISTRY_OK":
        raise AssertionError(receipt)
    official_timestamps = [
        int(record["timestamp_ts"])
        for record in records
        if record.get("timestamp_kind") == registry.TIMESTAMP_OFFICIAL_SPOT_T0
    ]
    if last_complete_metadata_refresh_received_at is None:
        last_complete_metadata_refresh_received_at = RECEIVED_AT
    if official_timestamps and last_complete_metadata_refresh_received_at == RECEIVED_AT:
        last_complete_metadata_refresh_received_at = datetime.fromtimestamp(
            min(official_timestamps) - config.CAPTURE_WINDOW_BEFORE_SEC,
            timezone.utc,
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
    summary = {
        "schema": registry.REGISTRY_SCHEMA,
        "status": "REFRESH_COMPLETE",
        "mutation_type": "metadata_refresh",
        "refresh_run_id": "metadata-bootstrap",
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
        "resolved_paths_hash": "b" * 64,
        "refreshed_at_utc": RECEIVED_AT,
        registry.LAST_COMPLETE_METADATA_REFRESH_RECEIVED_AT_FIELD: (
            last_complete_metadata_refresh_received_at
        ),
        "complete": True,
        registry.ACTIVE_CONTRACTS_FIELD: registry._active_contract_ids(records),
        "registry": receipt,
    }
    registry._write_summary_with_mutation_receipt(
        path,
        summary,
        lock_owner=None,
    )


class EpisodeIdentityAndQuotationTests(unittest.TestCase):
    def test_official_t0_uses_the_metadata_lifecycle_episode_identity(self) -> None:
        observation = _build_attestation()

        self.assertEqual(
            observation["episode_id"],
            registry.make_episode_id("bybit", CONTRACT_ID, GENERATION),
        )
        self.assertEqual(observation["lifecycle_generation"], GENERATION)

    def test_quoted_time_must_equal_announced_utc(self) -> None:
        mismatched_time = "Jan 15, 2027, 5:00AM UTC"
        with self.assertRaisesRegex(
            attestation.AttestationError, "quoted.*time|announced.*time|timestamp"
        ):
            _build_attestation(
                quoted_sentence=(
                    f"Spot trading for {QUOTED_SYMBOL} will start on "
                    f"{mismatched_time}."
                ),
                quoted_time_text=mismatched_time,
            )

    def test_quoted_symbol_must_equal_the_spot_symbol(self) -> None:
        mismatched_symbol = "OTHER/USDT"
        with self.assertRaisesRegex(
            attestation.AttestationError, "quoted.*symbol|spot.*symbol|symbol"
        ):
            _build_attestation(
                quoted_sentence=(
                    f"Spot trading for {mismatched_symbol} will start on {QUOTED_TIME}."
                ),
                quoted_symbol_text=mismatched_symbol,
            )

    def test_declared_fragments_must_appear_verbatim_in_the_sentence(self) -> None:
        with self.assertRaisesRegex(
            attestation.AttestationError, "quoted.*time|sentence|fragment"
        ):
            _build_attestation(quoted_time_text="Jan 15, 2027, 04:00 UTC")


class OfficialRecordSemanticVerificationTests(unittest.TestCase):
    def _report_for(self, official: dict) -> dict:
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        _write_records(path, _records(official=official))
        return registry.verify_registry(path)

    def test_a_complete_official_record_is_valid(self) -> None:
        self.assertEqual(
            self._report_for(_official_observation())["status"], "REGISTRY_OK"
        )

    def test_official_records_are_rejected_without_semantic_provenance(self) -> None:
        wrong_episode = registry.make_episode_id("bybit", "OTHERUSDT", GENERATION)
        cases = {
            "attestation_schema": (
                _official_observation(attestation_schema=None),
                r"attestation.*schema|schema.*attestation",
            ),
            "official_https_host": (
                _official_observation(
                    source_url="https://announcements.bybit.com.evil.example/listing"
                ),
                r"official.*host|source_url|announcement",
            ),
            "official_explicit_port": (
                _official_observation(
                    source_url="https://announcements.bybit.com:444/listing"
                ),
                r"official.*host|source_url|announcement",
            ),
            "utc_received_at": (
                _official_observation(received_at_utc="2027-01-08T04:00:00"),
                r"received_at_utc|UTC",
            ),
            "episode_identity": (
                _official_observation(episode_id=wrong_episode),
                r"episode_id|episode",
            ),
            "contract_spot_mapping": (
                _official_observation(spot_symbol="OTHERUSDT"),
                r"spot_symbol|mapping|contract",
            ),
        }
        for name, (official, expected_problem) in cases.items():
            with self.subTest(name=name):
                report = self._report_for(official)
                self.assertEqual(report["status"], "REGISTRY_PROBLEMS")
                self.assertRegex("; ".join(report["problems"]), expected_problem)

    def test_registry_rechecks_that_the_quoted_time_matches_the_official_t0(self) -> None:
        official = _official_observation()
        official["attestation"] = dict(
            official["attestation"],
            quoted_sentence=(
                f"Spot trading for {QUOTED_SYMBOL} will start on "
                "Jan 15, 2027, 5:00AM UTC."
            ),
            quoted_time_text="Jan 15, 2027, 5:00AM UTC",
        )
        report = self._report_for(official)
        self.assertEqual(report["status"], "REGISTRY_PROBLEMS")
        self.assertRegex("; ".join(report["problems"]), r"quoted time|announced_utc")

    def test_registry_rejects_bidi_or_escape_controls_in_official_evidence(self) -> None:
        for field, contaminated in (
            ("quoted_sentence", QUOTE.replace("will", "\u202ewill")),
            ("attested_by", "registry-v6-test\x1b"),
        ):
            with self.subTest(field=field):
                official = _official_observation()
                official["attestation"] = dict(
                    official["attestation"], **{field: contaminated}
                )
                report = self._report_for(official)
                self.assertEqual(report["status"], "REGISTRY_PROBLEMS")
                self.assertRegex("; ".join(report["problems"]), r"canonical|control")

    def test_okx_swap_suffix_maps_to_the_corresponding_spot_market(self) -> None:
        contract_id = "KII-USDT-SWAP"
        spot_symbol = "KII-USDT"
        generation = 0
        episode_id = registry.make_episode_id("okx", contract_id, generation)
        metadata = registry.make_timestamp_observation(
            episode_id=episode_id,
            venue="okx",
            premarket_contract_id=contract_id,
            spot_symbol=spot_symbol,
            timestamp_kind=registry.TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
            timestamp_ts=_t0_ts() - 8 * 24 * 3600,
            instrument_role="premarket_perp",
            source_class=registry.SOURCE_VENUE_INSTRUMENT_METADATA,
            source_identity="okx:instrument_metadata:listTime",
            source_url="https://www.okx.com/api/v5/public/instruments",
            received_at_utc=RECEIVED_AT,
            precision_sec=1,
            lifecycle_generation=generation,
        )
        official = registry.make_timestamp_observation(
            episode_id=episode_id,
            venue="okx",
            premarket_contract_id=contract_id,
            spot_symbol=spot_symbol,
            timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            timestamp_ts=_t0_ts(),
            instrument_role="spot",
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="human_attestation:registry-v6-test",
            source_url="https://www.okx.com/help/kii-listing",
            received_at_utc=RECEIVED_AT,
            precision_sec=60,
            caveats=("OFFICIAL_T0_READ_BY_A_PERSON_FROM_ANNOUNCEMENT_PROSE",),
            lifecycle_generation=generation,
        )
        official["attestation"] = {
            "schema": attestation.ATTESTATION_SCHEMA,
            "attested_by": "registry-v6-test",
            "announced_utc": ANNOUNCED_UTC,
            "quoted_sentence": QUOTE,
            "quoted_time_text": QUOTED_TIME,
            "quoted_symbol_text": QUOTED_SYMBOL,
            "announcement_url": "https://www.okx.com/help/kii-listing",
            "lead_sec_at_attestation": 7 * 24 * 3600,
        }
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        _write_records(path, registry.build_stream_revisions([], [metadata, official]))
        self.assertEqual(registry.verify_registry(path)["status"], "REGISTRY_OK")


class AttestationTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.path = root / "listing-events-v2.jsonl"
        self.lock_path = root / "listing-events-v2.lock"
        initial = registry.build_stream_revisions([], [_metadata_observation()])
        _write_records(self.path, initial)
        _write_refresh_summary(self.path, initial)
        self.preflight = {
            "schema": risk_gate.PREFLIGHT_RESULT_SCHEMA,
            "ok": True,
            "verified": True,
            "decision": "ALLOW_OFFICIAL_ATTESTATION",
            "write_class": "official_attestation",
            "run_id": "official-attestation-v6",
            "action": risk_gate.OFFICIAL_ATTESTATION_ACTION,
            "plan_id": trust_root.PLAN_ID,
            "plan_hash": trust_root.PLAN_HASH,
            "resolved_paths_hash": "c" * 64,
        }

    def test_attestation_and_production_summary_commit_as_one_locked_mutation(self) -> None:
        with mock.patch.object(registry, "REGISTRY_PATH", self.path), mock.patch.object(
            registry, "REGISTRY_LOCK_PATH", self.lock_path
        ), mock.patch.object(
            attestation.risk_gate, "preflight", return_value=self.preflight
        ) as preflight, mock.patch.object(
            attestation.time, "time", return_value=_t0_ts() - 7 * 24 * 3600
        ):
            result = attestation.attest(
                run_id="official-attestation-v6",
                venue="bybit",
                spot_symbol=SPOT_SYMBOL,
                premarket_contract_id=CONTRACT_ID,
                lifecycle_generation=GENERATION,
                announced_utc=ANNOUNCED_UTC,
                announcement_url=SOURCE_URL,
                quoted_sentence=QUOTE,
                quoted_time_text=QUOTED_TIME,
                quoted_symbol_text=QUOTED_SYMBOL,
                attested_by="registry-v6-test",
            )
            report = registry.verify_registry(self.path)

        self.assertEqual(preflight.call_count, 2)
        preflight.assert_has_calls(
            [
                mock.call(
                    write_class="official_attestation",
                    run_id="official-attestation-v6",
                ),
                mock.call(
                    write_class="official_attestation",
                    run_id="official-attestation-v6",
                ),
            ]
        )
        self.assertEqual(result["status"], "ATTESTED")
        self.assertEqual(report["status"], "REGISTRY_OK", report["problems"])
        summary = json.loads(
            self.path.with_suffix(".summary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(summary["mutation_type"], "official_attestation")
        self.assertEqual(summary["mutation_run_id"], "official-attestation-v6")
        self.assertEqual(summary["registry"]["head_record_hash"], report["head_record_hash"])
        self.assertEqual(summary["registry"]["entries"], 2)


class MaterializedCaptureProvenanceTests(unittest.TestCase):
    def test_candidate_carries_the_exact_official_record_and_source_evidence(self) -> None:
        root = Path(tempfile.mkdtemp())
        path = root / "listing-events-v2.jsonl"
        lock_path = root / "listing-events-v2.lock"
        records = _records()
        official_record = records[-1]
        _write_records(path, records)
        _write_refresh_summary(path, records)

        with mock.patch.object(registry, "REGISTRY_PATH", path), mock.patch.object(
            registry, "REGISTRY_LOCK_PATH", lock_path
        ):
            candidates = registry.events_for_capture(
                now_ts=_t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC,
                source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(
            candidate["episode_id"],
            registry.make_episode_id("bybit", CONTRACT_ID, GENERATION),
        )
        provenance = candidate["official_t0_provenance"]
        self.assertEqual(provenance["source_url"], SOURCE_URL)
        self.assertEqual(provenance["received_at_utc"], RECEIVED_AT)
        self.assertEqual(provenance["record_hash"], official_record["record_hash"])
        self.assertEqual(provenance["t0_precision_sec"], 60)
        self.assertEqual(
            provenance["caveats"],
            ["OFFICIAL_T0_READ_BY_A_PERSON_FROM_ANNOUNCEMENT_PROSE"],
        )
        self.assertEqual(provenance["attestation"]["quoted_sentence"], QUOTE)


if __name__ == "__main__":
    unittest.main()
