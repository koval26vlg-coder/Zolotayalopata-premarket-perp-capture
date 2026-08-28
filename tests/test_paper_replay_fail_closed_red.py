"""RED contract for the offline, fail-closed paper-only boundary.

These tests intentionally specify only the safe readiness layer.  They do not
authorize capture, invent a fill model, or turn a sealed capture identifier into a
trade.  Production implementation belongs to the GREEN phase.
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import paper_replay  # noqa: E402


def seconds_grade_candidate_report() -> dict:
    return {
        "status": "CANDIDATE_READY",
        "capture_authorized": False,
        "registry": {
            "status": "REGISTRY_OK",
            "registry_sha256": "1" * 64,
            "entries": 1,
        },
        "candidates": [
            {
                "episode_id": "ep_" + "2" * 64,
                "venue": "bybit",
                "premarket_contract_id": "NEWUSDT",
                "asset_class": "CRYPTO_TOKEN",
                "t0_source_class": "OFFICIAL_ANNOUNCEMENT",
                "t0_precision_sec": 1,
            }
        ],
        "rejections": [],
    }


def run_tick(candidate_report: object, *, sealed_capture_ids: tuple[str, ...] = ()) -> dict:
    with tempfile.TemporaryDirectory() as raw:
        output_dir = Path(raw) / "paper-output-must-not-be-created"
        try:
            result = paper_replay.paper_tick_from_candidate_report(
                candidate_report,
                output_dir=output_dir,
                sealed_capture_ids=sealed_capture_ids,
            )
        except Exception as exc:  # RED should be an assertion failure, not a test error.
            raise AssertionError(
                "paper-only boundary must return a fail-closed report for malformed "
                f"or incomplete input, not raise {type(exc).__name__}: {exc}"
            ) from exc
        if output_dir.exists():
            raise AssertionError("fail-closed paper-only checks must not write an artifact")
        if not isinstance(result, dict):
            raise AssertionError("paper-only boundary must return a JSON-object report")
        return result


class PaperReplayFailClosedRedTests(unittest.TestCase):
    def test_malformed_candidate_report_returns_explicit_incomplete_status(self) -> None:
        result = run_tick({})

        self.assertEqual(result["status"], "PAPER_NOT_RUN_INVALID_CANDIDATE_REPORT")
        self.assertEqual(result["virtual_positions_created"], 0)
        self.assertIsNone(result["net_pnl_usdt"])
        self.assertFalse(result["acceptance_capable"])

    def test_sealed_capture_id_remains_incomplete_until_cost_model_exists(self) -> None:
        result = run_tick(
            seconds_grade_candidate_report(),
            sealed_capture_ids=("capture-sealed-v1",),
        )

        self.assertEqual(result["status"], "PAPER_NOT_RUN_COST_MODEL_MISSING")
        self.assertFalse(result["cost_model_ready"])
        self.assertEqual(result["virtual_positions_created"], 0)
        self.assertIsNone(result["net_pnl_usdt"])
        self.assertFalse(result["acceptance_capable"])

    def test_incomplete_report_hash_is_deterministic_and_self_verifying(self) -> None:
        first = run_tick(
            seconds_grade_candidate_report(),
            sealed_capture_ids=("capture-sealed-v1",),
        )
        second = run_tick(
            json.loads(json.dumps(seconds_grade_candidate_report(), sort_keys=True)),
            sealed_capture_ids=("capture-sealed-v1",),
        )

        self.assertEqual(first, second)
        material = dict(first)
        claimed = material.pop("result_hash")
        self.assertEqual(claimed, paper_replay.canonical_result_hash(material))
        json.dumps(first, allow_nan=False, sort_keys=True)

    def test_runtime_has_no_filesystem_network_or_order_capability(self) -> None:
        source_path = SRC / "paper_replay.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))

        forbidden_import_roots = {
            "http",
            "httpx",
            "os",
            "public_http",
            "requests",
            "socket",
            "subprocess",
            "urllib",
        }
        imported_roots: set[str] = set()
        called_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called_names.add(node.func.attr)

        self.assertFalse(imported_roots & forbidden_import_roots)
        self.assertFalse(
            called_names
            & {
                "capture_event",
                "mkdir",
                "open",
                "rename",
                "replace",
                "rmdir",
                "touch",
                "unlink",
                "write_bytes",
                "write_text",
            }
        )
        lowered = source.lower()
        for endpoint_marker in (
            "/v5/order",
            "/api/v5/trade/",
            "/futures/usdt/orders",
            "place-order",
            "cancel-order",
        ):
            with self.subTest(endpoint_marker=endpoint_marker):
                self.assertNotIn(endpoint_marker, lowered)

    def test_every_fail_closed_branch_is_nonacceptance_only(self) -> None:
        reports = (
            run_tick({}),
            run_tick(seconds_grade_candidate_report()),
            run_tick(
                seconds_grade_candidate_report(),
                sealed_capture_ids=("capture-sealed-v1",),
            ),
        )

        for report in reports:
            with self.subTest(status=report.get("status")):
                self.assertIs(report.get("acceptance_capable"), False)
                self.assertEqual(report.get("virtual_positions_created"), 0)
                self.assertIsNone(report.get("net_pnl_usdt"))


if __name__ == "__main__":
    unittest.main()
