"""A venue's legitimate quirk must not reject an entire universe surface.

Strictness that refuses to write is the right failure direction, but a validator that
calls a legal value malformed stops the project rather than protecting it. Measured
2026-08-23: one OKX SWAP row of 454 (JP225-USDT-SWAP, state preopen) carries
ruleType "", and requiring that field non-empty rejected the surface, made every
refresh incomplete, and blocked every registry write.

These tests pin the difference between "the venue told us something unexpected" and
"the venue told us nothing".
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import event_registry as registry  # noqa: E402


def okx_payload(rows):
    return {"code": "0", "data": list(rows)}


def okx_row(**overrides):
    row = {
        "instId": "NEW-USDT-SWAP",
        "instType": "SWAP",
        "state": "live",
        "ruleType": "pre_market",
        "listTime": "1800000000000",
    }
    row.update(overrides)
    return row


class OkxRuleTypeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.surface = next(s for s in registry.SURFACES if s.surface_id == "okx_swap")

    def _rows(self, rows):
        return registry._surface_payload_rows(self.surface, okx_payload(rows))

    def test_an_empty_rule_type_is_accepted(self):
        # An instrument under no special rule. This exact shape blocked the project.
        accepted = self._rows([okx_row(instId="JP225-USDT-SWAP",
                                       state="preopen", ruleType="")])
        self.assertEqual(len(accepted), 1)

    def test_one_such_row_does_not_reject_its_neighbours(self):
        accepted = self._rows([
            okx_row(instId="A-USDT-SWAP"),
            okx_row(instId="JP225-USDT-SWAP", state="preopen", ruleType=""),
            okx_row(instId="B-USDT-SWAP", ruleType="normal"),
        ])
        self.assertEqual(len(accepted), 3)

    def test_an_empty_rule_type_is_still_not_a_premarket_row(self):
        # Accepting the row must not promote it: it has no pre-market rule.
        adapter = next(a for a in registry.ADAPTERS if a.venue == "okx")
        row = okx_row(instId="JP225-USDT-SWAP", state="preopen", ruleType="")
        self.assertFalse(registry._is_premarket_row(adapter, row))

    def test_a_missing_rule_type_is_still_refused(self):
        # Absent is not the same as empty: the field must be present and a string.
        row = okx_row()
        row.pop("ruleType")
        with self.assertRaises(registry.EventRegistryError):
            self._rows([row])

    def test_a_padded_rule_type_is_still_refused(self):
        with self.assertRaises(registry.EventRegistryError):
            self._rows([okx_row(ruleType=" pre_market ")])

    def test_an_empty_state_is_still_refused(self):
        # state carries the lifecycle; empty there really is nothing.
        with self.assertRaises(registry.EventRegistryError):
            self._rows([okx_row(state="")])

    def test_a_non_string_rule_type_is_still_refused(self):
        with self.assertRaises(registry.EventRegistryError):
            self._rows([okx_row(ruleType=None)])


if __name__ == "__main__":
    unittest.main()
