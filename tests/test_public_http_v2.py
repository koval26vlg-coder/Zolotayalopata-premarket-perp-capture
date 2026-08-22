"""Fail-closed tests for the runtime HTTP capability boundary.

All successful-request tests replace the bound-opener factory and patch DNS.  The
suite must never contact a venue or the public DNS while proving the policy.
"""

from __future__ import annotations

import inspect
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import public_http  # noqa: E402
import project_config as config  # noqa: E402


BYBIT_INSTRUMENTS = "https://api.bybit.com/v5/market/instruments-info"


def public_dns(*_args, **_kwargs):  # noqa: ANN002, ANN003
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443))
    ]


class FakeResponse:
    def __init__(self, final_url: str, body: bytes = b"{}") -> None:
        self._final_url = final_url
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self) -> str:
        return self._final_url

    def read(self, _limit: int) -> bytes:
        return self._body


class FakeOpener:
    def __init__(self, final_url: str | None = None) -> None:
        self.final_url = final_url
        self.calls = 0

    def open(self, request, timeout=None):  # noqa: ANN001, ARG002
        self.calls += 1
        return FakeResponse(self.final_url or request.full_url)


class EndpointIdentityTests(unittest.TestCase):
    def test_path_is_exact_not_a_prefix(self) -> None:
        self.assertTrue(public_http.endpoint_is_allowed(BYBIT_INSTRUMENTS))
        self.assertFalse(public_http.endpoint_is_allowed(BYBIT_INSTRUMENTS + "/private"))

    def test_only_https_is_accepted(self) -> None:
        with self.assertRaises(public_http.EndpointNotAllowed):
            public_http.require_allowed_endpoint(
                "http://api.bybit.com/v5/market/instruments-info"
            )

    def test_userinfo_fragment_and_non_tls_port_are_rejected(self) -> None:
        bad_urls = (
            "https://user@api.bybit.com/v5/market/instruments-info",
            BYBIT_INSTRUMENTS + "#ignored",
            "https://api.bybit.com:444/v5/market/instruments-info",
        )
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(public_http.EndpointNotAllowed):
                public_http.require_allowed_endpoint(url)

    def test_explicit_443_is_allowed(self) -> None:
        public_http.require_allowed_endpoint(
            "https://api.bybit.com:443/v5/market/instruments-info"
        )

    def test_dot_segments_and_encoded_path_separators_are_rejected(self) -> None:
        bad_urls = (
            "https://api.bybit.com/v5/market/../market/instruments-info",
            "https://api.bybit.com/v5/market/%2e%2e/market/instruments-info",
            "https://api.bybit.com/v5/market%2finstruments-info",
            "https://api.bybit.com/v5/market%5cinstruments-info",
            "https://api.bybit.com/v5/market\\instruments-info",
        )
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(public_http.EndpointNotAllowed):
                public_http.require_allowed_endpoint(url)

    def test_runtime_caller_cannot_supply_an_alternate_allow_list(self) -> None:
        self.assertNotIn("allowed", inspect.signature(public_http.endpoint_is_allowed).parameters)
        self.assertNotIn("allowed", inspect.signature(public_http.require_allowed_endpoint).parameters)
        self.assertNotIn("allowed_endpoints", inspect.signature(public_http.get_json).parameters)

    def test_runtime_caller_cannot_supply_an_unbound_opener(self) -> None:
        self.assertNotIn("opener", inspect.signature(public_http.get_json).parameters)


class QueryPolicyTests(unittest.TestCase):
    def test_query_policy_cannot_be_mutated_by_a_caller(self) -> None:
        with self.assertRaises(TypeError):
            public_http.ALLOWED_QUERY_KEYS_BY_ENDPOINT[("evil.example", "/private")] = (  # type: ignore[index]
                frozenset({"api_key"})
            )

    def test_every_plan_endpoint_has_an_explicit_query_policy(self) -> None:
        self.assertEqual(
            set(config.ALLOWED_ENDPOINTS),
            set(public_http.ALLOWED_QUERY_KEYS_BY_ENDPOINT),
        )

    def test_declared_query_keys_are_allowed(self) -> None:
        public_http.require_allowed_endpoint(
            BYBIT_INSTRUMENTS + "?category=linear&cursor=next"
        )

    def test_unknown_or_duplicate_query_keys_are_rejected(self) -> None:
        bad_urls = (
            BYBIT_INSTRUMENTS + "?api_key=secret",
            BYBIT_INSTRUMENTS + "?category=linear&category=inverse",
            BYBIT_INSTRUMENTS + "?=linear",
        )
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(public_http.EndpointNotAllowed):
                public_http.require_allowed_endpoint(url)

    def test_params_are_checked_before_dns_or_open(self) -> None:
        opener = FakeOpener()
        with mock.patch.object(
            public_http.socket, "getaddrinfo", side_effect=AssertionError("DNS called")
        ), mock.patch.object(
            public_http, "build_bound_opener", return_value=opener
        ) as build_opener:
            with self.assertRaises(public_http.EndpointNotAllowed):
                public_http.get_json(
                    BYBIT_INSTRUMENTS,
                    params={"private": "1"},
                )
        self.assertEqual(opener.calls, 0)
        build_opener.assert_not_called()


