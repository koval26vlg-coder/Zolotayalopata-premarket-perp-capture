"""The runtime half of the endpoint rule, tested where it used to let things through.

The capability scan reads source and catches a literal URL. A URL assembled at runtime
is invisible to it, so public_http is the only thing standing between this project and
an undeclared endpoint. Three ways past it were found by audit and are pinned here.
"""

from __future__ import annotations

import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import project_config as config  # noqa: E402
import public_http  # noqa: E402


DECLARED = "https://api.bybit.com/v5/market/tickers"


class AllowListTests(unittest.TestCase):
    def test_a_declared_endpoint_is_allowed(self):
        self.assertTrue(public_http.endpoint_is_allowed(DECLARED))

    def test_a_path_that_merely_starts_with_a_declared_one_is_refused(self):
        # str.startswith made every declared path a prefix of undeclared siblings.
        self.assertFalse(
            public_http.endpoint_is_allowed(DECLARED + "-undeclared")
        )

    def test_dot_dot_segments_are_refused_outright(self):
        # This one passed the old check while naming a forbidden namespace.
        self.assertFalse(
            public_http.endpoint_is_allowed(DECLARED + "/../../secret/withdraw")
        )

    def test_plain_http_is_refused(self):
        self.assertFalse(
            public_http.endpoint_is_allowed("http://api.bybit.com/v5/market/tickers")
        )
        self.assertEqual(public_http.ALLOWED_SCHEMES, ("https",))

    def test_a_declared_host_with_an_undeclared_path_is_refused(self):
        self.assertFalse(
            public_http.endpoint_is_allowed("https://api.bybit.com/v5/order/create")
        )

    def test_an_undeclared_host_is_refused(self):
        self.assertFalse(
            public_http.endpoint_is_allowed("https://evil.example/v5/market/tickers")
        )

    def test_a_port_or_userinfo_does_not_smuggle_a_host_through(self):
        for url in (
            "https://api.bybit.com:8443/v5/market/tickers",
            "https://user:pw@api.bybit.com/v5/market/tickers",
        ):
            with self.subTest(url=url):
                self.assertFalse(public_http.endpoint_is_allowed(url))

    def test_every_declared_endpoint_passes_its_own_check(self):
        for host, path in config.ALLOWED_ENDPOINTS:
            with self.subTest(endpoint=f"{host}{path}"):
                self.assertTrue(public_http.endpoint_is_allowed(f"https://{host}{path}"))

    def test_a_refused_url_never_reaches_the_network(self):
        opened = []
        with mock.patch.object(public_http, "build_opener",
                               side_effect=lambda *a, **k: opened.append("opened")):
            with self.assertRaises(public_http.EndpointNotAllowed):
                public_http.get_json("https://evil.example/anything")
        self.assertEqual(opened, [])


class RedirectTests(unittest.TestCase):
    """A redirect is a new request and faces the same rule as the first one.

    Without this the allow-list checked only the URL we typed: an allow-listed host
    answering 302 to anywhere would be followed without another thought."""

    def _handler(self):
        return public_http.AllowListRedirectHandler()

    def _request(self):
        return urllib.request.Request(DECLARED)

    def test_a_redirect_to_an_undeclared_host_is_refused(self):
        with self.assertRaises(public_http.EndpointNotAllowed):
            self._handler().redirect_request(
                self._request(), None, 302, "Found", {}, "https://evil.example/steal"
            )

    def test_a_redirect_to_an_undeclared_path_on_a_declared_host_is_refused(self):
        with self.assertRaises(public_http.EndpointNotAllowed):
            self._handler().redirect_request(
                self._request(), None, 302, "Found", {},
                "https://api.bybit.com/v5/order/create",
            )

    def test_a_redirect_downgrading_to_http_is_refused(self):
        with self.assertRaises(public_http.EndpointNotAllowed):
            self._handler().redirect_request(
                self._request(), None, 302, "Found", {},
                "http://api.bybit.com/v5/market/tickers",
            )

    def test_a_redirect_to_a_declared_endpoint_is_allowed_through(self):
        result = self._handler().redirect_request(
            self._request(), None, 302, "Found", {},
            "https://api.bybit.com/v5/market/kline",
        )
        self.assertIsNotNone(result)

    def test_the_opener_actually_installs_the_checking_handler(self):
        # A handler that exists but is not wired in would test clean and protect nothing.
        opener = public_http.build_opener()
        self.assertTrue(
            any(isinstance(h, public_http.AllowListRedirectHandler)
                for h in opener.handlers),
            "build_opener must install AllowListRedirectHandler",
        )

    def test_the_opener_carries_the_allow_list_it_was_given(self):
        narrow = (("api.bybit.com", "/v5/market/kline"),)
        opener = public_http.build_opener(narrow)
        handler = next(h for h in opener.handlers
                       if isinstance(h, public_http.AllowListRedirectHandler))
        with self.assertRaises(public_http.EndpointNotAllowed):
            handler.redirect_request(self._request(), None, 302, "Found", {}, DECLARED)


if __name__ == "__main__":
    unittest.main()
