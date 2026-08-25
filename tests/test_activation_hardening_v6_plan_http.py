"""Activation-hardening regressions for PlanOnly v6 and the HTTP boundary.

These tests are intentionally offline.  They prove that a future capture activation
cannot silently discard its immutable lineage, borrow metadata-refresh authority for
an official attestation, or spend more transport attempts than the capture budget
counts.  They do not mint a token or contact a venue.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import capture  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import project_config as config  # noqa: E402
import public_http  # noqa: E402
import risk_gate  # noqa: E402


def published_plan_files() -> tuple[str, ...]:
    """Every plan artifact on disk, oldest first, as repo-relative paths."""
    plans = sorted(
        (config.PROJECT_ROOT / "docs/plans").glob(
            "premarket-perp-capture-planonly-20260822*.json"
        ),
        key=lambda path: (len(path.stem), path.stem),
    )
    return tuple(f"docs/plans/{path.name}" for path in plans)




def _copy_runtime_checkout(destination: Path) -> Path:
    """Copy only files whose bytes the runtime verifier is expected to inspect."""
    source_root = config.PROJECT_ROOT
    active_relative = config.PLAN_PATH.relative_to(source_root)
    relative_paths = {
        active_relative,
        *(Path(str(item["path"])) for item in trust_root.RETIRED_PLANS),
        *(Path(relative) for _role, relative in config.BOUND_RUNTIME_FILES),
    }
    for relative in relative_paths:
        source = source_root / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return destination / active_relative


def _public_dns(*_args, **_kwargs):  # noqa: ANN002, ANN003
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443))
    ]


class _Response:
    def __init__(self, final_url: str, body: bytes) -> None:
        self.final_url = final_url
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self) -> str:
        return self.final_url

    def read(self, _limit: int) -> bytes:
        return self.body


class _Opener:
    def __init__(self, *, body: bytes = b"{}", error: Exception | None = None) -> None:
        self.body = body
        self.error = error
        self.calls = 0

    def open(self, request, timeout=None):  # noqa: ANN001, ARG002
        self.calls += 1
        if self.error is not None:
            raise self.error
        return _Response(request.full_url, self.body)


class RuntimePlanLineageTests(unittest.TestCase):
    def test_active_plan_retires_every_earlier_published_identity(self) -> None:
        self.assertEqual(
            tuple(str(item["path"]).replace("\\", "/") for item in trust_root.RETIRED_PLANS),
            tuple(name for name in published_plan_files()
                  if not name.endswith(config.PLAN_PATH.name)),
        )
        # The version is whatever the trust root pins today. What must hold at every
        # reissue is that the schema, the plan_id and the filename agree about it.
        version = trust_root.ACTIVE_PLAN["plan_id"].rsplit("_", 1)[-1]
        self.assertRegex(version, r"^v\d+$")
        self.assertEqual(
            trust_root.ACTIVE_PLAN["schema"],
            f"premarket_perp_capture_planonly_{version}",
        )
        self.assertTrue(config.PLAN_PATH.name.endswith(f"-{version}.json"))

    def test_load_and_verify_plan_rejects_each_missing_retired_plan(self) -> None:
        for retired in trust_root.RETIRED_PLANS:
            with self.subTest(plan=retired["path"]), tempfile.TemporaryDirectory() as temp:
                checkout = Path(temp)
                active_path = _copy_runtime_checkout(checkout)
                victim = checkout / str(retired["path"])
                os.chmod(victim, 0o666)
                victim.unlink()
                with (
                    mock.patch.object(config, "PROJECT_ROOT", checkout),
                    mock.patch.object(config, "PLAN_PATH", active_path),
                    self.assertRaisesRegex(risk_gate.RiskGateError, "retired|lineage|missing"),
                ):
                    risk_gate.load_and_verify_plan()

    def test_load_and_verify_plan_rejects_each_changed_retired_plan(self) -> None:
        for retired in trust_root.RETIRED_PLANS:
            with self.subTest(plan=retired["path"]), tempfile.TemporaryDirectory() as temp:
                checkout = Path(temp)
                active_path = _copy_runtime_checkout(checkout)
                victim = checkout / str(retired["path"])
                os.chmod(victim, 0o666)
                victim.write_bytes(victim.read_bytes() + b"\n")
                with (
                    mock.patch.object(config, "PROJECT_ROOT", checkout),
                    mock.patch.object(config, "PLAN_PATH", active_path),
                    self.assertRaisesRegex(risk_gate.RiskGateError, "retired|lineage|sha256"),
                ):
                    risk_gate.load_and_verify_plan()


class OfficialAttestationAuthorizationTests(unittest.TestCase):
    @staticmethod
    def _active_plan() -> dict[str, object]:
        return json.loads(config.PLAN_PATH.read_text(encoding="utf-8"))

    def test_plan_preregisters_a_separate_attestation_write_class_and_action(self) -> None:
        plan = self._active_plan()
        policy = config.WRITE_CLASSES.get("official_attestation")
        self.assertIsNotNone(policy)
        self.assertFalse(bool(policy["exclusive_writer_claim"]))
        self.assertFalse(bool(policy["capture_token"]))
        self.assertTrue(bool(policy["plan_and_capability_scan"]))
        self.assertEqual(plan["write_classes"]["official_attestation"], policy)

        action = risk_gate.OFFICIAL_ATTESTATION_ACTION
        self.assertIn(action, plan["authorized_after_gate_green"])
        authorization = risk_gate.PLAN_WRITE_AUTHORIZATION[plan["status"]]
        self.assertIn("official_attestation", authorization["write_classes"])
        self.assertNotIn("market_data_capture", authorization["write_classes"])

        with self.assertRaisesRegex(risk_gate.RiskGateError, "does not authorize"):
            risk_gate.verify_plan_write_authorization(plan, "market_data_capture")

    @staticmethod
    def _on_the_host_the_plan_describes(plan):
        """Run as if this were the machine whose control paths the plan records.

        The plan pins absolute paths to the shared gate, the claim file and the capture
        root. Path.resolve leaves a Windows path alone on Windows and turns it into a
        relative path under the working directory on Linux, so asserting a verified
        preflight without pinning the environment asserts a property of the developer's
        machine. The real verification still runs against these values - only the
        environment is fixed, not the check."""
        return mock.patch.object(
            risk_gate,
            "resolved_path_bindings",
            return_value=dict(plan["resolved_path_bindings"]),
        )

    @mock.patch.object(risk_gate, "mint_capture_token")
    @mock.patch.object(risk_gate, "inspect_run_record")
    @mock.patch.object(risk_gate, "inspect_claim")
    def test_attestation_preflight_is_bound_to_its_exact_write_class_and_action(
        self,
        inspect_claim: mock.Mock,
        inspect_run_record: mock.Mock,
        mint_token: mock.Mock,
    ) -> None:
        plan = self._active_plan()
        with (
            self._on_the_host_the_plan_describes(plan),
            mock.patch.object(risk_gate, "load_and_verify_plan", return_value=plan),
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
        ):
            result = risk_gate.preflight(
                write_class="official_attestation",
                run_id="official_attestation_v6_1",
            )

        self.assertTrue(result["verified"])
        self.assertEqual(result["decision"], "ALLOW_OFFICIAL_ATTESTATION")
        self.assertEqual(result["write_class"], "official_attestation")
        self.assertEqual(
            result["plan_authorization"]["authorized_action"],
            risk_gate.OFFICIAL_ATTESTATION_ACTION,
        )
        inspect_claim.assert_not_called()
        inspect_run_record.assert_not_called()
        mint_token.assert_not_called()

    def test_a_host_whose_control_paths_differ_from_the_plan_is_refused(self) -> None:
        """The property the previous test accidentally proved, asserted on purpose.

        A machine whose shared gate, claim file or capture root is not the one the plan
        pins must not be allowed to write. This is what CI was reporting as a failure:
        the gate refusing on a Linux runner was correct, and only the assertion above
        was wrong."""
        plan = self._active_plan()
        elsewhere = dict(plan["resolved_path_bindings"])
        elsewhere["capture_root"] = "/somewhere/else/captures"
        with (
            mock.patch.object(
                risk_gate, "resolved_path_bindings", return_value=elsewhere
            ),
            mock.patch.object(risk_gate, "load_and_verify_plan", return_value=plan),
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
        ):
            result = risk_gate.preflight(
                write_class="official_attestation",
                run_id="official_attestation_v6_2",
            )
        self.assertFalse(result["verified"])
        self.assertNotEqual(result.get("decision"), "ALLOW_OFFICIAL_ATTESTATION")

    def test_the_binding_covers_every_path_the_plan_pins(self) -> None:
        plan = self._active_plan()
        for key in plan["resolved_path_bindings"]:
            with self.subTest(path=key):
                moved = dict(plan["resolved_path_bindings"])
                moved[key] = moved[key] + "_moved"
                with mock.patch.object(
                    risk_gate, "resolved_path_bindings", return_value=moved
                ):
                    with self.assertRaisesRegex(
                        risk_gate.RiskGateError, "resolved path bindings"
                    ):
                        risk_gate.verify_resolved_path_bindings(plan)


class RuntimeHttpBoundaryTests(unittest.TestCase):
    def test_dynamically_assembled_disallowed_url_fails_before_dns_or_open(self) -> None:
        url = "https://" + "api.bybit.com" + "/v5/market/" + "private"
        with (
            mock.patch.object(
                public_http.socket,
                "getaddrinfo",
                side_effect=AssertionError("DNS must not run for a disallowed URL"),
            ),
            mock.patch.object(public_http, "build_bound_opener") as build_opener,
            self.assertRaises(public_http.EndpointNotAllowed),
        ):
            public_http.get_json(url, max_retries=0)
        build_opener.assert_not_called()

    def test_okx_nonzero_code_without_data_is_an_error_and_is_not_retried(self) -> None:
        url = "https://www.okx.com/api/v5/market/ticker"
        opener = _Opener(body=b'{"code":"50011","msg":"Rate limit reached"}')
        sleeps: list[float] = []
        with (
            mock.patch.object(public_http.socket, "getaddrinfo", side_effect=_public_dns),
            mock.patch.object(public_http, "build_bound_opener", return_value=opener),
            self.assertRaises(public_http.VenueErrorPayload),
        ):
            public_http.get_json(
                url,
                params={"instId": "ABC-USDT-SWAP"},
                max_retries=3,
                sleep_fn=sleeps.append,
            )
        self.assertEqual(opener.calls, 1)
        self.assertEqual(sleeps, [])

    def test_public_http_retry_budget_counts_transport_attempts_exactly(self) -> None:
        url = "https://api.bybit.com/v5/market/tickers"
        opener = _Opener(error=OSError("transient transport failure"))
        sleeps: list[float] = []
        with (
            mock.patch.object(public_http.socket, "getaddrinfo", side_effect=_public_dns) as dns,
            mock.patch.object(public_http, "build_bound_opener", return_value=opener),
            self.assertRaises(public_http.PublicHttpError),
        ):
            public_http.get_json(
                url,
                params={"category": "linear", "symbol": "ABCUSDT"},
                max_retries=1,
                sleep_fn=sleeps.append,
            )
        self.assertEqual(opener.calls, 2, "one retry means two transport attempts")
        self.assertEqual(dns.call_count, 2)
        self.assertEqual(len(sleeps), 1)

    def test_capture_poll_disables_hidden_transport_retries(self) -> None:
        self.assertFalse(
            hasattr(capture, "_default_fetch"),
            "live HTTP fetch must exist only inside the gated capture_event entrypoint",
        )


class CrossPlatformContractTests(unittest.TestCase):
    def test_ci_runs_the_safety_suite_on_windows_too(self) -> None:
        workflow = (ROOT / ".github/workflows/checks.yml").read_text(encoding="utf-8")
        self.assertIn("windows-latest", workflow)

    def test_readme_names_active_plan_and_shows_an_explicit_preflight_write_class(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        active_version = trust_root.PLAN_ID.rsplit("_", 1)[-1]
        self.assertIn(f"PlanOnly {active_version}", readme)
        self.assertIn(trust_root.PLAN_ID, readme)
        self.assertIn("--write-class", readme)


if __name__ == "__main__":
    unittest.main()
