"""RED contract for bounded post-hoc OHLCV acquisition.

Historical acquisition is a public-data writer, not a forward capture and not an
execution path.  The transport is injected in every test.  Production may later wire
that seam to ``public_http``, but this test suite never opens a network connection.

The output is three separate create-only namespaces: raw public responses, derived
DESCRIPTIVE_ONLY manifests, and terminal acquisition receipts.  Shared-gate and
global-writer authority must be held before even those directories are created.
"""

from __future__ import annotations

import ast
import copy
import gzip
import hashlib
import importlib
import importlib.util
import inspect
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

import public_http  # noqa: E402


MODULE_SPEC = importlib.util.find_spec("historical_acquisition")

T0 = 1_800_000_000
RECEIVED_AT_UTC = "2027-01-16T08:00:00Z"
RETRIEVED_AT_TS = T0 + 86_400

EXPECTED_ENDPOINTS = {
    "bybit": "https://api.bybit.com/v5/market/kline",
    "okx": "https://www.okx.com/api/v5/market/history-candles",
    "gate": (
        "https://download.gatedata.org/futures_usdt/candlesticks_1m/"
        "202602/AZTEC_USDT-202602.csv.gz"
    ),
}
CONTRACTS = {
    "bybit": "NEWUSDT",
    "okx": "NEW-USDT-SWAP",
    "gate": "NEW_USDT",
}


def _canonical_sha256(value: object) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _seed(venue: str, *, suffix: str = "") -> dict[str, object]:
    return {
        "schema": "premarket_perp_historical_seed_v1",
        "event_id": f"historical-{venue}-new-{T0}{suffix}",
        "venue": venue,
        "listing_venue": venue,
        "premarket_contract_id": CONTRACTS[venue],
        "spot_symbol": "NEWUSDT",
        "asset_class": "CRYPTO_TOKEN",
        "premarket_contract_launch_ts": T0 - 7_200,
        "official_spot_t0": T0,
        "first_trade_ts": None,
        "transition_ts": None,
        "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
        "t0_precision_sec": 1,
        "official_source_url": f"https://announcements.example.test/{venue}/new",
        "official_record_hash": "a" * 64,
        "history_start_ts": T0 - 60,
        "history_end_ts": T0 + 60,
    }


def _fixture_payload(venue: str) -> object:
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
    return [[str(T0), "1050", "110", "111", "99", "100", "10"]]


def _fake_manifest_builder(
    seed: dict[str, object],
    venue_payload: object,
    retrieved_at_ts: int,
) -> dict[str, object]:
    manifest: dict[str, object] = {
        "schema": "premarket_perp_historical_event_v1",
        "event_id": seed["event_id"],
        "venue": seed["venue"],
        "premarket_contract_id": seed["premarket_contract_id"],
        "official_spot_t0": seed["official_spot_t0"],
        "t0_source_class": seed["t0_source_class"],
        "history_source_class": seed["history_source_class"],
        "history_source_url": seed["history_source_url"],
        "history_request_params": copy.deepcopy(seed["history_request_params"]),
        "retrieved_at_ts": retrieved_at_ts,
        "raw_payload_sha256": _canonical_sha256(venue_payload),
        "evidence_use": "DESCRIPTIVE_ONLY",
        "price_evidence_class": "POSTHOC_OHLCV_NOT_EXECUTION_EVIDENCE",
        "posthoc_retrieval": True,
        "acceptance_capable": False,
        "capture_eligible": False,
        "execution_evidence": False,
        "orders_allowed": False,
        "candle_count": 1,
        "candles": [{
            "open_ts": T0,
            "open": "100",
            "high": "111",
            "low": "99",
            "close": "110",
            "closed": True,
        }],
    }
    manifest["manifest_sha256"] = _canonical_sha256(manifest)
    return manifest


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        nested = (_all_keys(item) for item in value.values())
        return set(value) | set().union(*nested)
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value))
    return set()


class HistoricalAcquisitionModuleContract(unittest.TestCase):
    def test_v39_historical_acquisition_module_exists(self) -> None:
        self.assertIsNotNone(
            MODULE_SPEC,
            "RED: src/historical_acquisition.py has not been implemented",
        )


