"""t0 provenance, revisions, and the runtime half of the endpoint allow-list.

The hypothesis under study is about the seconds around t0, so a t0 whose origin is
unknown is not a weaker datum - it is a different one. These tests pin that the
registry keeps the difference and refuses to average it away.
"""

from __future__ import annotations

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

import event_registry as registry  # noqa: E402
import project_config as config  # noqa: E402
import public_http  # noqa: E402


T0 = 1787400000  # 2026-08-22T12:00:00Z


def bybit_payload(symbol: str = "FOOUSDT", launch_ms: int = T0 * 1000) -> dict:
    return {"retCode": 0, "result": {
        "category": "linear",
        "list": [{
            "symbol": symbol,
            "launchTime": str(launch_ms),
            "status": "PreLaunch",
            "contractType": "LinearPerpetual",
            "isPreListing": True,
        }],
        "nextPageCursor": "",
    }}


def okx_payload(inst: str = "BAR-USDT-SWAP", list_ms: int = T0 * 1000) -> dict:
    return {"code": "0", "data": [{
        "instId": inst,
        "instType": "FUTURES",
        "ruleType": "pre_market",
        "listTime": str(list_ms),
    }]}


def gate_payload(name: str = "BAZ_USDT", create_s: int = T0) -> list:
    return [{"name": name, "create_time": float(create_s), "status": "prelaunch"}]


def all_payloads(*, bybit: dict | None = None) -> dict:
    return {
        "bybit": bybit or bybit_payload(),
        "okx": okx_payload(),
        "gate": gate_payload(),
    }


def preflight_receipt(run_id: str) -> dict:
    return {
        "schema": "premarket_write_preflight_v2",
        "ok": True,
        "verified": True,
        "decision": "ALLOW_METADATA_REGISTRY",
        "write_class": "metadata_registry",
        "run_id": run_id,
        "plan_id": "premarket_perp_capture_20260822_v2",
        "plan_hash": "a" * 64,
        "resolved_paths_hash": "b" * 64,
    }


class EndpointEnforcementTests(unittest.TestCase):
    """The capability scan reads source; a URL assembled at runtime is invisible to it."""

    class RecordingOpener:
        def __init__(self) -> None:
            self.calls = 0

        def open(self, request, timeout=None):  # noqa: ANN001
            self.calls += 1
            raise AssertionError("a blocked endpoint must never reach the network")

    def test_a_declared_endpoint_is_allowed(self) -> None:
        self.assertTrue(
            public_http.endpoint_is_allowed("https://api.bybit.com/v5/market/tickers?x=1")
        )

    def test_an_undeclared_host_is_refused(self) -> None:
        with self.assertRaises(public_http.EndpointNotAllowed):
            public_http.require_allowed_endpoint("https://api.binance.com/api/v3/ping")

    def test_an_undeclared_path_on_a_declared_host_is_refused(self) -> None:
        """Host alone is not the unit of reach."""
        with self.assertRaises(public_http.EndpointNotAllowed):
            public_http.require_allowed_endpoint(
                "https://api.bybit.com/v5/account/wallet-balance"
            )

    def test_the_refusal_happens_before_any_connection(self) -> None:
        opener = self.RecordingOpener()
        with mock.patch.object(public_http, "build_bound_opener", return_value=opener):
            with self.assertRaises(public_http.EndpointNotAllowed):
                public_http.get_json("https://api.binance.com/api/v3/ping")
        self.assertEqual(opener.calls, 0)

    def test_every_adapter_targets_a_declared_endpoint(self) -> None:
        for adapter in registry.ADAPTERS:
            with self.subTest(venue=adapter.venue):
                self.assertTrue(public_http.endpoint_is_allowed(adapter.url))


