from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import event_registry as registry  # noqa: E402


LEGACY_SHA256 = "fd3b864bc4b1b311b49a904246edd8980008ab5f1830df9042087df9619bc9a4"
LEGACY_HEAD = "7d3459943cf2122c0eb39a27452a4ab28eb41a08ceb61e19011730e14d6e8696"
LEGACY_RECEIPT = "d86913b6af93d3487ef3cdbe09a3b47f3519188eea65ebf2f18aaa8fd5976282"
LEGACY_SUMMARY_SHA256 = "72a619aa893cd794dbc0e3702f2f6fd9ccd3a495c8b610dfa564c8e20b6df176"
LEGACY_RECEIPT_FILE_SHA256 = "1c8eab6576a2d4452dfbcd9aba87563a0a50da4382da0447012e6f95cb1d865b"


class LegacyV2MigrationBoundaryTests(unittest.TestCase):
    def test_v2_source_bytes_and_authority_are_pinned(self) -> None:
        raw = registry.REGISTRY_V2_PATH.read_bytes()
        summary = json.loads(registry.REGISTRY_V2_SUMMARY_PATH.read_text(encoding="utf-8"))

        self.assertEqual(hashlib.sha256(raw).hexdigest(), LEGACY_SHA256)
        self.assertEqual(len([line for line in raw.splitlines() if line.strip()]), 16)
        self.assertEqual(summary["registry"]["head_record_hash"], LEGACY_HEAD)
        self.assertEqual(summary["mutation_receipt_hash"], LEGACY_RECEIPT)
        self.assertEqual(
            hashlib.sha256(registry.REGISTRY_V2_SUMMARY_PATH.read_bytes()).hexdigest(),
            LEGACY_SUMMARY_SHA256,
        )
        self.assertEqual(
            hashlib.sha256(registry.REGISTRY_V2_MUTATION_RECEIPT_PATH.read_bytes()).hexdigest(),
            LEGACY_RECEIPT_FILE_SHA256,
        )

    def test_projection_rejects_tampered_summary_or_missing_receipt_bytes(self) -> None:
        root = Path(tempfile.mkdtemp())
        registry_path = root / registry.REGISTRY_V2_PATH.name
        summary_path = root / registry.REGISTRY_V2_SUMMARY_PATH.name
        receipt_dir = root / (registry_path.name + registry.MUTATION_RECEIPT_DIR_SUFFIX)
        receipt_path = receipt_dir / registry.REGISTRY_V2_MUTATION_RECEIPT_PATH.name
        registry_bytes = registry.REGISTRY_V2_PATH.read_bytes()
        summary_bytes = registry.REGISTRY_V2_SUMMARY_PATH.read_bytes()
        registry_path.write_bytes(registry_bytes)
        summary_path.write_bytes(summary_bytes)
        receipt_dir.mkdir()
        shutil.copyfile(registry.REGISTRY_V2_MUTATION_RECEIPT_PATH, receipt_path)

        with (
            mock.patch.object(registry, "REGISTRY_V2_PATH", registry_path),
            mock.patch.object(registry, "REGISTRY_V2_SUMMARY_PATH", summary_path),
            mock.patch.object(registry, "REGISTRY_V2_MUTATION_RECEIPT_PATH", receipt_path),
        ):
            summary_path.write_bytes(summary_path.read_bytes() + b"\n")
            with self.assertRaisesRegex(registry.EventRegistryError, "summary hash"):
                registry.load_legacy_v2_projection()
            summary_path.write_bytes(summary_bytes)
            receipt_path.unlink()
            with self.assertRaisesRegex(registry.EventRegistryError, "receipt"):
                registry.load_legacy_v2_projection()

    def test_v3_is_a_distinct_empty_authority_path(self) -> None:
        self.assertNotEqual(registry.REGISTRY_PATH, registry.REGISTRY_V2_PATH)
        self.assertEqual(registry.REGISTRY_SCHEMA, "premarket_perp_event_registry_v3")
        self.assertFalse(registry.REGISTRY_PATH.exists())

    def test_projection_is_descriptive_and_does_not_modify_v2(self) -> None:
        before = registry.REGISTRY_V2_PATH.read_bytes()

        projected = registry.load_legacy_v2_projection()

        self.assertEqual(registry.REGISTRY_V2_PATH.read_bytes(), before)
        self.assertEqual(len(projected), 16)
        self.assertTrue(
            all(item["asset_class"] == registry.ASSET_CLASS_UNCLASSIFIED for item in projected)
        )
        self.assertTrue(all(item["capture_eligible"] is False for item in projected))
        self.assertTrue(all(item["legacy_origin"]["registry_sha256"] == LEGACY_SHA256 for item in projected))
        self.assertEqual(Counter(item["venue"] for item in projected), {"bybit": 4, "okx": 3, "gate": 9})


if __name__ == "__main__":
    unittest.main()
