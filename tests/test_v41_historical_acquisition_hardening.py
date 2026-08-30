"""Contracts for v42 historical-acquisition authority and durability.

Every transport and writer-claim seam is mocked.  Files created by these tests live
only below a temporary directory; the suite never opens a network connection or
writes a production/research artifact.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import historical_acquisition as acquisition  # noqa: E402
import risk_gate  # noqa: E402


T0 = 1_800_000_000
RECEIVED_AT_UTC = "2027-01-16T08:00:00Z"
WRITE_CLASS = "historical_market_data_acquisition"
PREFLIGHT_DECISION = "ALLOW_HISTORICAL_ACQUISITION"
HISTORICAL_ACTION = (
    "acquire bounded historical public pre-market evidence into a separate "
    "append-only namespace"
)
V42_PLAN_PATH = (
    ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v42.json"
)
OFFICIAL_ASSERTION_FIELDS = (
    "venue",
    "premarket_contract_id",
    "spot_symbol",
    "official_spot_t0",
    "t0_source_class",
    "official_source_url",
)
OFFICIAL_SOURCE_URLS = {
    "bybit": "https://announcements.bybit.com/en/article/new-listing",
    "okx": "https://www.okx.com/en-us/help/new-listing",
    "gate": "https://www.gate.com/announcements/article/12345",
}
CONTRACTS = {
    "bybit": "NEWUSDT",
    "okx": "NEW-USDT-SWAP",
    "gate": "NEW_USDT",
}


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _official_assertion_hash(seed: dict[str, object]) -> str:
    return _canonical_sha256({field: seed[field] for field in OFFICIAL_ASSERTION_FIELDS})


def _seed(venue: str = "bybit") -> dict[str, object]:
    seed: dict[str, object] = {
        "schema": "premarket_perp_historical_seed_v1",
        "event_id": f"historical-{venue}-new-{T0}",
        "venue": venue,
        "listing_venue": venue,
        "premarket_contract_id": CONTRACTS[venue],
        "spot_symbol": "NEWUSDT" if venue != "okx" else "NEW-USDT",
        "asset_class": "CRYPTO_TOKEN",
        "premarket_contract_launch_ts": T0 - 7_200,
        "official_spot_t0": T0,
        "first_trade_ts": None,
        "transition_ts": None,
        "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
        "t0_precision_sec": 1,
        "official_source_url": OFFICIAL_SOURCE_URLS[venue],
        "history_start_ts": T0 - 60,
        "history_end_ts": T0 + 60,
    }
    seed["official_record_hash"] = _official_assertion_hash(seed)
    return seed


def _fixture_payload(venue: str = "bybit") -> object:
    if venue == "bybit":
        return {
            "retCode": 0,
            "result": {
                "category": "linear",
                "symbol": CONTRACTS[venue],
                "list": [
                    [str(T0 * 1000), "100", "111", "99", "110", "10", "1050"]
                ],
            },
        }
    if venue == "okx":
        return {
            "code": "0",
            "data": [[
                str(T0 * 1000),
                "100",
                "111",
                "99",
                "110",
                "10",
                "10",
                "1050",
                "1",
            ]],
        }
    return {
        "archive_schema": "gate_futures_candlesticks_1m_v1",
        "rows": [[str(T0), "10", "110", "111", "99", "100"]],
    }


def _allowed_preflight(run_id: str) -> dict[str, object]:
    return {
        "schema": risk_gate.PREFLIGHT_RESULT_SCHEMA,
        "ok": True,
        "verified": True,
        "decision": PREFLIGHT_DECISION,
        "write_class": WRITE_CLASS,
        "run_id": run_id,
        "action": HISTORICAL_ACTION,
        "plan_id": "premarket_perp_capture_20260822_v42",
        "plan_hash": "e" * 64,
    }


class HistoricalAcquisitionV42HardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.roots = acquisition.HistoricalAcquisitionRoots(
            raw_root=self.base / "raw",
            manifest_root=self.base / "manifests",
            receipt_root=self.base / "receipts",
        )
        self.claim_path = self.base / "control" / "active-market-writer.json"
        self.claim_archive = self.base / "control" / "claim-archive"
        self.limits = acquisition.HistoricalAcquisitionLimits(
            max_events=3,
            max_requests=3,
            max_runtime_sec=30,
            max_retries=0,
        )

    def _run_with_authority(
        self,
        *,
        run_id: str,
        seed: dict[str, object],
        transport: mock.Mock,
        extra_patches: tuple[object, ...] = (),
    ) -> tuple[dict[str, object] | None, BaseException | None, mock.Mock]:
        release = mock.Mock(return_value=self.claim_archive / f"{run_id}.json")
        result: dict[str, object] | None = None
        error: BaseException | None = None
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(acquisition.config, "SHARED_WRITER_CLAIM_PATH", self.claim_path)
            )
            stack.enter_context(
                mock.patch.object(acquisition.config, "CLAIM_ARCHIVE_DIR", self.claim_archive)
            )
            stack.enter_context(
                mock.patch.object(
                    acquisition.risk_gate,
                    "preflight",
                    return_value=_allowed_preflight(run_id),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    acquisition.risk_gate,
                    "read_shared_gate",
                    return_value={"open": True, "status": "READY_FOR_POSTPROCESS"},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    acquisition.writer_claim,
                    "claim_global_market_writer",
                    return_value={"owner_pid": 123, "ownership_token": "1" * 32},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    acquisition.writer_claim,
                    "release_global_market_writer",
                    release,
                )
            )
            for patcher in extra_patches:
                stack.enter_context(patcher)
            try:
                result = acquisition.run_historical_acquisition(
                    run_id=run_id,
                    seeds=[seed],
                    roots=self.roots,
                    limits=self.limits,
                    transport=transport,
                    received_at_utc=RECEIVED_AT_UTC,
                )
            except Exception as exc:  # retain the error while still asserting cleanup
                error = exc
        return result, error, release

    def test_cli_accepts_only_the_exact_planonly_seed_path_and_file_sha(self) -> None:
        plan = json.loads(V42_PLAN_PATH.read_text(encoding="utf-8"))
        binding = next(
            row
            for row in plan["implementation"]["files"]
            if row["role"] == "historical_event_seeds"
        )
        exact_seed_path = (ROOT / binding["repo_path"]).resolve()
        alternate = self.base / "byte-identical-but-unbound-seeds.json"
        alternate.write_bytes(exact_seed_path.read_bytes())

        cases = (
            ("wrong_path", plan, alternate),
            ("wrong_sha", copy.deepcopy(plan), exact_seed_path),
        )
        cases[1][1]["implementation"]["files"] = [
            ({**row, "sha256": "0" * 64} if row["role"] == "historical_event_seeds" else row)
            for row in cases[1][1]["implementation"]["files"]
        ]

        for name, active_plan, seed_path in cases:
            with self.subTest(case=name):
                terminal = {
                    "status": "HISTORICAL_ACQUISITION_COMPLETE",
                    "pending_retry": False,
                }
                with (
                    mock.patch.object(
                        acquisition.risk_gate,
                        "load_and_verify_plan",
                        return_value=active_plan,
                    ) as load_plan,
                    mock.patch.object(
                        acquisition,
                        "run_historical_acquisition",
                        return_value=terminal,
                    ) as run,
                    mock.patch("sys.stdout", new_callable=io.StringIO),
                ):
                    exit_code = acquisition.main([
                        "--seed-file",
                        str(seed_path),
                        "--run-id",
                        f"cli-{name}",
                        "--received-at-utc",
                        RECEIVED_AT_UTC,
                    ])

                self.assertEqual(exit_code, 2)
                load_plan.assert_called()
                run.assert_not_called()

    def test_seed_binding_identity_must_match_fresh_preflight_before_claim(self) -> None:
        preflight = _allowed_preflight("plan-race")
        claim = mock.Mock()
        transport = mock.Mock()
        with (
            mock.patch.object(acquisition.risk_gate, "preflight", return_value=preflight),
            mock.patch.object(acquisition.writer_claim, "claim_global_market_writer", claim),
        ):
            with self.assertRaisesRegex(
                acquisition.HistoricalAcquisitionError,
                "seed binding plan identity changed",
            ):
                acquisition.run_historical_acquisition(
                    run_id="plan-race",
                    seeds=[_seed()],
                    roots=self.roots,
                    limits=self.limits,
                    transport=transport,
                    received_at_utc=RECEIVED_AT_UTC,
                    expected_plan_id="premarket_perp_capture_20260822_v40",
                    expected_plan_hash="d" * 64,
                )

        claim.assert_not_called()
        transport.assert_not_called()

    def test_cli_serializes_writer_claim_collision_as_retry_json(self) -> None:
        bound = {
            "schema": "premarket_perp_historical_seed_set_v1",
            "evidence_use": "DESCRIPTIVE_ONLY",
            "events": [_seed()],
            "_bound_plan_id": "premarket_perp_capture_20260822_v42",
            "_bound_plan_hash": "e" * 64,
        }
        with (
            mock.patch.object(acquisition, "_load_bound_seed_set", return_value=bound),
            mock.patch.object(
                acquisition,
                "run_historical_acquisition",
                side_effect=acquisition.writer_claim.GlobalMarketWriterClaimError(
                    "global writer already held"
                ),
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            exit_code = acquisition.main([
                "--seed-file",
                str(V42_PLAN_PATH),
                "--run-id",
                "claim-collision",
                "--received-at-utc",
                RECEIVED_AT_UTC,
            ])

        self.assertEqual(exit_code, 2)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "RETRY_NEXT_INTERVAL")
        self.assertTrue(result["pending_retry"])
        self.assertIn("GlobalMarketWriterClaimError", result["error"])

    def test_official_assertion_hash_class_precision_and_host_block_before_transport(self) -> None:
        invalid: list[tuple[str, dict[str, object]]] = []

        bad_hash = _seed()
        bad_hash["official_record_hash"] = "0" * 64
        invalid.append(("hash", bad_hash))

        bad_class = _seed()
        bad_class["t0_source_class"] = "UNVERIFIED_ANNOUNCEMENT_DISCOVERY"
        bad_class["official_record_hash"] = _official_assertion_hash(bad_class)
        invalid.append(("source_class", bad_class))

        bad_precision = _seed()
        bad_precision["t0_precision_sec"] = 60
        invalid.append(("precision", bad_precision))

        bad_host = _seed()
        bad_host["official_source_url"] = (
            "https://announcements.bybit.com.evil.test/en/article/new-listing"
        )
        bad_host["official_record_hash"] = _official_assertion_hash(bad_host)
        invalid.append(("host", bad_host))

        for index, (name, seed) in enumerate(invalid):
            with self.subTest(field=name):
                transport = mock.Mock(return_value=_fixture_payload())
                self._run_with_authority(
                    run_id=f"invalid-official-{index}",
                    seed=seed,
                    transport=transport,
                )
                transport.assert_not_called()

    def test_gate_archive_rows_are_normalized_oldest_first(self) -> None:
        archive = gzip.compress(
            (
                f"{T0 + 60},3,112,114,109,110\n"
                f"{T0},2,110,111,99,100\n"
                f"{T0 - 60},1,99,101,98,100\n"
            ).encode("utf-8")
        )
        with mock.patch.object(
            acquisition.public_http,
            "get_bytes",
            return_value=archive,
        ) as get_bytes:
            payload = acquisition.default_public_transport(
                acquisition.OHLCV_ENDPOINTS["gate"],
                {"from": T0 - 60, "to": T0 + 60},
                timeout_sec=20,
                max_retries=0,
            )

        self.assertEqual(
            [row[0] for row in payload["rows"]],
            [str(T0 - 60), str(T0), str(T0 + 60)],
        )
        get_bytes.assert_called_once()

    def test_preflight_is_exact_historical_authority_and_never_a_capture_token(self) -> None:
        plan = json.loads(V42_PLAN_PATH.read_text(encoding="utf-8"))
        free = {"present": False, "blocks": False, "stale": False}
        with (
            mock.patch.object(risk_gate, "load_and_verify_plan", return_value=plan),
            mock.patch.object(risk_gate, "verify_resolved_path_bindings", return_value={}),
            mock.patch.object(
                risk_gate,
                "run_capability_scan",
                return_value={"status": "CAPABILITY_SCAN_CLEAN"},
            ),
            mock.patch.object(
                risk_gate,
                "read_shared_gate",
                return_value={"open": True, "status": "READY_FOR_POSTPROCESS"},
            ),
            mock.patch.object(risk_gate, "inspect_claim", return_value=free),
            mock.patch.object(risk_gate, "inspect_run_record", return_value=free),
            mock.patch.object(
                risk_gate,
                "mint_capture_token",
                side_effect=AssertionError("historical acquisition has no capture token"),
            ) as mint_token,
        ):
            receipt = risk_gate.preflight(
                write_class=WRITE_CLASS,
                run_id="historical-preflight-v42",
            )

        self.assertEqual(receipt["decision"], PREFLIGHT_DECISION)
        self.assertEqual(receipt["write_class"], WRITE_CLASS)
        self.assertEqual(receipt["action"], HISTORICAL_ACTION)
        self.assertNotIn("capture_token", receipt)
        mint_token.assert_not_called()

        acquisition._validate_preflight(receipt)
        near_misses = {
            "decision": {**receipt, "decision": "ALLOW_VISIBLE_CAPTURE"},
            "write_class": {**receipt, "write_class": "market_data_capture"},
            "action": {**receipt, "action": risk_gate.CAPTURE_ACTION},
            "capture_token": {**receipt, "capture_token": "must-not-exist"},
        }
        for field, candidate in near_misses.items():
            with self.subTest(rejected_field=field):
                with self.assertRaises(acquisition.HistoricalAcquisitionError):
                    acquisition._validate_preflight(candidate)

    def test_receipt_write_fsync_or_readback_failure_releases_claim_as_retry_or_error(self) -> None:
        real_write = acquisition._write_json_exclusive
        real_read_bytes = Path.read_bytes

        def fail_receipt_write(path: Path, payload: dict[str, object]) -> None:
            if path.parent == self.roots.receipt_root:
                raise OSError("injected receipt write failure")
            real_write(path, payload)

        fsync_calls = 0

        def fail_receipt_fsync(_descriptor: int) -> None:
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 3:
                raise OSError("injected receipt fsync failure")

        def fail_receipt_readback(path: Path) -> bytes:
            if path.parent == self.roots.receipt_root:
                raise OSError("injected receipt readback failure")
            return real_read_bytes(path)

        cases = (
            (
                "write",
                mock.patch.object(
                    acquisition,
                    "_write_json_exclusive",
                    side_effect=fail_receipt_write,
                ),
            ),
            ("fsync", mock.patch.object(acquisition.os, "fsync", side_effect=fail_receipt_fsync)),
            (
                "readback",
                mock.patch.object(
                    Path,
                    "read_bytes",
                    autospec=True,
                    side_effect=fail_receipt_readback,
                ),
            ),
        )

        for index, (name, failure_patch) in enumerate(cases):
            with self.subTest(failure=name):
                result, error, release = self._run_with_authority(
                    run_id=f"receipt-{name}-{index}",
                    seed=_seed(),
                    transport=mock.Mock(return_value=_fixture_payload()),
                    extra_patches=(failure_patch,),
                )

                release.assert_called_once()
                final_status = release.call_args.kwargs["final_status"]
                self.assertNotEqual(final_status, "HISTORICAL_ACQUISITION_COMPLETE")
                self.assertRegex(str(final_status), r"RETRY|ERROR")
                if result is not None:
                    self.assertNotEqual(
                        result.get("status"),
                        "HISTORICAL_ACQUISITION_COMPLETE",
                    )
                    self.assertIs(result.get("pending_retry"), True)
                else:
                    self.assertIsNotNone(error)


if __name__ == "__main__":
    unittest.main()