class NormalisationTests(unittest.TestCase):
    def _one(self, venue: str, payload):
        adapter = next(a for a in registry.ADAPTERS if a.venue == venue)
        events = registry.normalise(adapter, payload, observed_at_utc="2026-08-22T00:00:00Z")
        return events[0] if events else None

    def test_bybit_milliseconds_become_seconds(self) -> None:
        event = self._one("bybit", bybit_payload())
        self.assertEqual(event["t0_ts"], T0)
        self.assertEqual(event["symbol"], "FOOUSDT")
        self.assertEqual(event["t0_source_field"], "launchTime")

    def test_okx_milliseconds_become_seconds(self) -> None:
        self.assertEqual(self._one("okx", okx_payload())["t0_ts"], T0)

    def test_gate_seconds_stay_seconds(self) -> None:
        self.assertEqual(self._one("gate", gate_payload())["t0_ts"], T0)

    def test_gate_carries_its_caveat_rather_than_pretending(self) -> None:
        """create_time is contract creation, which is not necessarily trading start."""
        event = self._one("gate", gate_payload())
        self.assertIn("CONTRACT_CREATION_NOT_TRADING_START", event["caveats"])
        self.assertGreater(event["t0_precision_sec"], 1)

    def test_every_event_declares_where_its_t0_came_from(self) -> None:
        for venue, payload in (("bybit", bybit_payload()), ("okx", okx_payload()),
                               ("gate", gate_payload())):
            with self.subTest(venue=venue):
                event = self._one(venue, payload)
                self.assertEqual(
                    event["t0_source_class"], registry.SOURCE_VENUE_INSTRUMENT_METADATA
                )
                self.assertTrue(event["t0_semantics"])

    def test_a_missing_launch_time_is_not_a_zero(self) -> None:
        payload = {"retCode": 0, "result": {"list": [{"symbol": "X", "launchTime": ""}]}}
        self.assertIsNone(self._one("bybit", payload))

    def test_nothing_is_an_official_announcement_yet(self) -> None:
        """The class exists so that adding one later is a different class, not a
        quiet upgrade of what venue metadata is taken to mean."""
        for venue, payload in (("bybit", bybit_payload()), ("okx", okx_payload())):
            self.assertNotEqual(
                self._one(venue, payload)["t0_source_class"],
                registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            )


class PaginationTests(unittest.TestCase):
    """Live check on 2026-08-22: Bybit returned exactly 500 rows with a live cursor and
    333 more waiting. The registry held 500 of 833 linear instruments and reported
    nothing wrong - the same silent-truncation defect the spot audit found."""

    def _adapter(self, venue: str):
        return next(a for a in registry.ADAPTERS if a.venue == venue)

    def test_the_cursor_is_followed_to_the_end(self) -> None:
        pages = [
            {"retCode": 0, "result": {"list": [{"symbol": "A", "launchTime": str(T0 * 1000)}],
                                      "nextPageCursor": "p2"}},
            {"retCode": 0, "result": {"list": [{"symbol": "B", "launchTime": str(T0 * 1000)}],
                                      "nextPageCursor": ""}},
        ]
        queue = list(pages)
        result = registry.fetch_venue(self._adapter("bybit"), lambda a, p: queue.pop(0))
        self.assertEqual(result.pages, 2)
        self.assertFalse(result.truncated)
        self.assertEqual([row["symbol"] for row in result.rows], ["A", "B"])

    def test_the_cursor_is_sent_back_to_the_venue(self) -> None:
        seen: list[dict] = []

        def fetch(adapter, params):
            seen.append(dict(params))
            cursor = "" if len(seen) > 1 else "p2"
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": cursor}}

        registry.fetch_venue(self._adapter("bybit"), fetch)
        self.assertNotIn("cursor", seen[0])
        self.assertEqual(seen[1]["cursor"], "p2")

    def test_repeated_cursor_is_rejected_not_swallowed(self) -> None:
        adapter = self._adapter("bybit")
        endless = {"retCode": 0, "result": {"list": [], "nextPageCursor": "always"}}
        with self.assertRaisesRegex(registry.EventRegistryError, "repeated pagination cursor"):
            registry.fetch_venue(adapter, lambda a, p: endless)

    def test_a_venue_without_a_cursor_stops_after_one_page(self) -> None:
        result = registry.fetch_venue(self._adapter("gate"), lambda a, p: gate_payload())
        self.assertEqual(result.pages, 1)
        self.assertFalse(result.truncated)

    def test_a_refresh_reports_completeness(self) -> None:
        path = Path(tempfile.mkdtemp()) / "r.jsonl"
        run_id = "pagination-refresh"
        pages = [
            {"retCode": 0, "result": {
                "category": "linear",
                "list": [{
                    "symbol": "A", "launchTime": str(T0 * 1000),
                    "status": "PreLaunch", "contractType": "LinearPerpetual",
                    "isPreListing": True,
                }],
                "nextPageCursor": "p2",
            }},
            {"retCode": 0, "result": {
                "category": "linear",
                "list": [{
                    "symbol": "B", "launchTime": str(T0 * 1000),
                    "status": "PreLaunch", "contractType": "LinearPerpetual",
                    "isPreListing": True,
                }],
                "nextPageCursor": "",
            }},
        ]
        with mock.patch.object(
            registry.risk_gate, "preflight", return_value=preflight_receipt(run_id)
        ):
            summary = registry.refresh(
                payloads={"bybit": pages, "okx": okx_payload(), "gate": gate_payload()},
                path=path, observed_at_utc="2026-08-22T00:00:00Z", run_id=run_id,
            )
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["truncated_venues"], [])
        self.assertEqual(summary["pages_by_venue"]["bybit"], 2)
        self.assertEqual(summary["observed_by_venue"]["bybit"], 2)


class RevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "listing-events.jsonl"

    def _refresh(self, launch_ms: int, observed: str) -> dict:
        run_id = "revision-" + observed.replace(":", "")
        with mock.patch.object(
            registry.risk_gate, "preflight", return_value=preflight_receipt(run_id)
        ):
            return registry.refresh(
                payloads=all_payloads(bybit=bybit_payload(launch_ms=launch_ms)),
                path=self.path, observed_at_utc=observed, run_id=run_id,
            )

    def test_a_new_event_is_appended_at_revision_zero(self) -> None:
        summary = self._refresh(T0 * 1000, "2026-08-22T00:00:00Z")
        self.assertEqual(summary["new_streams"], 3)
        self.assertEqual(registry.load_registry(self.path)[0]["stream_revision"], 0)

    def test_an_unchanged_event_appends_nothing(self) -> None:
        self._refresh(T0 * 1000, "2026-08-22T00:00:00Z")
        summary = self._refresh(T0 * 1000, "2026-08-22T01:00:00Z")
        self.assertEqual(summary["appended_entries"], 0)
        self.assertEqual(len(registry.load_registry(self.path)), 3)

    def test_a_moved_launch_time_becomes_a_revision_that_keeps_the_old_value(self) -> None:
        """Pre-market listings get delayed. A t0 that silently changed after a capture
        would invalidate the capture with nothing left to show it had moved."""
        self._refresh(T0 * 1000, "2026-08-22T00:00:00Z")
        summary = self._refresh((T0 + 1800) * 1000, "2026-08-22T01:00:00Z")

        self.assertEqual(summary["revisions"], 1)
        entries = registry.load_registry(self.path)
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[0]["t0_ts"], T0)              # untouched
        self.assertEqual(entries[-1]["t0_ts"], T0 + 1800)
        self.assertEqual(entries[-1]["stream_revision"], 1)
        self.assertEqual(entries[-1]["supersedes_record_hash"], entries[0]["record_hash"])

    def test_the_current_view_is_the_latest_revision(self) -> None:
        self._refresh(T0 * 1000, "2026-08-22T00:00:00Z")
        self._refresh((T0 + 1800) * 1000, "2026-08-22T01:00:00Z")
        current = registry.materialize_episodes(registry.load_registry(self.path))
        bybit = next(item for item in current if item["venue"] == "bybit")
        self.assertEqual(bybit["premarket_contract_launch_ts"], T0 + 1800)


class VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "listing-events.jsonl"
        run_id = "verification-setup"
        with mock.patch.object(
            registry.risk_gate, "preflight", return_value=preflight_receipt(run_id)
        ):
            registry.refresh(
                payloads=all_payloads(), path=self.path,
                observed_at_utc="2026-08-22T00:00:00Z", run_id=run_id,
            )

    def test_an_intact_registry_verifies(self) -> None:
        report = registry.verify_registry(self.path)
        self.assertEqual(report["status"], "REGISTRY_OK")
        self.assertEqual(report["events"], 3)
        self.assertEqual(
            report["by_source_class"], {registry.SOURCE_VENUE_INSTRUMENT_METADATA: 3}
        )

    def test_an_edited_entry_is_caught_by_its_own_hash(self) -> None:
        entry = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        entry["t0_ts"] = T0 + 9999
        self.path.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
        report = registry.verify_registry(self.path)
        self.assertEqual(report["status"], "REGISTRY_PROBLEMS")
        self.assertIn("record_hash", " ".join(report["problems"]))

    def test_a_broken_revision_sequence_is_caught(self) -> None:
        entries = registry.load_registry(self.path)
        skipped = dict(entries[0])
        skipped["stream_revision"] = 5
        skipped["record_hash"] = registry._record_hash(skipped)
        self.path.write_text(json.dumps(skipped, sort_keys=True) + "\n", encoding="utf-8")
        self.assertIn("stream_revision", " ".join(registry.verify_registry(self.path)["problems"]))


