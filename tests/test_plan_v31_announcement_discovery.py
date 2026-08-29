"""RED PlanOnly identity and authorization contract for announcement discovery v31."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import frozen_plan_bindings as trust_root  # noqa: E402
import plan_builder  # noqa: E402
import project_config as config  # noqa: E402
import public_http  # noqa: E402
import risk_gate  # noqa: E402


V30_PATH = ROOT / "docs/plans/premarket-perp-capture-planonly-20260822-v30.json"
V30_PLAN_ID = "premarket_perp_capture_20260822_v30"
V30_PLAN_HASH = "32877c7c731bdf63167b20827f373726e34e1fbc1bcd61db26d6975444067ab5"
V30_FILE_SHA256 = "d68bf90c354063622a33e762a58ec610594af1bcc7359cf8125a28e9f933a192"
V31_RELATIVE_PATH = "docs/plans/premarket-perp-capture-planonly-20260822-v31.json"
V31_PATH = ROOT / V31_RELATIVE_PATH
V31_PLAN_ID = "premarket_perp_capture_20260822_v31"
V31_SCHEMA = "premarket_perp_capture_planonly_v31"
V31_PLAN_HASH = "0359596666d918145af2fe3e172cd9907b9f286b0d25a986671be8113415bb98"
V31_FILE_SHA256 = "a92c2f8a105a6b63c8747d5a45e75ec22af445da286292a4666bc02d02db26b4"
V31_STATUS = "ANNOUNCEMENT_DISCOVERY_CANDIDATE_STORE_NO_CAPTURE"


class V31IdentityTests(unittest.TestCase):
    def test_v30_predecessor_bytes_are_unchanged(self) -> None:
        self.assertEqual(hashlib.sha256(V30_PATH.read_bytes()).hexdigest(), V30_FILE_SHA256)
        payload = json.loads(V30_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload["plan_id"], V30_PLAN_ID)
        self.assertEqual(payload["plan_hash"], V30_PLAN_HASH)

    def test_v31_artifact_preserves_exact_v30_to_v31_lineage(self) -> None:
        self.assertEqual(config.V30_PLAN_PATH, V30_PATH)
        self.assertEqual(config.V31_PLAN_PATH, V31_PATH)
        payload = json.loads(V31_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], V31_SCHEMA)
        self.assertEqual(payload["plan_id"], V31_PLAN_ID)
        self.assertEqual(payload["supersedes_plan_id"], V30_PLAN_ID)
        self.assertEqual(payload["supersedes_plan_hash"], V30_PLAN_HASH)
        self.assertEqual(
            payload["supersedes_plan_path"],
            "docs/plans/premarket-perp-capture-planonly-20260822-v30.json",
        )
        self.assertEqual(payload["status"], V31_STATUS)
        self.assertEqual(payload["plan_hash"], V31_PLAN_HASH)
        self.assertEqual(hashlib.sha256(V31_PATH.read_bytes()).hexdigest(), V31_FILE_SHA256)

    def test_v31_and_v30_are_both_retired_without_identity_reuse(self) -> None:
        retired = {item["path"]: item for item in trust_root.RETIRED_PLANS}
        self.assertEqual(retired[V30_PATH.relative_to(ROOT).as_posix()]["plan_hash"], V30_PLAN_HASH)
        self.assertEqual(
            retired[V30_PATH.relative_to(ROOT).as_posix()]["plan_file_sha256"],
            V30_FILE_SHA256,
        )
        self.assertEqual(retired[V31_RELATIVE_PATH]["plan_hash"], V31_PLAN_HASH)
        self.assertEqual(
            retired[V31_RELATIVE_PATH]["plan_file_sha256"],
            V31_FILE_SHA256,
        )


class AnnouncementRiskAndReachTests(unittest.TestCase):
    def test_discovery_has_its_own_exact_write_class_without_capture_controls(self) -> None:
        policy = config.WRITE_CLASSES["announcement_discovery"]
        self.assertIs(policy["exclusive_writer_claim"], False)
        self.assertIs(policy["capture_token"], False)
        self.assertIs(policy["plan_and_capability_scan"], True)
        self.assertIs(policy["endpoint_allow_list"], True)
        self.assertEqual(policy["max_retries_per_request"], 0)

    def test_status_authorizes_discovery_but_never_market_capture(self) -> None:
        authorization = risk_gate.PLAN_WRITE_AUTHORIZATION[V31_STATUS]
        self.assertIn("announcement_discovery", authorization["write_classes"])
        self.assertIn(
            risk_gate.ANNOUNCEMENT_DISCOVERY_ACTION,
            authorization["authorized_actions"],
        )
        self.assertNotIn("market_data_capture", authorization["write_classes"])
        self.assertNotIn(risk_gate.CAPTURE_ACTION, authorization["authorized_actions"])

    def test_announcement_endpoints_are_separate_and_exact(self) -> None:
        self.assertEqual(
            set(config.ANNOUNCEMENT_ALLOWED_ENDPOINTS),
            {
                ("api.bybit.com", "/v5/announcements/index"),
                ("api.bitget.com", "/api/v2/public/annoucements"),
                ("api.kucoin.com", "/api/v3/announcements"),
            },
        )
        for endpoint in config.ANNOUNCEMENT_ALLOWED_ENDPOINTS:
            self.assertIn(endpoint, config.ALLOWED_ENDPOINTS)
            self.assertIn(endpoint, public_http.ALLOWED_QUERY_KEYS_BY_ENDPOINT)

    def test_discovery_runtime_and_launcher_are_sha_bound(self) -> None:
        bound = dict(config.BOUND_RUNTIME_FILES)
        self.assertEqual(bound["announcement_discovery"], "src/announcement_discovery.py")
        self.assertEqual(
            bound["announcement_candidate_store"],
            "src/announcement_candidate_store.py",
        )
        self.assertEqual(
            bound["announcement_discovery_launcher"],
            "tools/start_premarket_announcement_discovery_visible.ps1",
        )

    def test_risk_contract_remains_research_only_and_execution_free(self) -> None:
        contract = config.RISK_CONTRACT
        self.assertIs(contract["research_only"], True)
        self.assertIs(contract["public_data_only"], True)
        for key in (
            "private_api",
            "api_keys",
            "request_signing",
            "orders",
            "paper_execution",
            "live_execution",
            "uses_leverage",
            "uses_margin",
            "real_capital",
        ):
            with self.subTest(capability=key):
                self.assertIs(contract[key], False)


if __name__ == "__main__":
    unittest.main()
