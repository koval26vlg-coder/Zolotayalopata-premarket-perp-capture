"""Quarantine moves a registry generation aside; it never discards one.

verify_registry has always been able to name RESTORE_MATCHING_SUMMARY_OR_QUARANTINE_
AND_BOOTSTRAP_NEW_GENERATION as the recovery action. Until this existed it was a string
in a report, which left the operator to improvise at exactly the step where improvising
is worst.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import event_registry as registry  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402


class QuarantineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.path = self.root / "listing-events-v2.jsonl"
        self.path.write_text('{"a": 1}\n', encoding="utf-8", newline="\n")
        self.summary = self.path.with_suffix(".summary.json")
        self.summary.write_text('{"complete": true}\n', encoding="utf-8", newline="\n")
        self.receipts = self.path.with_name(self.path.name + ".mutation-receipts")
        self.receipts.mkdir()
        (self.receipts / "0.json").write_text("{}\n", encoding="utf-8", newline="\n")

    def _quarantine(self, **overrides):
        fields = {"run_id": "recovery_1", "reason": "summary predates the active plan",
                  "path": self.path}
        fields.update(overrides)
        return registry.quarantine_registry(**fields)

    def test_the_generation_is_moved_not_deleted(self):
        receipt = self._quarantine()
        destination = Path(receipt["quarantine_dir"])
        self.assertFalse(self.path.exists())
        self.assertTrue((destination / self.path.name).is_file())
        self.assertTrue((destination / self.summary.name).is_file())
        self.assertTrue((destination / self.receipts.name).is_dir())

    def test_the_moved_bytes_are_unchanged_and_recorded(self):
        original = hashlib.sha256(self.path.read_bytes()).hexdigest()
        receipt = self._quarantine()
        moved = {item["name"]: item["sha256"] for item in receipt["moved"]}
        self.assertEqual(moved[self.path.name], original)
        landed = Path(receipt["quarantine_dir"]) / self.path.name
        self.assertEqual(hashlib.sha256(landed.read_bytes()).hexdigest(), original)

    def test_the_receipt_records_why_and_under_which_plan(self):
        receipt = self._quarantine(reason="lineage fork")
        self.assertEqual(receipt["reason"], "lineage fork")
        self.assertEqual(receipt["active_plan_id"], trust_root.PLAN_ID)
        self.assertEqual(receipt["active_plan_hash"], trust_root.PLAN_HASH)
        on_disk = json.loads(
            (Path(receipt["quarantine_dir"]) / "quarantine-receipt.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(on_disk["reason"], "lineage fork")

    def test_a_reason_and_a_run_id_are_both_required(self):
        # Quarantining without saying why leaves the next reader with a gap and no
        # account of it, which is the thing this project keeps refusing to do.
        with self.assertRaises(registry.EventRegistryError):
            self._quarantine(reason="  ")
        with self.assertRaises(registry.EventRegistryError):
            self._quarantine(run_id="")

    def test_quarantining_nothing_is_refused(self):
        self.path.unlink()
        with self.assertRaisesRegex(registry.EventRegistryError, "no registry"):
            self._quarantine()

    def test_two_quarantines_do_not_collide(self):
        first = self._quarantine()
        self.path.write_text('{"a": 2}\n', encoding="utf-8", newline="\n")
        second = self._quarantine(run_id="recovery_2")
        self.assertNotEqual(first["quarantine_dir"], second["quarantine_dir"])
        self.assertTrue(Path(first["quarantine_dir"]).is_dir())

    def test_the_directory_name_stays_short_enough_to_commit(self):
        # Mutation-receipt filenames are already ~85 characters. A quarantine prefix
        # built from a full run_id pushed the total past the 260-character Windows
        # limit and git refused to add the files - which would have made the recovery
        # itself unrecordable.
        receipt = self._quarantine(run_id="a_deliberately_long_recovery_run_identifier_x")
        name = Path(receipt["quarantine_dir"]).name
        self.assertLessEqual(len(name), 26)
        self.assertNotIn("deliberately", name)
        self.assertEqual(receipt["run_id"], "a_deliberately_long_recovery_run_identifier_x")

    def test_a_fresh_registry_can_be_written_after_quarantine(self):
        self._quarantine()
        self.assertFalse(self.path.exists())
        self.assertEqual(registry.load_registry(self.path), [])


if __name__ == "__main__":
    unittest.main()