class CaptureSelectionTests(unittest.TestCase):
    def _entries(self) -> list[dict]:
        proxy = registry.make_timestamp_observation(
            episode_id="a", venue="bybit", premarket_contract_id="A",
            spot_symbol="A", timestamp_kind=registry.TIMESTAMP_PREMARKET_CONTRACT_LAUNCH,
            timestamp_ts=T0, instrument_role="premarket_perp",
            source_class=registry.SOURCE_VENUE_INSTRUMENT_METADATA,
            source_identity="bybit:metadata", source_url="https://api.bybit.com/example",
            received_at_utc="2026-08-22T00:00:00Z",
        )
        official = registry.make_timestamp_observation(
            episode_id="b", venue="okx", premarket_contract_id="B",
            spot_symbol="B", timestamp_kind=registry.TIMESTAMP_OFFICIAL_SPOT_T0,
            timestamp_ts=T0 + 60, instrument_role="spot",
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
            source_identity="okx:announcement", source_url="https://okx.com/example",
            received_at_utc="2026-08-22T00:00:00Z",
        )
        return registry.build_stream_revisions([], [proxy, official])

    def _select(
        self,
        *,
        now_ts: int,
        source_class: str,
        horizon_sec: int = 24 * 3600,
    ) -> list[dict]:
        path = Path(tempfile.mkdtemp()) / "listing-events-v2.jsonl"
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in self._entries()),
            encoding="utf-8",
        )
        receipt = registry.verify_registry(path)
        summary = {
            "schema": registry.REGISTRY_SCHEMA,
            "status": "REFRESH_COMPLETE",
            "complete": True,
            "plan_hash": registry.trust_root.PLAN_HASH,
            "refresh_run_id": "legacy-selector-fixture",
            "resolved_paths_hash": "a" * 64,
            "registry": receipt,
        }
        path.with_suffix(".summary.json").write_text(
            json.dumps(summary, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with mock.patch.object(registry, "REGISTRY_PATH", path):
            return registry.events_for_capture(
                path,
                now_ts=now_ts,
                source_class=source_class,
                horizon_sec=horizon_sec,
            )

    def test_only_one_source_class_is_ever_returned(self) -> None:
        """Mixing classes is the defect the spot monitor's listed_ts column had."""
        with self.assertRaisesRegex(registry.EventRegistryError, "descriptive-only"):
            self._select(
                now_ts=T0 - 3600,
                source_class=registry.SOURCE_VENUE_INSTRUMENT_METADATA,
            )

    def test_the_other_class_is_selectable_but_separate(self) -> None:
        upcoming = self._select(
            now_ts=T0 - 3600,
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
        )
        self.assertEqual([event["symbol"] for event in upcoming], ["B"])

    def test_events_already_past_are_not_offered(self) -> None:
        upcoming = self._select(
            now_ts=T0 + 61,
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
        )
        self.assertEqual([event["symbol"] for event in upcoming], [])

    def test_the_horizon_is_respected(self) -> None:
        upcoming = self._select(
            now_ts=T0,
            horizon_sec=30,
            source_class=registry.SOURCE_OFFICIAL_ANNOUNCEMENT,
        )
        self.assertEqual([event["symbol"] for event in upcoming], [])

    def test_an_unknown_source_class_is_refused(self) -> None:
        with self.assertRaises(registry.EventRegistryError):
            self._select(now_ts=T0, source_class="GUESS")


class WriteClassTests(unittest.TestCase):
    def test_a_metadata_refresh_does_not_take_the_exclusive_claim(self) -> None:
        """Requiring it for a handful of read requests would block a capture for
        no reason."""
        metadata = config.WRITE_CLASSES["metadata_registry"]
        self.assertIs(metadata["exclusive_writer_claim"], False)
        self.assertIs(metadata["capture_token"], False)

    def test_a_capture_does_take_it(self) -> None:
        capture = config.WRITE_CLASSES["market_data_capture"]
        self.assertIs(capture["exclusive_writer_claim"], True)
        self.assertIs(capture["capture_token"], True)

    def test_both_classes_are_still_bound_by_plan_and_reach(self) -> None:
        for name, entry in config.WRITE_CLASSES.items():
            with self.subTest(write_class=name):
                self.assertIs(entry["plan_and_capability_scan"], True)
                self.assertIs(entry["endpoint_allow_list"], True)

    def test_the_plan_records_the_same_distinction(self) -> None:
        plan = json.loads(config.PLAN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            plan["write_classes"]["market_data_capture"]["exclusive_writer_claim"], True
        )
        self.assertEqual(
            plan["write_classes"]["metadata_registry"]["exclusive_writer_claim"], False
        )


if __name__ == "__main__":
    unittest.main()
