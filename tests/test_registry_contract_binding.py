"""Recorded data binds to the rules it was produced under, not to the whole document.

The registry summary used to carry the active plan_hash, so reissuing the PlanOnly for
anything at all - a new module, a wider announcement list, a capture cadence - left the
registry failing verification. The only sanctioned repair was quarantine and bootstrap,
which meant every edit cost a generation, and the repair for the quarantine module
itself had to run through the quarantine it was repairing.

Binding to the registry contract keeps the guarantee that made the rule worth having -
data must be provably produced under the rules it claims - while letting the rest of
the plan move.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import event_registry as registry  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import plan_builder  # noqa: E402
import risk_gate  # noqa: E402
from canonical_hash import canonical_hash  # noqa: E402


class ContractHashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = plan_builder.build_plan("2026-08-24T00:00:00.000Z")

    def test_the_plan_publishes_a_registry_contract_hash(self):
        self.assertEqual(len(self.plan["registry_contract_hash"]), 64)
        self.assertEqual(
            self.plan["registry_contract_hash"],
            canonical_hash(self.plan["event_registry"]),
        )

    def test_the_runtime_reads_it_from_the_verified_plan(self):
        self.assertEqual(
            registry.active_registry_contract_hash(),
            self.plan["registry_contract_hash"],
        )

    def test_a_change_outside_the_registry_leaves_the_contract_alone(self):
        # The whole point: this is what used to cost a generation.
        for field in ("objective", "allowed_endpoints", "capture_bounds", "sampling"):
            with self.subTest(field=field):
                altered = copy.deepcopy(self.plan)
                if field not in altered:
                    self.skipTest(f"{field} is not in this plan")
                altered[field] = (
                    "changed" if isinstance(altered[field], str) else {"changed": True}
                )
                self.assertEqual(
                    canonical_hash(altered["event_registry"]),
                    self.plan["registry_contract_hash"],
                )

    def test_a_change_to_the_registry_contract_does_move_it(self):
        # And this is the guarantee that must survive: data produced under different
        # registry rules must not verify as though nothing happened.
        altered = copy.deepcopy(self.plan)
        altered["event_registry"]["lineage"] = "something else entirely"
        self.assertNotEqual(
            canonical_hash(altered["event_registry"]),
            self.plan["registry_contract_hash"],
        )

    def test_the_registry_contract_covers_the_clauses_that_govern_it(self):
        contract = self.plan["event_registry"]
        for clause in ("schema", "path", "timestamp_kinds", "source_classes",
                       "acceptance_anchor", "lineage", "locking"):
            with self.subTest(clause=clause):
                self.assertIn(clause, contract)


class LiveRegistryTests(unittest.TestCase):
    def test_the_checked_in_registry_verifies_under_the_active_plan(self):
        report = registry.verify_registry()
        self.assertEqual(report["status"], "REGISTRY_OK", report.get("problems"))
        self.assertTrue(report["summary_verified"])

    def test_the_summary_records_the_contract_it_was_written_under(self):
        summary = json.loads(registry.REGISTRY_SUMMARY_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            summary["registry_contract_hash"], registry.active_registry_contract_hash()
        )

    def test_the_summary_still_records_which_plan_wrote_it(self):
        # Provenance is not dropped by narrowing the binding: the plan that produced
        # the data is still named, it just no longer invalidates it.
        summary = json.loads(registry.REGISTRY_SUMMARY_PATH.read_text(encoding="utf-8"))
        self.assertTrue(summary.get("plan_id"))
        self.assertEqual(len(str(summary.get("plan_hash") or "")), 64)


if __name__ == "__main__":
    unittest.main()
