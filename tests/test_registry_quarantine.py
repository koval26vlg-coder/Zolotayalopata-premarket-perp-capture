"""The removed v15 quarantine shortcut must never reappear."""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import event_registry as registry  # noqa: E402


class LegacyQuarantineRemovalTests(unittest.TestCase):
    def test_event_registry_exposes_no_ungated_quarantine_writer(self) -> None:
        self.assertFalse(hasattr(registry, "quarantine_registry"))

    def test_event_registry_cli_rejects_the_removed_quarantine_flag(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                registry.main(["--quarantine-registry"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