class DnsPolicyTests(unittest.TestCase):
    def test_public_address_is_accepted_before_open(self) -> None:
        opener = FakeOpener()
        with mock.patch.object(
            public_http.socket, "getaddrinfo", side_effect=public_dns
        ), mock.patch.object(
            public_http, "build_bound_opener", return_value=opener
        ) as build_opener:
            self.assertEqual(
                public_http.get_json(
                    BYBIT_INSTRUMENTS,
                    params={"category": "linear"},
                    max_retries=0,
                ),
                {},
            )
        self.assertEqual(opener.calls, 1)
        build_opener.assert_called_once_with(
            BYBIT_INSTRUMENTS + "?category=linear", ("1.1.1.1",), attempt=0
        )

    def test_non_public_dns_answers_are_rejected_before_open(self) -> None:
        addresses = (
            "127.0.0.1",       # loopback
            "10.0.0.7",        # private
            "169.254.1.2",     # link-local
            "192.0.2.5",       # reserved/documentation
            "224.0.0.1",       # multicast
            "0.0.0.0",         # unspecified
            "::1",             # IPv6 loopback
            "fc00::1",         # IPv6 private
        )
        for address in addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            answer = [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))]
            opener = FakeOpener()
            with self.subTest(address=address), mock.patch.object(
                public_http.socket, "getaddrinfo", return_value=answer
            ), mock.patch.object(
                public_http, "build_bound_opener", return_value=opener
            ) as build_opener:
                with self.assertRaises(public_http.DnsAddressNotAllowed):
                    public_http.get_json(BYBIT_INSTRUMENTS, max_retries=0)
            self.assertEqual(opener.calls, 0)
            build_opener.assert_not_called()

    def test_one_private_answer_poisoning_a_mixed_set_fails_closed(self) -> None:
        answer = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
        ]
        opener = FakeOpener()
        with mock.patch.object(
            public_http.socket, "getaddrinfo", return_value=answer
        ), mock.patch.object(
            public_http, "build_bound_opener", return_value=opener
        ) as build_opener:
            with self.assertRaises(public_http.DnsAddressNotAllowed):
                public_http.get_json(BYBIT_INSTRUMENTS, max_retries=0)
        self.assertEqual(opener.calls, 0)
        build_opener.assert_not_called()

    def test_bound_connection_uses_validated_ip_and_original_tls_name(self) -> None:
        context = mock.Mock()
        raw_socket = object()
        wrapped_socket = object()
        context.wrap_socket.return_value = wrapped_socket
        connection = public_http.BoundHTTPSConnection(
            "api.bybit.com",
            resolved_address="1.1.1.1",
            timeout=7,
            context=context,
        )
        with mock.patch.object(
            public_http.socket, "create_connection", return_value=raw_socket
        ) as create_connection:
            connection.connect()

        create_connection.assert_called_once_with(("1.1.1.1", 443), 7, None)
        context.wrap_socket.assert_called_once_with(
            raw_socket,
            server_hostname="api.bybit.com",
        )
        self.assertEqual(connection.host, "api.bybit.com")
        self.assertIs(connection.sock, wrapped_socket)


class RedirectPolicyTests(unittest.TestCase):
    def test_default_opener_installs_a_no_redirect_handler(self) -> None:
        opener = public_http.build_bound_opener(
            BYBIT_INSTRUMENTS, ("1.1.1.1",), attempt=0
        )
        self.assertTrue(
            any(isinstance(handler, public_http.NoRedirectHandler) for handler in opener.handlers)
        )
        bound_handlers = [
            handler
            for handler in opener.handlers
            if isinstance(handler, public_http.BoundHTTPSHandler)
        ]
        self.assertEqual(len(bound_handlers), 1)
        self.assertEqual(bound_handlers[0].resolved_address, "1.1.1.1")
        self.assertEqual(bound_handlers[0].expected_host, "api.bybit.com")

    def test_redirect_handler_refuses_without_following(self) -> None:
        handler = public_http.NoRedirectHandler()
        request = public_http.urllib.request.Request(BYBIT_INSTRUMENTS)
        with self.assertRaises(public_http.RedirectNotAllowed):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://api.bybit.com/v5/market/tickers",
            )

    def test_changed_final_url_is_rejected_even_with_an_injected_opener(self) -> None:
        opener = FakeOpener("https://api.bybit.com/v5/market/tickers?category=linear")
        with mock.patch.object(
            public_http.socket, "getaddrinfo", side_effect=public_dns
        ), mock.patch.object(public_http, "build_bound_opener", return_value=opener):
            with self.assertRaises(public_http.RedirectNotAllowed):
                public_http.get_json(
                    BYBIT_INSTRUMENTS,
                    params={"category": "linear"},
                    max_retries=0,
                )

    def test_disallowed_final_url_is_validated(self) -> None:
        opener = FakeOpener("https://127.0.0.1/private")
        with mock.patch.object(
            public_http.socket, "getaddrinfo", side_effect=public_dns
        ), mock.patch.object(public_http, "build_bound_opener", return_value=opener):
            with self.assertRaises(public_http.EndpointNotAllowed):
                public_http.get_json(BYBIT_INSTRUMENTS, max_retries=0)


if __name__ == "__main__":
    unittest.main()
