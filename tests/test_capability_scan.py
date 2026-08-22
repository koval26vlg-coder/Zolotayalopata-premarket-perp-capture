"""The scan has to actually catch the things the risk contract rules out.

A gate nobody has tried to get past is a claim, not a control. Each test here builds a
runtime that does one forbidden thing and asserts the scan refuses it.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import project_config as config  # noqa: E402
from capability_scan import (  # noqa: E402
    CapabilityViolation,
    EXCLUDED_DIRS,
    SCANNED_GLOBS,
    assert_runtime_is_clean,
    load_markers,
    scan_runtime,
)


MARKERS_PATH = ROOT / "docs/risk/forbidden-capabilities.txt"
ALLOWED = config.ALLOWED_ENDPOINTS


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "src").mkdir()

    def _write(self, body: str, name: str = "collector.py") -> None:
        (self.tmp / "src" / name).write_text(body, encoding="utf-8")

    def _scan(self):
        return scan_runtime(
            self.tmp, markers=load_markers(MARKERS_PATH), allowed_endpoints=ALLOWED
        )

    def _findings(self) -> list[str]:
        findings, _ = self._scan()
        return [f.detail for f in findings]

    def test_an_innocent_public_collector_passes(self) -> None:
        self._write(
            'URL = "https://api.bybit.com/v5/market/tickers"\n'
            'def fetch():\n    return URL\n'
        )
        findings, exemptions = self._scan()
        self.assertEqual(findings, [])
        self.assertEqual(exemptions, [])

    def test_order_placement_is_refused(self) -> None:
        self._write('ORDER = "https://api.bybit.com/v5/order/create"\n')
        self.assertIn("/v5/order", self._findings())

    def test_request_signing_is_refused(self) -> None:
        self._write('import hmac\nsig = hmac.new(b"x", b"y")\n')
        self.assertIn("hmac", self._findings())

    def test_credentials_are_refused(self) -> None:
        self._write('api_key = "whatever"\n')
        self.assertIn("api_key", self._findings())

    def test_changing_leverage_is_refused(self) -> None:
        """Observing a leveraged market is the point; taking leverage is not."""
        self._write('PATH = "/v5/position/leverage"\n')
        self.assertIn("/position/leverage", self._findings())

    def test_withdrawal_is_refused(self) -> None:
        self._write('PATH = "/api/v4/withdrawals"\n')
        self.assertIn("/withdraw", self._findings())

    def test_an_endpoint_outside_the_allow_list_is_refused(self) -> None:
        self._write('URL = "https://api.binance.com/api/v3/ticker"\n')
        self.assertIn("https://api.binance.com/api/v3/ticker", self._findings())

    def test_an_allowed_host_with_an_undeclared_path_is_refused(self) -> None:
        """Host alone is not the unit of reach: the exact path is declared too."""
        self._write('URL = "https://api.bybit.com/v5/account/wallet-balance"\n')
        self.assertIn("https://api.bybit.com/v5/account/wallet-balance", self._findings())

    def test_declared_path_prefix_collision_is_refused(self) -> None:
        url = "https://api.bybit.com/v5/market/tickers-private"
        self._write(f'URL = "{url}"\n')
        self.assertIn(url, self._findings())

    def test_non_https_scheme_is_refused_even_for_declared_host_and_path(self) -> None:
        url = "http://api.bybit.com/v5/market/tickers"
        self._write(f'URL = "{url}"\n')
        self.assertIn(url, self._findings())

    def test_powershell_tooling_is_scanned_as_well(self) -> None:
        (self.tmp / "tools").mkdir()
        (self.tmp / "tools" / "run.ps1").write_text(
            '$u = "https://api.binance.com/api/v3/ping"\n', encoding="utf-8"
        )
        self.assertIn("https://api.binance.com/api/v3/ping", self._findings())


class ExemptionTests(unittest.TestCase):
    """Declaring a prohibition means naming it, so exemptions exist - narrowly."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "src").mkdir()

    def _scan(self):
        return scan_runtime(
            self.tmp, markers=load_markers(MARKERS_PATH), allowed_endpoints=ALLOWED
        )

    def test_an_inline_exemption_clears_that_pattern(self) -> None:
        (self.tmp / "src" / "c.py").write_text(
            'CONTRACT = {"api_keys": False}  # risk-scan: allow api_key\n', encoding="utf-8"
        )
        findings, exemptions = self._scan()
        self.assertEqual(findings, [])
        self.assertEqual([f.detail for f in exemptions], ["api_key"])

    def test_an_exemption_does_not_cover_other_patterns_on_the_same_line(self) -> None:
        (self.tmp / "src" / "c.py").write_text(
            'x = "api_key"; import hmac  # risk-scan: allow api_key\n', encoding="utf-8"
        )
        findings, _ = self._scan()
        self.assertEqual([f.detail for f in findings], ["hmac"])

    def test_an_exemption_does_not_leak_to_the_next_line(self) -> None:
        (self.tmp / "src" / "c.py").write_text(
            'a = 1  # risk-scan: allow api_key\napi_key = "leaked"\n', encoding="utf-8"
        )
        findings, _ = self._scan()
        self.assertEqual([f.detail for f in findings], ["api_key"])


