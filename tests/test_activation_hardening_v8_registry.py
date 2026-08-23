"""Offline regressions for the v8 registry/attestation hardening package.

The suite uses only temporary files and already-built observations.  It never calls
venue adapters, refresh, capture, or the network.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


TESTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(TESTS_ROOT))

import event_registry as registry  # noqa: E402
import official_attestation as attestation  # noqa: E402
import project_config as config  # noqa: E402
import test_activation_hardening_v6_registry as v6  # noqa: E402
import test_activation_hardening_v7_registry as v7  # noqa: E402


def _write_registry(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _receipt_dir(path: Path) -> Path:
    return path.with_name(path.name + ".mutation-receipts")


@contextmanager
def _production_registry(records: list[dict], *, refresh_at: str | None = None):
    root = Path(tempfile.mkdtemp())
    path = root / "listing-events-v2.jsonl"
    lock_path = root / "listing-events-v2.lock"
    _write_registry(path, records)
    v6._write_refresh_summary(
        path,
        records,
        last_complete_metadata_refresh_received_at=refresh_at,
    )
    with mock.patch.object(registry, "REGISTRY_PATH", path), mock.patch.object(
        registry, "REGISTRY_LOCK_PATH", lock_path
    ):
        yield path


def _attest(path: Path, *, run_id: str, lifecycle_generation: int = 0) -> dict:
    return attestation.attest(
        path=path,
        run_id=run_id,
        venue="bybit",
        spot_symbol=v6.SPOT_SYMBOL,
        premarket_contract_id=v6.CONTRACT_ID,
        lifecycle_generation=lifecycle_generation,
        announced_utc=v6.ANNOUNCED_UTC,
        announcement_url=v6.SOURCE_URL,
        quoted_sentence=v6.QUOTE,
        quoted_time_text=v6.QUOTED_TIME,
        quoted_symbol_text=v6.QUOTED_SYMBOL,
        attested_by="registry-v8-test",
    )


class ExactCadenceTests(unittest.TestCase):
    def test_selection_uses_configured_thirty_second_early_grace(self) -> None:
        records = v6._records()
        window_start = v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC

        with _production_registry(
            records,
            refresh_at=v7._iso(
                window_start - config.CAPTURE_LAUNCH_EARLY_GRACE_SEC - 1
            ),
        ):
            too_early = registry.events_for_capture(
                now_ts=window_start - config.CAPTURE_LAUNCH_EARLY_GRACE_SEC - 1,
                source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            )
            on_boundary = registry.events_for_capture(
                now_ts=window_start - config.CAPTURE_LAUNCH_EARLY_GRACE_SEC,
                source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            )

        self.assertEqual(too_early, [])
        self.assertEqual(len(on_boundary), 1)

    def test_selection_uses_configured_five_second_late_grace(self) -> None:
        records = v6._records()
        window_start = v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC

        with _production_registry(records):
            on_boundary = registry.events_for_capture(
                now_ts=window_start + config.CAPTURE_LAUNCH_LATE_GRACE_SEC,
                source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            )
            too_late = registry.events_for_capture(
                now_ts=window_start + config.CAPTURE_LAUNCH_LATE_GRACE_SEC + 1,
                source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            )

        self.assertEqual(len(on_boundary), 1)
        self.assertEqual(too_late, [])


class VerbatimQuotationTests(unittest.TestCase):
    def test_control_character_rewriting_is_rejected(self) -> None:
        cases = {
            "sentence-newline": {
                "quoted_sentence": v6.QUOTE.replace(" will ", "\nwill "),
            },
            "time-tab": {
                "quoted_sentence": v6.QUOTE.replace("Jan 15", "Jan\t15"),
                "quoted_time_text": v6.QUOTED_TIME.replace("Jan 15", "Jan\t15"),
            },
            "symbol-carriage-return": {
                "quoted_sentence": v6.QUOTE.replace("KII/USDT", "KII/\rUSDT"),
                "quoted_symbol_text": "KII/\rUSDT",
            },
        }
        for name, overrides in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(
                attestation.AttestationError, r"quote|quoted|canonical|control|whitespace"
            ):
                v6._build_attestation(**overrides)

    def test_sentence_and_fragments_are_stored_byte_for_byte(self) -> None:
        sentence = v6.QUOTE.replace("Spot trading", "Spot  trading")

        observation = v6._build_attestation(quoted_sentence=sentence)

        evidence = observation["attestation"]
        self.assertEqual(evidence["quoted_sentence"], sentence)
        self.assertEqual(evidence["quoted_time_text"], v6.QUOTED_TIME)
        self.assertEqual(evidence["quoted_symbol_text"], v6.QUOTED_SYMBOL)

    def test_registry_rejects_control_rewritten_attestation_evidence(self) -> None:
        official = v6._official_observation()
        official["attestation"] = dict(
            official["attestation"],
            quoted_sentence=v6.QUOTE.replace(" will ", "\nwill "),
        )
        records = registry.build_stream_revisions(
            [], [v6._metadata_observation(), official]
        )
        path = Path(tempfile.mkdtemp()) / "registry.jsonl"
        _write_registry(path, records)

        report = registry.verify_registry(path)

        self.assertEqual(report["status"], "REGISTRY_PROBLEMS")
        self.assertRegex("; ".join(report["problems"]), r"quote|attestation|control")


class ImmutableMutationAnchorTests(unittest.TestCase):
    def test_coordinated_registry_and_summary_rollback_without_receipt_fails(self) -> None:
        root = Path(tempfile.mkdtemp())
        path = root / "listing-events-v2.jsonl"
        lock_path = root / "listing-events-v2.lock"
        initial = registry.build_stream_revisions([], [v6._metadata_observation()])
        _write_registry(path, initial)
        v6._write_refresh_summary(
            path,
            initial,
            last_complete_metadata_refresh_received_at=v7._iso(
                v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC
            ),
        )
        initial_summary = path.with_suffix(".summary.json").read_bytes()

        with mock.patch.object(
            attestation.risk_gate,
            "preflight",
            return_value=v7._valid_attestation_preflight("anchor-v8"),
        ), mock.patch.object(
            attestation.time,
            "time",
            return_value=v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC,
        ):
            _attest(path, run_id="anchor-v8")

        receipts = sorted(_receipt_dir(path).glob("*.json"))
        self.assertEqual(len(receipts), 2, "each committed mutation needs one O_EXCL receipt")
        with self.assertRaises(FileExistsError):
            descriptor = os.open(receipts[0], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)

        # A coordinated attacker rewrites both mutable files to an older, internally
        # consistent state but cannot replace the immutable receipt for the true tail.
        _write_registry(path, initial)
        path.with_suffix(".summary.json").write_bytes(initial_summary)
        with mock.patch.object(registry, "REGISTRY_PATH", path), mock.patch.object(
            registry, "REGISTRY_LOCK_PATH", lock_path
        ):
            report = registry.verify_registry(path)

        self.assertEqual(report["status"], "REGISTRY_PROBLEMS")
        self.assertRegex("; ".join(report["problems"]), r"mutation receipt|anchor|summary")


class MutationTransactionRecoveryTests(unittest.TestCase):
    def test_summary_failure_rolls_registry_and_summary_back_under_lock(self) -> None:
        root = Path(tempfile.mkdtemp())
        path = root / "listing-events-v2.jsonl"
        initial = registry.build_stream_revisions([], [v6._metadata_observation()])
        _write_registry(path, initial)
        v6._write_refresh_summary(
            path,
            initial,
            last_complete_metadata_refresh_received_at=v7._iso(
                v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC
            ),
        )
        registry_before = path.read_bytes()
        summary_path = path.with_suffix(".summary.json")
        summary_before = summary_path.read_bytes()
        receipts_before = sorted(item.name for item in _receipt_dir(path).glob("*.json"))

        with mock.patch.object(
            attestation.risk_gate,
            "preflight",
            return_value=v7._valid_attestation_preflight("rollback-v8"),
        ), mock.patch.object(
            attestation.time,
            "time",
            return_value=v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC,
        ), mock.patch.object(
            registry, "_write_json_atomic", side_effect=OSError("summary fsync failed")
        ):
            with self.assertRaisesRegex(OSError, "summary fsync failed"):
                _attest(path, run_id="rollback-v8")

        self.assertEqual(path.read_bytes(), registry_before)
        self.assertEqual(summary_path.read_bytes(), summary_before)
        self.assertEqual(
            sorted(item.name for item in _receipt_dir(path).glob("*.json")),
            receipts_before,
        )


class IdempotentLateRetryTests(unittest.TestCase):
    def test_exact_duplicate_retry_after_lead_boundary_returns_existing_hash(self) -> None:
        root = Path(tempfile.mkdtemp())
        path = root / "registry.jsonl"
        initial = registry.build_stream_revisions([], [v6._metadata_observation()])
        _write_registry(path, initial)
        v6._write_refresh_summary(
            path,
            initial,
            last_complete_metadata_refresh_received_at=v7._iso(
                v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC
            ),
        )
        clocks = iter(
            [
                v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC,
                v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC,
                v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC + 1,
            ]
        )

        with mock.patch.object(
            attestation.risk_gate,
            "preflight",
            side_effect=lambda *, write_class, run_id: v7._valid_attestation_preflight(
                run_id
            ),
        ), mock.patch.object(attestation.time, "time", side_effect=lambda: next(clocks)):
            first = _attest(path, run_id="late-retry-first-v8")
            second = _attest(path, run_id="late-retry-second-v8")

        self.assertEqual(first["status"], "ATTESTED")
        self.assertEqual(second["status"], "ALREADY_RECORDED")
        self.assertEqual(second["official_record_hash"], first["official_record_hash"])


class ExplicitActiveLifecycleGenerationTests(unittest.TestCase):
    def test_cli_requires_an_explicit_lifecycle_generation(self) -> None:
        argv = [
            "--attest",
            "--run-id", "cli-v8",
            "--venue", "bybit",
            "--spot-symbol", v6.SPOT_SYMBOL,
            "--premarket-contract-id", v6.CONTRACT_ID,
            "--announced-utc", v6.ANNOUNCED_UTC,
            "--announcement-url", v6.SOURCE_URL,
            "--quote", v6.QUOTE,
            "--quoted-time", v6.QUOTED_TIME,
            "--quoted-symbol", v6.QUOTED_SYMBOL,
            "--attested-by", "registry-v8-test",
        ]

        with mock.patch.object(attestation, "attest", return_value={}) as write:
            with self.assertRaisesRegex(SystemExit, "lifecycle-generation"):
                attestation.main(argv)

        write.assert_not_called()

    def test_new_attestation_requires_the_current_active_generation(self) -> None:
        root = Path(tempfile.mkdtemp())
        path = root / "registry.jsonl"
        metadata_zero = v6._metadata_observation()
        generation = 1
        episode_id = registry.make_episode_id("bybit", v6.CONTRACT_ID, generation)
        metadata_one = registry.make_timestamp_observation(
            episode_id=episode_id,
            venue="bybit",
            premarket_contract_id=v6.CONTRACT_ID,
            spot_symbol=v6.SPOT_SYMBOL,
            timestamp_kind=registry.TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
            timestamp_ts=v6._t0_ts() - 6 * 24 * 3600,
            instrument_role="premarket_perp",
            source_class=registry.SOURCE_VENUE_INSTRUMENT_METADATA,
            source_identity="bybit:instrument_metadata:launchTime",
            source_url="https://api.bybit.com/v5/market/instruments-info",
            received_at_utc="2027-01-09T04:00:00Z",
            precision_sec=1,
            lifecycle_generation=generation,
        )
        initial = registry.build_stream_revisions([], [metadata_zero, metadata_one])
        _write_registry(path, initial)
        v6._write_refresh_summary(path, initial)

        with mock.patch.object(
            attestation.risk_gate,
            "preflight",
            return_value=v7._valid_attestation_preflight("stale-generation-v8"),
        ), mock.patch.object(
            attestation.time,
            "time",
            return_value=v6._t0_ts() - config.CAPTURE_WINDOW_BEFORE_SEC,
        ):
            with self.assertRaisesRegex(
                attestation.AttestationError, r"active.*generation|generation.*active"
            ):
                _attest(path, run_id="stale-generation-v8", lifecycle_generation=0)

        self.assertEqual(len(registry.load_registry(path)), 2)


if __name__ == "__main__":
    unittest.main()
