"""Causal replay v2 contract: received-time only, no invented market path."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import project_config as config  # noqa: E402
import capture  # noqa: E402
import replay  # noqa: E402
from canonical_hash import canonical_hash  # noqa: E402


T0 = 1_800_000_000.0


def bybit_book(bid: float, ask: float, *, bid_size: float = 10.0,
               ask_size: float = 11.0) -> dict:
    return {
        "retCode": 0,
        "result": {
            "s": "NEWUSDT",
            "b": [[str(bid), str(bid_size)]],
            "a": [[str(ask), str(ask_size)]],
            "ts": "0",
        },
    }


def row(
    *,
    request_ts: float,
    received_ts: float,
    exchange_ts: float,
    bid: float,
    ask: float,
    error: str | None = None,
) -> dict:
    item = {
        "schema": "premarket_perp_capture_sample_v1",
        "capture_id": "test_capture",
        "venue": "bybit",
        "symbol": "NEWUSDT",
        "probe": "orderbook",
        "t0_ts": T0,
        "request_ts": request_ts,
        "received_ts": received_ts,
        "exchange_ts": exchange_ts,
        "payload": bybit_book(bid, ask),
    }
    if error is not None:
        item["error"] = error
    return item


def write_capture(rows: list[dict]) -> Path:
    directory = Path(tempfile.mkdtemp())
    raw = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows).encode()
    (directory / "samples.jsonl").write_bytes(raw)
    manifest = {
        "schema": "premarket_perp_capture_v1",
        "capture_id": "test_capture",
        "evidence_class": "SYNTHETIC_OFFLINE_ONLY",
        "acceptance_capable": False,
        "venue": "bybit",
        "symbol": "NEWUSDT",
        "t0_ts": int(T0),
        "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
        "output_sha256": hashlib.sha256(raw).hexdigest(),
        "replay_readiness": {"ready": True},
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline=""
    )
    return directory


def valid_row(received_offset: float, *, exchange_offset: float | None = None,
              bid: float = 100.0, ask: float = 101.0) -> dict:
    exchange_offset = received_offset if exchange_offset is None else exchange_offset
    return row(
        request_ts=T0 + received_offset - 0.1,
        received_ts=T0 + received_offset,
        exchange_ts=T0 + exchange_offset,
        bid=bid,
        ask=ask,
    )


class ReceivedClockSeriesTests(unittest.TestCase):
    def test_book_sample_carries_all_three_clocks(self) -> None:
        sample = replay.BookSample(
            source_row_index=7,
            request_ts=T0,
            received_ts=T0 + 0.1,
            exchange_ts=T0 + 0.05,
            bid_px=100.0,
            bid_sz=5.0,
            ask_px=101.0,
            ask_sz=6.0,
        )
        self.assertEqual(sample.request_ts, T0)
        self.assertEqual(sample.received_ts, T0 + 0.1)
        self.assertEqual(sample.exchange_ts, T0 + 0.05)

    def test_series_is_sorted_by_received_not_exchange_time(self) -> None:
        rows = [
            valid_row(2.0, exchange_offset=1.0, bid=102, ask=103),
            valid_row(1.0, exchange_offset=1.5, bid=100, ask=101),
        ]
        series = replay.book_series(rows, "bybit")
        self.assertEqual([item.received_ts for item in series], [T0 + 1, T0 + 2])
        self.assertEqual([item.bid_px for item in series], [100.0, 102.0])

    def test_noncausal_received_before_request_is_filtered(self) -> None:
        rows = [row(
            request_ts=T0 + 1,
            received_ts=T0,
            exchange_ts=T0,
            bid=100,
            ask=101,
        )]
        self.assertEqual(replay.book_series(rows, "bybit"), [])

    def test_stale_exchange_snapshot_is_filtered(self) -> None:
        stale = config.MAX_SAMPLE_STALENESS_SEC["orderbook"] + 0.001
        rows = [valid_row(1.0, exchange_offset=1.0 - stale)]
        self.assertEqual(replay.book_series(rows, "bybit"), [])

    def test_future_skewed_exchange_snapshot_is_filtered(self) -> None:
        future = config.MAX_EXCHANGE_FUTURE_SKEW_SEC + 0.001
        rows = [valid_row(1.0, exchange_offset=1.0 + future)]
        self.assertEqual(replay.book_series(rows, "bybit"), [])

    def test_nonfinite_clock_is_filtered(self) -> None:
        bad = valid_row(1.0)
        bad["received_ts"] = math.nan
        self.assertEqual(replay.book_series([bad], "bybit"), [])


class CausalSelectionTests(unittest.TestCase):
    def series(self, offsets: list[float]) -> list[replay.BookSample]:
        return replay.book_series(
            [valid_row(offset, bid=100 + index, ask=101 + index)
             for index, offset in enumerate(offsets)],
            "bybit",
        )

    def test_first_valid_sample_at_or_after_target_is_selected(self) -> None:
        observation = replay.first_causal_book(
            self.series([-0.1, 0.2, 0.3]),
            target_ts=T0,
            side="bid",
            max_lag_sec=0.5,
        )
        self.assertTrue(observation.observed)
        self.assertEqual(observation.received_ts, T0 + 0.2)
        self.assertEqual(observation.price, 101.0)
        self.assertAlmostEqual(observation.selection_lag_sec, 0.2)

    def test_pre_target_sample_is_never_a_fallback(self) -> None:
        observation = replay.first_causal_book(
            self.series([-0.1]), target_ts=T0, side="bid", max_lag_sec=0.5
        )
        self.assertFalse(observation.observed)
        self.assertEqual(observation.status, "NO_SAMPLE_AT_OR_AFTER_TARGET")
        self.assertIsNone(observation.price)

    def test_first_sample_after_one_cadence_is_not_observed(self) -> None:
        observation = replay.first_causal_book(
            self.series([0.500001]), target_ts=T0, side="bid", max_lag_sec=0.5
        )
        self.assertFalse(observation.observed)
        self.assertEqual(observation.status, "SAMPLE_AFTER_CADENCE")
        self.assertIsNone(observation.price)

    def test_sample_exactly_one_cadence_late_is_observed(self) -> None:
        observation = replay.first_causal_book(
            self.series([0.5]), target_ts=T0, side="ask", max_lag_sec=0.5
        )
        self.assertTrue(observation.observed)
        self.assertEqual(observation.price, 101.0)
        self.assertEqual(observation.visible_size, 11.0)

    def test_equal_received_times_are_tied_by_source_row_order(self) -> None:
        series = replay.book_series(
            [valid_row(0.2, bid=100, ask=101), valid_row(0.2, bid=200, ask=201)],
            "bybit",
        )
        observation = replay.first_causal_book(
            series, target_ts=T0, side="bid", max_lag_sec=0.5
        )
        self.assertEqual(observation.price, 100.0)
        self.assertEqual(observation.source_row_index, 0)


class ReplayV2ReportTests(unittest.TestCase):
    def dense_capture(self) -> Path:
        offsets = [-60.0, 0.0, 5.0, 15.0, 60.0]
        rows = [
            valid_row(offset, bid=100 + index * 2, ask=101 + index * 2)
            for index, offset in enumerate(offsets)
        ]
        return write_capture(rows)

    def test_report_is_v2_and_uses_point_observations(self) -> None:
        report = replay.replay_capture(
            self.dense_capture(),
            evidence_mode="SYNTHETIC_DESCRIPTIVE_ONLY",
        )
        self.assertEqual(report["schema"], "premarket_perp_replay_v2")
        self.assertTrue(report["causal_replay_readiness"]["ready"])
        self.assertEqual(report["horizons_observed"], report["horizons_requested"])
        for horizon in report["horizons"]:
            self.assertTrue(horizon["gross_bbo_return"]["computable"])
            self.assertIn("value", horizon["gross_bbo_return"])
            self.assertNotIn("low", horizon["gross_bbo_return"])
            self.assertNotIn("high", horizon["gross_bbo_return"])
            self.assertIn("received_ts", horizon["exit_observation"])
            self.assertIn("selection_lag_sec", horizon["exit_observation"])

    def test_sparse_bracket_does_not_invent_a_bound_or_return(self) -> None:
        directory = write_capture([
            valid_row(-1.0, bid=100, ask=101),
            valid_row(1.0, bid=200, ask=201),
        ])
        report = replay.replay_capture(
            directory,
            horizons_sec=(0,),
            entry_lead_sec=1,
            evidence_mode="SYNTHETIC_DESCRIPTIVE_ONLY",
        )
        horizon = report["horizons"][0]
        self.assertEqual(horizon["exit_observation"]["status"], "SAMPLE_AFTER_CADENCE")
        self.assertFalse(horizon["gross_bbo_return"]["computable"])
        self.assertIsNone(horizon["gross_bbo_return"]["value"])
        self.assertFalse(report["causal_replay_readiness"]["ready"])

    def test_only_pre_target_exit_evidence_is_missing(self) -> None:
        directory = write_capture([
            valid_row(-60.0, bid=100, ask=101),
            valid_row(-0.001, bid=110, ask=111),
        ])
        report = replay.replay_capture(
            directory,
            horizons_sec=(0,),
            evidence_mode="SYNTHETIC_DESCRIPTIVE_ONLY",
        )
        horizon = report["horizons"][0]
        self.assertEqual(
            horizon["exit_observation"]["status"], "NO_SAMPLE_AT_OR_AFTER_TARGET"
        )
        self.assertFalse(horizon["gross_bbo_return"]["computable"])

    def test_report_makes_only_a_descriptive_bbo_claim(self) -> None:
        report = replay.replay_capture(
            self.dense_capture(),
            evidence_mode="SYNTHETIC_DESCRIPTIVE_ONLY",
        )
        self.assertEqual(report["research_classification"], "DESCRIPTIVE_ONLY")
        encoded = json.dumps(report, sort_keys=True).lower()
        for forbidden_key in (
            '"fill"', '"filled"', '"execution"', '"acceptance_decision"',
            '"accepted"', '"rejected"',
        ):
            self.assertNotIn(forbidden_key, encoded)

    def test_legacy_bracket_and_bound_apis_are_removed(self) -> None:
        for name in (
            "Bracket", "PriceBound", "bracket_at", "bound_from_bracket",
            "bounded_return",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(replay, name))


CAPTURE_ID = "capture-v16-production"
PLAN_ID = "premarket-perp-plan-v16-test"
PLAN_HASH = "e" * 64
IMPLEMENTATION = {
    "files": [
        {
            "name": "replay",
            "repo_path": "src/replay.py",
            "sha256": "f" * 64,
        }
    ]
}
LINEAGE = {
    "episode_id": "bybit:NEWUSDT:official",
    "venue": "bybit",
    "premarket_contract_id": "NEWUSDT",
    "spot_symbol": "NEWUSDT",
    "official_spot_t0": int(T0),
    "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
    "t0_precision_sec": 1,
    "asset_class": "CRYPTO_TOKEN",
    "issuer_namespace": "crypto_asset",
    "issuer_id": "new-token",
    "asset_identity_hash": "9" * 64,
    "official_record_hash": "a" * 64,
    "official_source_url": "https://announcements.bybit.com/en/article/new-listing",
    "official_source_identity": "human_attestation:test",
    "registry_sha256": "b" * 64,
    "registry_tail_record_hash": "d" * 64,
    "mutation_receipt_seq": 7,
    "mutation_receipt_hash": "1" * 64,
    "summary_content_sha256": "2" * 64,
    "registry_authority_state_hash": "3" * 64,
    "plan_id": PLAN_ID,
    "plan_hash": PLAN_HASH,
}


class ProductionEvidenceFixture:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.capture_root = self.root / "captures"
        self.capture_dir = self.capture_root / CAPTURE_ID
        self.evidence_dir = self.root / "evidence"
        self.capture_dir.mkdir(parents=True)
        self.evidence_dir.mkdir()

        probe = next(
            item for item in capture.probes_for("bybit") if item.probe == "orderbook"
        )

        def production_row(offset: float, *, bid: float, ask: float) -> dict:
            request_ts = T0 + offset - 0.1
            received_ts = T0 + offset
            payload = bybit_book(bid, ask)
            payload["result"]["ts"] = str(int(received_ts * 1000))
            return {
                "schema": capture.SAMPLE_SCHEMA,
                "capture_id": CAPTURE_ID,
                "venue": "bybit",
                "symbol": "NEWUSDT",
                "probe": "orderbook",
                "t0_ts": T0,
                "request_ts": request_ts,
                "received_ts": received_ts,
                "exchange_ts": received_ts,
                "offset_sec": round(request_ts - T0, 3),
                "payload": payload,
                **capture.request_identity_for(probe, "NEWUSDT"),
            }

        self.rows = [
            production_row(-60.0, bid=100, ask=101),
            production_row(0.0, bid=110, ask=111),
        ]
        readiness = capture.replay_readiness(
            self.rows,
            t0_ts=T0,
            t0_precision_sec=1,
            required_probes=("trades", "orderbook", "ticker"),
        )
        classification = capture.capture_evidence_classification(readiness)
        self.manifest = {
            "schema": "premarket_perp_capture_v1",
            "capture_id": CAPTURE_ID,
            **classification,
            "venue": "bybit",
            "symbol": "NEWUSDT",
            "t0_ts": int(T0),
            "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
            "t0_precision_sec": 1,
            "output_sha256": "",
            "replay_readiness": readiness,
            "plan_id": PLAN_ID,
            "plan_hash": PLAN_HASH,
            "implementation": copy.deepcopy(IMPLEMENTATION),
            "lineage": copy.deepcopy(LINEAGE),
        }
        self.receipt: dict = {}
        self.write_samples()
        self.write_manifest_and_receipt()

    @property
    def receipt_path(self) -> Path:
        return self.evidence_dir / f"{CAPTURE_ID}.json"

    def write_samples(self) -> None:
        raw = "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
            for item in self.rows
        ).encode("utf-8")
        (self.capture_dir / "samples.jsonl").write_bytes(raw)
        self.manifest["output_sha256"] = hashlib.sha256(raw).hexdigest()

    def write_manifest_and_receipt(self) -> None:
        manifest_raw = (
            json.dumps(self.manifest, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8")
        (self.capture_dir / "manifest.json").write_bytes(manifest_raw)
        self.receipt = {
            "schema": "premarket_perp_capture_receipt_v1",
            "capture_id": CAPTURE_ID,
            "evidence_class": self.manifest["evidence_class"],
            "acceptance_capable": self.manifest["acceptance_capable"],
            "venue": "bybit",
            "symbol": "NEWUSDT",
            "t0_ts": int(T0),
            "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
            "output_sha256": self.manifest["output_sha256"],
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "plan_id": PLAN_ID,
            "plan_hash": PLAN_HASH,
            "implementation": copy.deepcopy(IMPLEMENTATION),
            "lineage": copy.deepcopy(self.manifest["lineage"]),
            "replay_readiness": copy.deepcopy(self.manifest["replay_readiness"]),
        }
        for field in (
            "episode_id",
            "venue",
            "premarket_contract_id",
            "spot_symbol",
            "official_spot_t0",
            "t0_source_class",
            "t0_precision_sec",
            "official_record_hash",
            "official_source_url",
            "official_source_identity",
            "registry_sha256",
            "registry_tail_record_hash",
            "mutation_receipt_seq",
            "mutation_receipt_hash",
            "summary_content_sha256",
            "registry_authority_state_hash",
            "plan_id",
            "plan_hash",
            "asset_class",
            "issuer_namespace",
            "issuer_id",
            "asset_identity_hash",
        ):
            self.receipt[field] = self.receipt["lineage"][field]
        self.reseal_receipt()

    def reseal_receipt(self) -> None:
        self.receipt.pop("receipt_hash", None)
        self.receipt["receipt_hash"] = canonical_hash(self.receipt)
        self.receipt_path.write_text(
            json.dumps(self.receipt, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
            newline="",
        )

    def rewrite_manifest_and_reseal_receipt(self) -> None:
        manifest_raw = (
            json.dumps(self.manifest, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8")
        (self.capture_dir / "manifest.json").write_bytes(manifest_raw)
        self.receipt["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
        self.reseal_receipt()


class StrictProductionEvidenceLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ProductionEvidenceFixture()

    def require_new_api(self) -> None:
        self.assertTrue(
            hasattr(replay, "load_replay_evidence"),
            "strict production replay evidence loader is missing",
        )
        self.assertTrue(hasattr(replay, "event_registry"))
        self.assertTrue(hasattr(replay, "risk_gate"))

    @contextmanager
    def verified_dependencies(self):
        self.require_new_api()
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(config, "CAPTURE_ROOT", self.fixture.capture_root)
            )
            stack.enter_context(
                mock.patch.object(config, "EVIDENCE_DIR", self.fixture.evidence_dir)
            )
            lineage = stack.enter_context(
                mock.patch.object(
                    replay.event_registry,
                    "verify_capture_lineage",
                    create=True,
                    return_value={"ok": True, "status": "CAPTURE_LINEAGE_OK"},
                )
            )
            plan = stack.enter_context(
                mock.patch.object(
                    replay.risk_gate,
                    "verify_plan_identity",
                    create=True,
                    return_value={
                        "ok": True,
                        "status": "PLAN_IDENTITY_OK",
                        "evidence_origin_capture_authorized": True,
                        "evidence_origin_write_class": "market_data_capture",
                        "evidence_origin_capture_root": str(self.fixture.capture_root),
                    },
                )
            )
            yield lineage, plan

    def load(self):
        with self.verified_dependencies() as verifiers:
            evidence = replay.load_replay_evidence(self.fixture.capture_dir)
            return evidence, verifiers

    def test_production_load_verifies_every_authority_and_binding(self) -> None:
        evidence, (lineage_verifier, plan_verifier) = self.load()
        self.assertTrue(evidence.production_verified)
        self.assertEqual(evidence.evidence_mode, "PRODUCTION_VERIFIED")
        self.assertEqual(evidence.receipt["capture_id"], CAPTURE_ID)
        self.assertEqual(len(evidence.samples), 2)

        lineage_verifier.assert_called_once()
        combined = lineage_verifier.call_args.args[0]
        for field, value in LINEAGE.items():
            self.assertEqual(combined[field], value)
        self.assertNotIn("implementation", combined)
        plan_verifier.assert_called_once_with(
            plan_id=PLAN_ID,
            plan_hash=PLAN_HASH,
            implementation=IMPLEMENTATION,
            required_write_class="market_data_capture",
        )

    def test_capture_job_lineage_round_trips_through_the_strict_loader(self) -> None:
        event = {
            **LINEAGE,
            "symbol": "NEWUSDT",
            "t0_precision_sec": 1,
            "caveats": [],
        }
        job = capture.job_from_event(event, capture_id=CAPTURE_ID)
        self.fixture.manifest["lineage"] = copy.deepcopy(job.lineage)
        self.fixture.write_manifest_and_receipt()

        evidence, _verifiers = self.load()

        self.assertEqual(evidence.manifest["lineage"]["venue"], job.venue)
        self.assertEqual(evidence.receipt["lineage"], job.lineage)

    def test_resealed_samples_cannot_change_market_identity_or_t0(self) -> None:
        for field, replacement in (
            ("venue", "okx"),
            ("symbol", "OTHERUSDT"),
            ("t0_ts", 123),
        ):
            with self.subTest(field=field):
                fixture = ProductionEvidenceFixture()
                fixture.rows[0][field] = replacement
                fixture.write_samples()
                fixture.write_manifest_and_receipt()
                self.fixture = fixture
                with self.verified_dependencies():
                    with self.assertRaisesRegex(replay.ReplayError, field):
                        replay.load_replay_evidence(fixture.capture_dir)

    def test_resealed_payload_for_another_instrument_is_rejected(self) -> None:
        self.fixture.rows[0]["payload"]["result"]["s"] = "OTHERUSDT"
        self.fixture.write_samples()
        self.fixture.write_manifest_and_receipt()
        with self.verified_dependencies():
            with self.assertRaisesRegex(replay.ReplayError, "market payload"):
                replay.load_replay_evidence(self.fixture.capture_dir)

    def test_resealed_manifest_cannot_self_declare_causal_readiness(self) -> None:
        self.fixture.manifest["replay_readiness"] = {"ready": True}
        self.fixture.manifest["evidence_class"] = "CAUSAL_REPLAY_INPUT_READY"
        self.fixture.write_manifest_and_receipt()
        with self.verified_dependencies():
            with self.assertRaisesRegex(replay.ReplayError, "replay_readiness"):
                replay.load_replay_evidence(self.fixture.capture_dir)

    def test_capture_directory_must_be_a_strict_descendant_of_capture_root(self) -> None:
        self.require_new_api()
        sibling = self.fixture.root / "outside" / CAPTURE_ID
        sibling.parent.mkdir()
        sibling.mkdir()
        with mock.patch.object(config, "CAPTURE_ROOT", self.fixture.capture_root), \
             mock.patch.object(config, "EVIDENCE_DIR", self.fixture.evidence_dir):
            with self.assertRaisesRegex(replay.ReplayError, "strict descendant"):
                replay.load_replay_evidence(sibling)
            with self.assertRaisesRegex(replay.ReplayError, "strict descendant"):
                replay.load_replay_evidence(self.fixture.capture_root)

    def test_production_receipt_is_mandatory_at_the_fixed_evidence_path(self) -> None:
        self.fixture.receipt_path.unlink()
        with self.verified_dependencies():
            with self.assertRaisesRegex(replay.ReplayError, "receipt"):
                replay.load_replay_evidence(self.fixture.capture_dir)

    def test_receipt_canonical_hash_is_verified(self) -> None:
        self.fixture.receipt["capture_id"] = "tampered"
        self.fixture.receipt_path.write_text(
            json.dumps(self.fixture.receipt), encoding="utf-8"
        )
        with self.verified_dependencies():
            with self.assertRaisesRegex(replay.ReplayError, "receipt_hash"):
                replay.load_replay_evidence(self.fixture.capture_dir)

    def test_raw_manifest_and_samples_hashes_are_both_verified(self) -> None:
        with self.subTest(artifact="manifest"):
            manifest_path = self.fixture.capture_dir / "manifest.json"
            manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
            with self.verified_dependencies():
                with self.assertRaisesRegex(replay.ReplayError, "manifest_sha256"):
                    replay.load_replay_evidence(self.fixture.capture_dir)

        self.setUp()
        with self.subTest(artifact="samples"):
            samples_path = self.fixture.capture_dir / "samples.jsonl"
            samples_path.write_bytes(samples_path.read_bytes() + b"{}\n")
            with self.verified_dependencies():
                with self.assertRaisesRegex(replay.ReplayError, "samples|output_sha256"):
                    replay.load_replay_evidence(self.fixture.capture_dir)

    def test_every_artifact_schema_and_sample_capture_id_are_checked(self) -> None:
        cases = (
            ("manifest", lambda: self.fixture.manifest.__setitem__("schema", "wrong")),
            ("receipt", lambda: self.fixture.receipt.__setitem__("schema", "wrong")),
            ("sample", lambda: self.fixture.rows[0].__setitem__("schema", "wrong")),
            ("sample_id", lambda: self.fixture.rows[0].__setitem__("capture_id", "other")),
            (
                "sample_plan",
                lambda: self.fixture.rows[0].__setitem__("plan_id", "other-plan"),
            ),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                self.setUp()
                if label == "receipt":
                    mutate()
                    self.fixture.reseal_receipt()
                else:
                    mutate()
                if label.startswith("sample"):
                    self.fixture.write_samples()
                    self.fixture.write_manifest_and_receipt()
                elif label == "manifest":
                    self.fixture.write_manifest_and_receipt()
                with self.verified_dependencies():
                    with self.assertRaisesRegex(
                        replay.ReplayError, "schema|capture_id|plan"
                    ):
                        replay.load_replay_evidence(self.fixture.capture_dir)

    def test_plan_identity_and_implementation_must_match_manifest_and_receipt(self) -> None:
        cases = (
            ("plan_id", "different-plan"),
            ("plan_hash", "0" * 64),
            ("implementation", {"files": []}),
        )
        for field, value in cases:
            with self.subTest(field=field):
                self.setUp()
                self.fixture.receipt[field] = value
                self.fixture.reseal_receipt()
                with self.verified_dependencies():
                    with self.assertRaisesRegex(
                        replay.ReplayError, "plan|implementation"
                    ):
                        replay.load_replay_evidence(self.fixture.capture_dir)

    def test_production_classification_is_nonacceptance_and_readiness_is_separate(self) -> None:
        with self.subTest(evidence_class="DESCRIPTIVE_ONLY"):
            self.fixture.manifest["evidence_class"] = "DESCRIPTIVE_ONLY"
            self.fixture.write_manifest_and_receipt()
            with self.verified_dependencies():
                evidence = replay.load_replay_evidence(self.fixture.capture_dir)
            self.assertTrue(evidence.production_verified)

        self.setUp()
        with self.subTest(evidence_class="legacy-public-class"):
            self.fixture.manifest["evidence_class"] = "PUBLIC_PREMARKET_PERP_CAPTURE"
            self.fixture.write_manifest_and_receipt()
            with self.verified_dependencies():
                with self.assertRaisesRegex(replay.ReplayError, "evidence_class"):
                    replay.load_replay_evidence(self.fixture.capture_dir)

        self.setUp()
        with self.subTest(acceptance_capable=True):
            self.fixture.manifest["acceptance_capable"] = True
            self.fixture.write_manifest_and_receipt()
            with self.verified_dependencies():
                with self.assertRaisesRegex(replay.ReplayError, "acceptance_capable"):
                    replay.load_replay_evidence(self.fixture.capture_dir)

    def test_required_lineage_is_duplicated_and_crypto_token_only(self) -> None:
        cases = (
            ("missing", "official_record_hash", None),
            ("mismatch", "registry_tail_record_hash", "0" * 64),
            ("wrong_asset", "asset_class", "TOKENIZED_EQUITY"),
            ("bad_seq", "mutation_receipt_seq", True),
        )
        for label, field, value in cases:
            with self.subTest(label=label):
                self.setUp()
                if label == "missing":
                    self.fixture.receipt["lineage"].pop(field)
                else:
                    self.fixture.receipt["lineage"][field] = value
                self.fixture.reseal_receipt()
                with self.verified_dependencies():
                    with self.assertRaisesRegex(
                        replay.ReplayError, "lineage|asset_class|mutation_receipt_seq"
                    ):
                        replay.load_replay_evidence(self.fixture.capture_dir)

    def test_registry_or_plan_verifier_cannot_fail_open(self) -> None:
        self.require_new_api()
        with mock.patch.object(config, "CAPTURE_ROOT", self.fixture.capture_root), \
             mock.patch.object(config, "EVIDENCE_DIR", self.fixture.evidence_dir), \
             mock.patch.object(
                 replay.event_registry,
                 "verify_capture_lineage",
                 create=True,
                 return_value={"ok": False, "status": "LINEAGE_INVALID"},
             ), \
             mock.patch.object(
                 replay.risk_gate,
                 "verify_plan_identity",
                 create=True,
                 return_value={
                     "ok": True,
                     "status": "PLAN_IDENTITY_OK",
                     "evidence_origin_capture_authorized": True,
                     "evidence_origin_write_class": "market_data_capture",
                     "evidence_origin_capture_root": str(self.fixture.capture_root),
                 },
             ):
            with self.assertRaisesRegex(replay.ReplayError, "lineage"):
                replay.load_replay_evidence(self.fixture.capture_dir)

        with mock.patch.object(config, "CAPTURE_ROOT", self.fixture.capture_root), \
             mock.patch.object(config, "EVIDENCE_DIR", self.fixture.evidence_dir), \
             mock.patch.object(
                 replay.event_registry,
                 "verify_capture_lineage",
                 create=True,
                 return_value={"ok": True, "status": "CAPTURE_LINEAGE_OK"},
             ), \
             mock.patch.object(
                 replay.risk_gate,
                 "verify_plan_identity",
                 create=True,
                 return_value={
                     "ok": True,
                     "status": "PLAN_IDENTITY_INVALID",
                     "evidence_origin_capture_authorized": True,
                     "evidence_origin_write_class": "market_data_capture",
                     "evidence_origin_capture_root": str(self.fixture.capture_root),
                 },
             ):
            with self.assertRaisesRegex(replay.ReplayError, "plan identity"):
                replay.load_replay_evidence(self.fixture.capture_dir)

        with mock.patch.object(config, "CAPTURE_ROOT", self.fixture.capture_root), \
             mock.patch.object(config, "EVIDENCE_DIR", self.fixture.evidence_dir), \
             mock.patch.object(
                 replay.event_registry,
                 "verify_capture_lineage",
                 create=True,
                 return_value={"ok": True, "status": "CAPTURE_LINEAGE_OK"},
             ), \
             mock.patch.object(
                 replay.risk_gate,
                 "verify_plan_identity",
                 create=True,
                 return_value={
                     "ok": True,
                     "status": "PLAN_IDENTITY_OK",
                     "evidence_origin_capture_authorized": False,
                     "evidence_origin_write_class": "market_data_capture",
                     "evidence_origin_capture_root": str(self.fixture.capture_root),
                 },
             ):
            with self.assertRaisesRegex(replay.ReplayError, "capture authority"):
                replay.load_replay_evidence(self.fixture.capture_dir)

        relocated_root = self.fixture.root / "different-plan-root"
        with mock.patch.object(config, "CAPTURE_ROOT", self.fixture.capture_root), \
             mock.patch.object(config, "EVIDENCE_DIR", self.fixture.evidence_dir), \
             mock.patch.object(
                 replay.event_registry,
                 "verify_capture_lineage",
                 create=True,
                 return_value={"ok": True, "status": "CAPTURE_LINEAGE_OK"},
             ), \
             mock.patch.object(
                 replay.risk_gate,
                 "verify_plan_identity",
                 create=True,
                 return_value={
                     "ok": True,
                     "status": "PLAN_IDENTITY_OK",
                     "evidence_origin_capture_authorized": True,
                     "evidence_origin_write_class": "market_data_capture",
                     "evidence_origin_capture_root": str(relocated_root),
                 },
             ):
            with self.assertRaisesRegex(replay.ReplayError, "historical PlanOnly capture root"):
                replay.load_replay_evidence(self.fixture.capture_dir)

    def test_replay_capture_uses_verified_production_loader(self) -> None:
        with self.verified_dependencies():
            report = replay.replay_capture(self.fixture.capture_dir)
        self.assertTrue(report["evidence_verification"]["production_verified"])
        self.assertEqual(
            report["evidence_verification"]["mode"], "PRODUCTION_VERIFIED"
        )
        self.assertEqual(report["research_classification"], "DESCRIPTIVE_ONLY")
        self.assertNotIn("acceptance_decision", report)

    def test_production_replay_requires_the_preregistered_targets(self) -> None:
        with self.verified_dependencies():
            for horizons, entry_lead in (
                ((), config.PRIMARY_ENTRY_LEAD_SEC),
                ((0,), config.PRIMARY_ENTRY_LEAD_SEC),
                (config.PRIMARY_EXIT_OFFSETS_SEC, 0),
                (config.PRIMARY_EXIT_OFFSETS_SEC, -1),
                (config.PRIMARY_EXIT_OFFSETS_SEC, config.PRIMARY_ENTRY_LEAD_SEC + 1),
            ):
                with self.subTest(horizons=horizons, entry_lead=entry_lead):
                    with self.assertRaisesRegex(
                        replay.ReplayError,
                        "preregistered|entry lead|positive|horizons",
                    ):
                        replay.replay_capture(
                            self.fixture.capture_dir,
                            horizons_sec=horizons,
                            entry_lead_sec=entry_lead,
                        )

    def test_replay_readiness_cannot_exceed_the_sealed_capture_readiness(self) -> None:
        self.assertFalse(self.fixture.manifest["replay_readiness"]["ready"])
        with self.verified_dependencies():
            report = replay.replay_capture(self.fixture.capture_dir)
        self.assertFalse(report["causal_replay_readiness"]["ready"])
        self.assertFalse(
            report["causal_replay_readiness"]["sealed_capture_ready"]
        )

    def test_manifest_precision_must_match_the_official_lineage(self) -> None:
        self.fixture.manifest["t0_precision_sec"] = 60
        self.fixture.write_manifest_and_receipt()
        with self.verified_dependencies():
            with self.assertRaisesRegex(replay.ReplayError, "t0_precision_sec"):
                replay.load_replay_evidence(self.fixture.capture_dir)

    def test_synthetic_fixture_requires_explicit_nonacceptance_mode(self) -> None:
        synthetic = write_capture([valid_row(-60), valid_row(0)])
        self.require_new_api()
        with mock.patch.object(config, "CAPTURE_ROOT", self.fixture.capture_root), \
             mock.patch.object(config, "EVIDENCE_DIR", self.fixture.evidence_dir):
            with self.assertRaisesRegex(replay.ReplayError, "strict descendant"):
                replay.replay_capture(synthetic)
            report = replay.replay_capture(
                synthetic,
                horizons_sec=(0,),
                evidence_mode="SYNTHETIC_DESCRIPTIVE_ONLY",
            )
        self.assertFalse(report["evidence_verification"]["production_verified"])
        self.assertTrue(report["evidence_verification"]["nonacceptance_only"])
        self.assertEqual(report["research_classification"], "DESCRIPTIVE_ONLY")

    def test_a_production_root_capture_cannot_be_downgraded_to_synthetic(self) -> None:
        self.require_new_api()
        with mock.patch.object(config, "CAPTURE_ROOT", self.fixture.capture_root), \
             mock.patch.object(config, "EVIDENCE_DIR", self.fixture.evidence_dir):
            with self.assertRaisesRegex(replay.ReplayError, "cannot be downgraded"):
                replay.load_replay_evidence(
                    self.fixture.capture_dir,
                    evidence_mode="SYNTHETIC_DESCRIPTIVE_ONLY",
                )


if __name__ == "__main__":
    unittest.main()