class ScopeTests(unittest.TestCase):
    def test_the_scan_covers_runtime_and_tooling_only(self) -> None:
        self.assertEqual(SCANNED_GLOBS, ("src/*.py", "tools/*.ps1", "tools/*.py"))

    def test_the_exclusion_list_stays_narrow(self) -> None:
        """Tests must be able to name a forbidden thing; nothing else gets a pass."""
        self.assertEqual(EXCLUDED_DIRS, ("tests", "docs", ".git", "__pycache__"))

    def test_this_repository_passes_its_own_scan(self) -> None:
        report = assert_runtime_is_clean(
            ROOT, markers_path=MARKERS_PATH, allowed_endpoints=ALLOWED
        )
        self.assertEqual(report["status"], "CAPABILITY_SCAN_CLEAN")
        self.assertGreater(report["markers"], 0)

    def test_every_exemption_in_this_repository_is_accounted_for(self) -> None:
        """Exemptions are decisions. If the count moves, it moves in review."""
        report = assert_runtime_is_clean(
            ROOT, markers_path=MARKERS_PATH, allowed_endpoints=ALLOWED
        )
        self.assertEqual(
            sorted(entry.split()[-1] for entry in report["exemptions"]),
            ["api_key", "api_key", "api_key"],
        )

    def test_a_violation_raises_rather_than_returning_quietly(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "src").mkdir()
        (tmp / "src" / "bad.py").write_text('api_secret = "x"\n', encoding="utf-8")
        with self.assertRaises(CapabilityViolation):
            assert_runtime_is_clean(
                tmp, markers_path=MARKERS_PATH, allowed_endpoints=ALLOWED
            )


class AllowListSanityTests(unittest.TestCase):
    def test_no_declared_endpoint_is_itself_a_forbidden_surface(self) -> None:
        """A path could be allow-listed and forbidden at once; that must be impossible."""
        markers = [m.pattern for m in load_markers(MARKERS_PATH)]
        for host, path in ALLOWED:
            with self.subTest(endpoint=f"{host}{path}"):
                for marker in markers:
                    self.assertNotIn(marker, f"{host}{path}".lower())

    def test_the_allow_list_is_public_market_data_only(self) -> None:
        for host, path in ALLOWED:
            with self.subTest(endpoint=f"{host}{path}"):
                lowered = path.lower()
                self.assertTrue(
                    "market" in lowered or "instruments" in lowered
                    or "contracts" in lowered or "candlesticks" in lowered
                    or "tickers" in lowered or "order_book" in lowered
                    or "trades" in lowered,
                    f"{path} does not look like public market data",
                )


if __name__ == "__main__":
    unittest.main()