@unittest.skipUnless(
    MODULE_SPEC is not None,
    "historical_acquisition is intentionally absent in the RED phase",
)
class HistoricalAcquisitionV39Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.acquisition = importlib.import_module("historical_acquisition")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.roots = self.acquisition.HistoricalAcquisitionRoots(
            raw_root=base / "raw",
            manifest_root=base / "manifests",
            receipt_root=base / "receipts",
        )
        self.claim_path = base / "control" / "active-market-writer.json"
        self.claim_archive = base / "control" / "claim-archive"
        self.limits = self.acquisition.HistoricalAcquisitionLimits(
            max_events=3,
            max_requests=3,
            max_runtime_sec=30,
            max_retries=0,
        )

    def _authorized_run(
        self,
        *,
        seeds: list[dict[str, object]],
        transport: object,
        limits: object | None = None,
        run_id: str = "historical-v39-test",
    ) -> tuple[dict[str, object], dict[str, mock.Mock]]:
        mocks: dict[str, mock.Mock] = {}
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    self.acquisition.config,
                    "SHARED_WRITER_CLAIM_PATH",
                    self.claim_path,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    self.acquisition.config,
                    "CLAIM_ARCHIVE_DIR",
                    self.claim_archive,
                )
            )
            mocks["preflight"] = stack.enter_context(
                mock.patch.object(
                    self.acquisition.risk_gate,
                    "preflight",
                    return_value={
                        "ok": True,
                        "verified": True,
                        "decision": "ALLOW_HISTORICAL_ACQUISITION",
                        "plan_id": "premarket_perp_capture_20260822_v40",
                        "plan_hash": "e" * 64,
                    },
                )
            )
            mocks["gate"] = stack.enter_context(
                mock.patch.object(
                    self.acquisition.risk_gate,
                    "read_shared_gate",
                    return_value={"open": True, "status": "READY_FOR_POSTPROCESS"},
                )
            )
            mocks["claim"] = stack.enter_context(
                mock.patch.object(
                    self.acquisition.writer_claim,
                    "claim_global_market_writer",
                    return_value={"owner_pid": 123, "ownership_token": "1" * 32},
                )
            )
            mocks["release"] = stack.enter_context(
                mock.patch.object(
                    self.acquisition.writer_claim,
                    "release_global_market_writer",
                    return_value=self.claim_archive / "released.json",
                )
            )
            mocks["builder"] = stack.enter_context(
                mock.patch.object(
                    self.acquisition.historical_event_builder,
                    "build_historical_event",
                    side_effect=_fake_manifest_builder,
                )
            )
            mocks["consume"] = stack.enter_context(
                mock.patch.object(
                    self.acquisition.risk_gate,
                    "consume_capture_token",
                    side_effect=AssertionError("historical acquisition has no capture token"),
                )
            )
            mocks["mint"] = stack.enter_context(
                mock.patch.object(
                    self.acquisition.risk_gate,
                    "mint_capture_token",
                    side_effect=AssertionError("historical acquisition must not mint tokens"),
                )
            )
            result = self.acquisition.run_historical_acquisition(
                run_id=run_id,
                seeds=seeds,
                roots=self.roots,
                limits=limits or self.limits,
                transport=transport,
                received_at_utc=RECEIVED_AT_UTC,
            )
        return result, mocks

    def test_api_requires_injected_transport_and_has_no_token_or_order_argument(self) -> None:
        signature = inspect.signature(self.acquisition.run_historical_acquisition)
        self.assertEqual(
            {
                "run_id",
                "seeds",
                "roots",
                "limits",
                "transport",
                "received_at_utc",
            },
            set(signature.parameters),
        )
        self.assertIs(
            signature.parameters["transport"].default,
            inspect.Parameter.empty,
        )
        parameter_text = " ".join(signature.parameters).lower()
        for forbidden in ("capture_token", "api_key", "secret", "order", "leverage"):
            self.assertNotIn(forbidden, parameter_text)

    def test_only_the_three_declared_public_ohlcv_endpoints_are_reachable(self) -> None:
        self.assertEqual(self.acquisition.OHLCV_ENDPOINTS, EXPECTED_ENDPOINTS)
        for url in self.acquisition.OHLCV_ENDPOINTS.values():
            self.assertTrue(public_http.endpoint_is_allowed(url))
            self.assertTrue(url.startswith("https://"))
        self.assertEqual(set(self.acquisition.OHLCV_ENDPOINTS), {"bybit", "okx", "gate"})

    def test_default_transport_parses_and_bounds_the_fixed_gate_archive(self) -> None:
        archive = gzip.compress(
            (
                f"{T0 - 60},1,99,101,98,100\n"
                f"{T0},2,110,111,99,100\n"
                f"{T0 + 60},3,112,114,109,110\n"
                f"{T0 + 120},4,113,115,111,112\n"
            ).encode("utf-8")
        )
        with mock.patch.object(
            self.acquisition.public_http,
            "get_bytes",
            return_value=archive,
            create=True,
        ) as get_bytes:
            payload = self.acquisition.default_public_transport(
                EXPECTED_ENDPOINTS["gate"],
                {"from": T0, "to": T0 + 60},
                timeout_sec=20,
                max_retries=0,
            )
        self.assertEqual(
            payload,
            {
                "archive_schema": "gate_futures_candlesticks_1m_v1",
                "rows": [
                    [str(T0), "2", "110", "111", "99", "100"],
                    [str(T0 + 60), "3", "112", "114", "109", "110"],
                ],
            },
        )
        get_bytes.assert_called_once_with(
            EXPECTED_ENDPOINTS["gate"],
            params=None,
            timeout_sec=20,
            max_retries=0,
        )

    def test_cli_uses_preregistered_limits_roots_and_default_public_transport(self) -> None:
        seed_file = Path(self.temporary.name) / "seeds.json"
        seed_file.write_text(
            json.dumps(
                {
                    "schema": "premarket_perp_historical_seed_set_v1",
                    "events": [_seed("bybit")],
                }
            ),
            encoding="utf-8",
        )
        terminal = {
            "status": "HISTORICAL_ACQUISITION_COMPLETE",
            "pending_retry": False,
        }
        with (
            mock.patch.object(
                self.acquisition,
                "run_historical_acquisition",
                return_value=terminal,
            ) as run,
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            exit_code = self.acquisition.main(
                [
                    "--seed-file",
                    str(seed_file),
                    "--run-id",
                    "cli-bound-run",
                    "--received-at-utc",
                    RECEIVED_AT_UTC,
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), terminal)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["seeds"], [_seed("bybit")])
        self.assertIs(kwargs["transport"], self.acquisition.default_public_transport)
        self.assertEqual(kwargs["roots"].raw_root, self.acquisition.config.HISTORICAL_RAW_ROOT)
        self.assertEqual(
            kwargs["limits"].max_retries,
            self.acquisition.config.MAX_HISTORICAL_RETRIES_PER_REQUEST,
        )

    def test_preflight_claim_and_postclaim_gate_precede_directories_network_and_writes(self) -> None:
        order: list[str] = []

        def preflight(**kwargs: object) -> dict[str, object]:
            order.append("preflight")
            self.assertEqual(kwargs, {
                "write_class": "historical_market_data_acquisition",
                "run_id": "authority-order",
            })
            return {
                "ok": True,
                "verified": True,
                "plan_id": "premarket_perp_capture_20260822_v40",
                "plan_hash": "e" * 64,
            }

        def claim(*_args: object, **_kwargs: object) -> dict[str, object]:
            order.append("claim")
            self.assertFalse(self.roots.raw_root.exists())
            self.assertFalse(self.roots.manifest_root.exists())
            self.assertFalse(self.roots.receipt_root.exists())
            return {"owner_pid": 123, "ownership_token": "1" * 32}

        def gate() -> dict[str, object]:
            order.append("postclaim_gate")
            self.assertFalse(self.roots.raw_root.exists())
            self.assertFalse(self.roots.manifest_root.exists())
            self.assertFalse(self.roots.receipt_root.exists())
            return {"open": True, "status": "READY_FOR_POSTPROCESS"}

        def transport(
            url: str,
            params: dict[str, object],
            *,
            timeout_sec: int,
            max_retries: int,
        ) -> object:
            order.append("transport")
            self.assertEqual(url, EXPECTED_ENDPOINTS["bybit"])
            self.assertEqual(max_retries, 0)
            self.assertGreater(timeout_sec, 0)
            self.assertNotIn("api_key", params)
            return _fixture_payload("bybit")

        real_write = self.acquisition._write_json_exclusive

        def write(path: Path, payload: dict[str, object]) -> None:
            order.append(
                "receipt_write" if path.parent == self.roots.receipt_root else "write"
            )
            real_write(path, payload)

        def release(*_args: object, **_kwargs: object) -> Path:
            order.append("release")
            return self.claim_archive / "released.json"

        with (
            mock.patch.object(self.acquisition.config, "SHARED_WRITER_CLAIM_PATH", self.claim_path),
            mock.patch.object(self.acquisition.config, "CLAIM_ARCHIVE_DIR", self.claim_archive),
            mock.patch.object(self.acquisition.risk_gate, "preflight", side_effect=preflight),
            mock.patch.object(self.acquisition.risk_gate, "read_shared_gate", side_effect=gate),
            mock.patch.object(
                self.acquisition.writer_claim,
                "claim_global_market_writer",
                side_effect=claim,
            ),
            mock.patch.object(
                self.acquisition.writer_claim,
                "release_global_market_writer",
                side_effect=release,
            ),
            mock.patch.object(
                self.acquisition.historical_event_builder,
                "build_historical_event",
                side_effect=_fake_manifest_builder,
            ),
            mock.patch.object(self.acquisition, "_write_json_exclusive", side_effect=write),
        ):
            result = self.acquisition.run_historical_acquisition(
                run_id="authority-order",
                seeds=[_seed("bybit")],
                roots=self.roots,
                limits=self.limits,
                transport=transport,
                received_at_utc=RECEIVED_AT_UTC,
            )

        self.assertEqual(result["status"], "HISTORICAL_ACQUISITION_COMPLETE")
        self.assertLess(order.index("preflight"), order.index("claim"))
        self.assertLess(order.index("claim"), order.index("postclaim_gate"))
        self.assertLess(order.index("postclaim_gate"), order.index("transport"))
        self.assertLess(order.index("transport"), order.index("write"))
        self.assertLess(order.index("receipt_write"), order.index("release"))

    def test_blocked_preflight_or_closed_postclaim_gate_cannot_write_or_fetch(self) -> None:
        transport = mock.Mock(side_effect=AssertionError("network is forbidden"))
        claim = mock.Mock()
        with mock.patch.object(
            self.acquisition.risk_gate,
            "preflight",
            return_value={"ok": False, "verified": False, "decision": "BLOCK"},
        ), mock.patch.object(
            self.acquisition.writer_claim,
            "claim_global_market_writer",
            claim,
        ):
            with self.assertRaises(self.acquisition.HistoricalAcquisitionError):
                self.acquisition.run_historical_acquisition(
                    run_id="blocked",
                    seeds=[_seed("bybit")],
                    roots=self.roots,
                    limits=self.limits,
                    transport=transport,
                    received_at_utc=RECEIVED_AT_UTC,
                )
        claim.assert_not_called()
        transport.assert_not_called()
        self.assertFalse(self.roots.raw_root.exists())
        self.assertFalse(self.roots.manifest_root.exists())
        self.assertFalse(self.roots.receipt_root.exists())

        release = mock.Mock(return_value=self.claim_archive / "released.json")
        with (
            mock.patch.object(self.acquisition.config, "SHARED_WRITER_CLAIM_PATH", self.claim_path),
            mock.patch.object(self.acquisition.config, "CLAIM_ARCHIVE_DIR", self.claim_archive),
            mock.patch.object(
                self.acquisition.risk_gate,
                "preflight",
                return_value={
                    "ok": True,
                    "verified": True,
                    "plan_id": "plan-v40",
                    "plan_hash": "e" * 64,
                },
            ),
            mock.patch.object(
                self.acquisition.writer_claim,
                "claim_global_market_writer",
                return_value={"owner_pid": 123, "ownership_token": "1" * 32},
            ),
            mock.patch.object(
                self.acquisition.writer_claim,
                "release_global_market_writer",
                release,
            ),
            mock.patch.object(
                self.acquisition.risk_gate,
                "read_shared_gate",
                return_value={"open": False, "status": "BLOCKED"},
            ),
        ):
            with self.assertRaises(self.acquisition.HistoricalAcquisitionError):
                self.acquisition.run_historical_acquisition(
                    run_id="gate-closed",
                    seeds=[_seed("bybit")],
                    roots=self.roots,
                    limits=self.limits,
                    transport=transport,
                    received_at_utc=RECEIVED_AT_UTC,
                )
        release.assert_called_once()
        transport.assert_not_called()
        self.assertFalse(self.roots.raw_root.exists())

    def test_raw_manifest_and_receipt_are_separate_canonical_append_only_artifacts(self) -> None:
        transport = mock.Mock(return_value=_fixture_payload("bybit"))
        result, mocks = self._authorized_run(
            seeds=[_seed("bybit")],
            transport=transport,
            run_id="canonical",
        )

        raw_files = list(self.roots.raw_root.glob("*.json"))
        manifest_files = list(self.roots.manifest_root.glob("*.json"))
        receipt_files = list(self.roots.receipt_root.glob("*.json"))
        self.assertEqual(len(raw_files), 1)
        self.assertEqual(len(manifest_files), 1)
        self.assertEqual(len(receipt_files), 1)
        raw = json.loads(raw_files[0].read_text(encoding="utf-8"))
        manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))
        receipt = json.loads(receipt_files[0].read_text(encoding="utf-8"))

        self.assertEqual(raw["schema"], "premarket_perp_historical_raw_v1")
        self.assertEqual(raw["source_url"], EXPECTED_ENDPOINTS["bybit"])
        self.assertEqual(raw["received_at_utc"], RECEIVED_AT_UTC)
        self.assertEqual(raw["evidence_use"], "DESCRIPTIVE_ONLY")
        raw_without_hash = dict(raw)
        del raw_without_hash["record_sha256"]
        self.assertEqual(raw["record_sha256"], _canonical_sha256(raw_without_hash))

        manifest_without_hash = dict(manifest)
        del manifest_without_hash["manifest_sha256"]
        self.assertEqual(
            manifest["manifest_sha256"],
            _canonical_sha256(manifest_without_hash),
        )
        self.assertEqual(manifest["evidence_use"], "DESCRIPTIVE_ONLY")

        self.assertEqual(receipt["schema"], "premarket_perp_historical_acquisition_receipt_v1")
        self.assertEqual(receipt["received_at_utc"], RECEIVED_AT_UTC)
        receipt_without_hash = dict(receipt)
        del receipt_without_hash["receipt_sha256"]
        self.assertEqual(
            receipt["receipt_sha256"],
            _canonical_sha256(receipt_without_hash),
        )
        self.assertEqual(receipt["limits"], {
            "max_events": 3,
            "max_requests": 3,
            "max_runtime_sec": 30,
            "max_retries": 0,
        })
        self.assertEqual(result["receipt_sha256"], receipt["receipt_sha256"])
        self.assertEqual(mocks["builder"].call_count, 1)

        for artifact in (raw, manifest, receipt, result):
            self.assertNotIn("received_ts", _all_keys(artifact))
            self.assertNotIn("capture_token", _all_keys(artifact))
            self.assertNotIn("order_id", _all_keys(artifact))

    def test_existing_identity_is_refused_without_fetch_or_overwrite(self) -> None:
        first_transport = mock.Mock(return_value=_fixture_payload("bybit"))
        self._authorized_run(
            seeds=[_seed("bybit")],
            transport=first_transport,
            run_id="no-overwrite",
        )
        before = {
            path: path.read_bytes()
            for root in (
                self.roots.raw_root,
                self.roots.manifest_root,
                self.roots.receipt_root,
            )
            for path in root.glob("*.json")
        }
        second_transport = mock.Mock(side_effect=AssertionError("must fail before fetch"))

        with self.assertRaises(self.acquisition.HistoricalAcquisitionError):
            self._authorized_run(
                seeds=[_seed("bybit")],
                transport=second_transport,
                run_id="no-overwrite",
            )

        second_transport.assert_not_called()
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_max_events_and_max_requests_are_independent_hard_boundaries(self) -> None:
        seeds = [_seed("bybit"), _seed("okx"), _seed("gate")]
        cases = (
            self.acquisition.HistoricalAcquisitionLimits(
                max_events=1,
                max_requests=3,
                max_runtime_sec=30,
                max_retries=0,
            ),
            self.acquisition.HistoricalAcquisitionLimits(
                max_events=3,
                max_requests=1,
                max_runtime_sec=30,
                max_retries=0,
            ),
        )
        for index, limits in enumerate(cases):
            with self.subTest(limits=limits):
                base = Path(self.temporary.name) / f"bounds-{index}"
                self.roots = self.acquisition.HistoricalAcquisitionRoots(
                    raw_root=base / "raw",
                    manifest_root=base / "manifests",
                    receipt_root=base / "receipts",
                )
                transport = mock.Mock(side_effect=lambda url, *_args, **_kwargs: (
                    _fixture_payload(next(
                        venue for venue, expected in EXPECTED_ENDPOINTS.items()
                        if expected == url
                    ))
                ))
                result, _mocks = self._authorized_run(
                    seeds=seeds,
                    transport=transport,
                    limits=limits,
                    run_id=f"bounds-{index}",
                )
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(result["completed_events"], 1)
                self.assertEqual(result["status"], "BOUNDED_RETRY_NEXT_INTERVAL")
                self.assertIs(result["pending_retry"], True)
                self.assertEqual(len(result["queued_event_ids"]), 2)

    def test_runtime_boundary_stops_before_another_request(self) -> None:
        ticks = iter((0.0, 0.0, 31.0))

        def monotonic() -> float:
            return next(ticks, 31.0)

        transport = mock.Mock(return_value=_fixture_payload("bybit"))
        with mock.patch.object(self.acquisition.time, "monotonic", side_effect=monotonic):
            result, _mocks = self._authorized_run(
                seeds=[_seed("bybit"), _seed("okx")],
                transport=transport,
                run_id="runtime-bound",
            )
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(result["status"], "BOUNDED_RETRY_NEXT_INTERVAL")
        self.assertIs(result["pending_retry"], True)
        self.assertIn("max_runtime_sec", result["boundary_reason"])

    def test_retries_are_zero_and_transport_never_receives_auth_or_order_fields(self) -> None:
        with self.assertRaises(ValueError):
            self.acquisition.HistoricalAcquisitionLimits(
                max_events=1,
                max_requests=1,
                max_runtime_sec=1,
                max_retries=1,
            )

        calls: list[tuple[str, dict[str, object], dict[str, object]]] = []

        def failing_transport(
            url: str,
            params: dict[str, object],
            **kwargs: object,
        ) -> object:
            calls.append((url, dict(params), dict(kwargs)))
            raise RuntimeError("bounded fixture failure")

        result, mocks = self._authorized_run(
            seeds=[_seed("okx")],
            transport=failing_transport,
            run_id="no-retry",
        )
        self.assertEqual(len(calls), 1)
        url, params, kwargs = calls[0]
        self.assertEqual(url, EXPECTED_ENDPOINTS["okx"])
        self.assertEqual(kwargs["max_retries"], 0)
        forbidden_query = {
            "api_key",
            "apikey",
            "key",
            "secret",
            "signature",
            "sign",
            "timestamp",
            "recv_window",
            "order_id",
            "side",
            "quantity",
            "leverage",
        }
        self.assertFalse({str(key).lower() for key in params} & forbidden_query)
        self.assertEqual(result["status"], "RETRY_NEXT_INTERVAL")
        self.assertIs(result["pending_retry"], True)
        mocks["consume"].assert_not_called()
        mocks["mint"].assert_not_called()

    def test_partial_venue_failure_commits_terminal_receipt_and_keeps_retry(self) -> None:
        def transport(
            url: str,
            _params: dict[str, object],
            **_kwargs: object,
        ) -> object:
            venue = next(
                candidate
                for candidate, endpoint in EXPECTED_ENDPOINTS.items()
                if endpoint == url
            )
            if venue == "okx":
                raise RuntimeError("OKX fixture unavailable")
            return _fixture_payload(venue)

        result, mocks = self._authorized_run(
            seeds=[_seed("bybit"), _seed("okx"), _seed("gate")],
            transport=transport,
            run_id="partial",
        )
        receipt_files = list(self.roots.receipt_root.glob("*.json"))
        self.assertEqual(len(receipt_files), 1)
        receipt = json.loads(receipt_files[0].read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "PARTIAL_RETRY_NEXT_INTERVAL")
        self.assertEqual(receipt["status"], "PARTIAL_RETRY_NEXT_INTERVAL")
        self.assertIs(receipt["pending_retry"], True)
        self.assertEqual(receipt["completed_events"], 2)
        self.assertEqual(receipt["failed_events"], 1)
        self.assertEqual(set(receipt["venue_errors"]), {"okx"})
        self.assertEqual(len(list(self.roots.raw_root.glob("*.json"))), 2)
        self.assertEqual(len(list(self.roots.manifest_root.glob("*.json"))), 2)
        mocks["release"].assert_called_once()
        self.assertEqual(
            mocks["release"].call_args.kwargs["final_status"],
            "PARTIAL_RETRY_NEXT_INTERVAL",
        )

    def test_source_uses_exclusive_creation_and_has_no_execution_capability(self) -> None:
        source_path = Path(self.acquisition.__file__).resolve()
        source = source_path.read_text(encoding="utf-8")
        self.assertIn("O_EXCL", source)
        tree = ast.parse(source)
        forbidden_calls = {
            "consume_capture_token",
            "mint_capture_token",
            "create_order",
            "place_order",
            "submit_order",
            "set_leverage",
            "transfer",
            "withdraw",
        }
        called: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
        self.assertFalse(called & forbidden_calls)


if __name__ == "__main__":
    unittest.main()
