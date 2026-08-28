"""Fail-closed registry quarantine transaction contract for the v16 rebind."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import event_registry as registry  # noqa: E402
import frozen_plan_bindings as trust_root  # noqa: E402
import project_config as config  # noqa: E402
import registry_quarantine as quarantine  # noqa: E402
import risk_gate  # noqa: E402


RECOVERY_REPORT = {
    "status": "REGISTRY_INVALID",
    "summary_verified": False,
    "problems": ["registry summary hash does not match"],
    "recovery_action": "RESTORE_MATCHING_SUMMARY_OR_QUARANTINE_AND_BOOTSTRAP_NEW_GENERATION",
}


def allowed_preflight(run_id: str) -> dict:
    return {
        "schema": risk_gate.PREFLIGHT_RESULT_SCHEMA,
        "ok": True,
        "verified": True,
        "decision": "ALLOW_REGISTRY_QUARANTINE",
        "write_class": "registry_quarantine",
        "run_id": run_id,
        "action": risk_gate.REGISTRY_QUARANTINE_ACTION,
        "plan_id": trust_root.PLAN_ID,
        "plan_hash": trust_root.PLAN_HASH,
        "registry_contract_hash": registry.active_registry_contract_hash(),
        "resolved_paths_hash": "d" * 64,
    }


class QuarantineFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.registry_path = self.tmp / "listing-events-v2.jsonl"
        self.summary_path = self.tmp / "listing-events-v2.summary.json"
        self.receipt_dir = self.tmp / "listing-events-v2.jsonl.mutation-receipts"
        self.lock_path = self.tmp / "listing-events-v2.lock"
        self.quarantine_root = self.tmp / "quarantine"
        self.paths = quarantine.QuarantinePaths(
            registry_path=self.registry_path,
            summary_path=self.summary_path,
            receipt_dir=self.receipt_dir,
            lock_path=self.lock_path,
            quarantine_root=self.quarantine_root,
            shared_writer_claim_path=self.tmp / "global-market-writer-claim.json",
        )
        self.registry_bytes = b'{"record":"one"}\n'
        self.summary_bytes = b'{"summary":"broken"}\n'
        self.raw_receipts = {
            "00000000000000000000-a.json": b'{"valid":"json"}\n',
            "00000000000000000001-b.json": b"\xff\x00not-json\r\n",
        }
        self.registry_path.write_bytes(self.registry_bytes)
        self.summary_path.write_bytes(self.summary_bytes)
        self.receipt_dir.mkdir()
        for name, raw in self.raw_receipts.items():
            (self.receipt_dir / name).write_bytes(raw)

    def _production_paths(self):
        return mock.patch.object(quarantine, "_production_paths", return_value=self.paths)

    def _candidate(self) -> dict:
        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ):
            return quarantine.inspect_quarantine_candidate()

    def _execute(
        self,
        *,
        run_id: str = "quarantine-test",
        reason: str = "summary lineage mismatch",
        expected_generation_cas: str | None = None,
        preflight_side_effect=None,
    ) -> dict:
        expected_generation_cas = (
            expected_generation_cas
            or quarantine.snapshot_registry_generation(self.paths).generation_cas
        )
        receipts = preflight_side_effect or [
            allowed_preflight(run_id),
            allowed_preflight(run_id),
        ]
        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(risk_gate, "preflight", side_effect=receipts):
            return quarantine.quarantine_registry(
                run_id=run_id,
                reason=reason,
                expected_generation_cas=expected_generation_cas,
            )


class QuarantinePolicyTests(unittest.TestCase):
    def test_quarantine_is_a_distinct_write_class_and_action(self) -> None:
        self.assertIn(quarantine.WRITE_CLASS, config.WRITE_CLASSES)
        self.assertEqual(
            risk_gate.WRITE_CLASS_ACTION[quarantine.WRITE_CLASS],
            risk_gate.REGISTRY_QUARANTINE_ACTION,
        )
        self.assertFalse(config.WRITE_CLASSES[quarantine.WRITE_CLASS]["capture_token"])
        self.assertTrue(
            config.WRITE_CLASSES[quarantine.WRITE_CLASS]["exclusive_writer_claim"]
        )

    def test_existing_v15_statuses_did_not_gain_quarantine_authority(self) -> None:
        for status in (
            "AWAIT_CAPTURE_IMPLEMENTATION_AUDIT_NO_CAPTURE",
            "CAPTURE_IMPLEMENTATION_AUDIT_GREEN_NO_CAPTURE",
        ):
            with self.subTest(status=status):
                self.assertNotIn(
                    quarantine.WRITE_CLASS,
                    risk_gate.PLAN_WRITE_AUTHORIZATION[status]["write_classes"],
                )

    def test_v16_quarantine_status_authorizes_no_capture(self) -> None:
        authorization = risk_gate.PLAN_WRITE_AUTHORIZATION[
            risk_gate.REGISTRY_QUARANTINE_PLAN_STATUS
        ]
        self.assertIn(quarantine.WRITE_CLASS, authorization["write_classes"])
        self.assertNotIn("market_data_capture", authorization["write_classes"])
        self.assertNotIn(risk_gate.CAPTURE_ACTION, authorization["authorized_actions"])

    def test_v16_plan_matrix_requires_the_exact_quarantine_action(self) -> None:
        authorization = risk_gate.PLAN_WRITE_AUTHORIZATION[
            risk_gate.REGISTRY_QUARANTINE_PLAN_STATUS
        ]
        plan = {
            "status": risk_gate.REGISTRY_QUARANTINE_PLAN_STATUS,
            "authorized_after_gate_green": sorted(authorization["authorized_actions"]),
        }
        verified = risk_gate.verify_plan_write_authorization(
            plan, quarantine.WRITE_CLASS
        )
        self.assertEqual(
            verified["authorized_action"], risk_gate.REGISTRY_QUARANTINE_ACTION
        )
        with self.assertRaises(risk_gate.RiskGateError):
            risk_gate.verify_plan_write_authorization(plan, "market_data_capture")

    def test_open_preflight_emits_the_distinct_quarantine_decision(self) -> None:
        authorization = risk_gate.PLAN_WRITE_AUTHORIZATION[
            risk_gate.REGISTRY_QUARANTINE_PLAN_STATUS
        ]
        plan = {
            "plan_id": "synthetic_v16",
            "plan_hash": "a" * 64,
            "status": risk_gate.REGISTRY_QUARANTINE_PLAN_STATUS,
            "authorized_after_gate_green": sorted(authorization["authorized_actions"]),
        }
        with mock.patch.object(risk_gate, "load_and_verify_plan", return_value=plan), \
             mock.patch.object(risk_gate, "verify_resolved_path_bindings", return_value={}), \
             mock.patch.object(risk_gate, "run_capability_scan", return_value={
                 "status": "CAPABILITY_SCAN_CLEAN", "report_hash": "b" * 64,
             }), \
             mock.patch.object(risk_gate, "read_shared_gate", return_value={
                 "open": True, "status": "READY_FOR_POSTPROCESS",
             }):
            receipt = risk_gate.preflight(
                write_class=quarantine.WRITE_CLASS,
                run_id="synthetic-quarantine",
            )
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["decision"], quarantine.PREFLIGHT_DECISION)
        self.assertNotIn("capture_token", receipt)

    def test_quarantine_preflight_checks_global_writer_and_run_record(self) -> None:
        authorization = risk_gate.PLAN_WRITE_AUTHORIZATION[
            risk_gate.REGISTRY_QUARANTINE_PLAN_STATUS
        ]
        plan = {
            "plan_id": "synthetic-v17-quarantine",
            "plan_hash": "a" * 64,
            "status": risk_gate.REGISTRY_QUARANTINE_PLAN_STATUS,
            "authorized_after_gate_green": sorted(authorization["authorized_actions"]),
        }
        with mock.patch.object(risk_gate, "load_and_verify_plan", return_value=plan), \
             mock.patch.object(risk_gate, "verify_resolved_path_bindings", return_value={}), \
             mock.patch.object(risk_gate, "run_capability_scan", return_value={
                 "status": "CAPABILITY_SCAN_CLEAN", "report_hash": "b" * 64,
             }), \
             mock.patch.object(risk_gate, "read_shared_gate", return_value={
                 "open": True, "status": "READY_FOR_POSTPROCESS",
             }), \
             mock.patch.object(risk_gate, "inspect_claim", return_value={
                 "present": True, "blocks": True, "stale": False,
                 "detail": "foreign market writer is active",
             }) as inspect_claim, \
             mock.patch.object(risk_gate, "inspect_run_record", return_value={
                 "present": False, "blocks": False, "stale": False,
             }) as inspect_run_record:
            receipt = risk_gate.preflight(
                write_class=quarantine.WRITE_CLASS,
                run_id="quarantine-global-exclusion",
            )

        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["decision"], "BLOCK")
        self.assertEqual(receipt["blockers"][0]["source"], "shared_writer_claim")
        inspect_claim.assert_called_once()
        inspect_run_record.assert_called_once()

    def test_quarantine_runtime_is_future_plan_bound(self) -> None:
        self.assertIn(
            ("registry_quarantine", "src/registry_quarantine.py"),
            config.BOUND_RUNTIME_FILES,
        )


class SnapshotCasTests(QuarantineFixture):
    def test_tombstone_names_are_exactly_role_bound_and_reject_windows_ads(self) -> None:
        transaction_id = "20260828T120000Z-example"
        canonical = {
            "registry": self.registry_path,
            "summary": self.summary_path,
            "mutation_receipts": self.receipt_dir,
        }
        for role, path in canonical.items():
            with self.subTest(role=role):
                allowed = quarantine._allowed_tombstone_names(
                    role, transaction_id, path
                )
                self.assertIn(
                    quarantine._tombstone_name(role, transaction_id), allowed
                )
                self.assertNotIn(
                    f".q-{transaction_id}-wrong.deactivated", allowed
                )
                self.assertNotIn(
                    f".q-{transaction_id}-{role}.deactivated:ads", allowed
                )

    def test_generation_cas_covers_registry_summary_and_raw_receipts(self) -> None:
        baseline = quarantine.snapshot_registry_generation(self.paths).generation_cas
        for path in (
            self.registry_path,
            self.summary_path,
            self.receipt_dir / sorted(self.raw_receipts)[0],
        ):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b"x")
                changed = quarantine.snapshot_registry_generation(self.paths).generation_cas
                self.assertNotEqual(changed, baseline)
                path.write_bytes(original)

    def test_receipt_directory_shape_is_part_of_cas(self) -> None:
        with_directory = quarantine.snapshot_registry_generation(
            self.paths
        ).generation_cas
        for child in self.receipt_dir.iterdir():
            child.unlink()
        self.receipt_dir.rmdir()
        without_directory = quarantine.snapshot_registry_generation(
            self.paths
        ).generation_cas
        self.assertNotEqual(with_directory, without_directory)

    def test_receipt_subdirectories_fail_closed(self) -> None:
        (self.receipt_dir / "unexpected").mkdir()
        with self.assertRaisesRegex(quarantine.QuarantineError, "regular files"):
            quarantine.snapshot_registry_generation(self.paths)

    def test_read_only_inspection_reports_operator_cas(self) -> None:
        result = self._candidate()
        self.assertEqual(result["status"], "QUARANTINE_CANDIDATE")
        self.assertEqual(
            result["expected_generation_cas"],
            quarantine.snapshot_registry_generation(self.paths).generation_cas,
        )
        self.assertTrue(self.registry_path.is_file())


class TransactionGuardTests(QuarantineFixture):
    def test_initial_preflight_blocks_before_lock_or_archive(self) -> None:
        blocked = dict(allowed_preflight("blocked"), ok=False, verified=False, decision="BLOCK")
        before = quarantine.snapshot_registry_generation(self.paths).generation_cas
        with self._production_paths(), mock.patch.object(
            risk_gate, "preflight", return_value=blocked
        ):
            with self.assertRaisesRegex(quarantine.QuarantineError, "initial preflight"):
                quarantine.quarantine_registry(
                    run_id="blocked",
                    reason="fixture",
                    expected_generation_cas=before,
                )
        self.assertEqual(
            quarantine.snapshot_registry_generation(self.paths).generation_cas, before
        )
        self.assertFalse(self.lock_path.exists())
        self.assertFalse(self.quarantine_root.exists())

    def test_healthy_registry_cannot_be_quarantined(self) -> None:
        expected = quarantine.snapshot_registry_generation(self.paths).generation_cas
        healthy = {
            "status": "REGISTRY_OK",
            "summary_verified": True,
            "problems": [],
            "recovery_action": None,
        }
        with self._production_paths(), mock.patch.object(
            risk_gate,
            "preflight",
            side_effect=[allowed_preflight("healthy"), allowed_preflight("healthy")],
        ), mock.patch.object(registry, "verify_registry", return_value=healthy):
            with self.assertRaisesRegex(quarantine.QuarantineError, "not require quarantine"):
                quarantine.quarantine_registry(
                    run_id="healthy",
                    reason="fixture",
                    expected_generation_cas=expected,
                )
        self.assertTrue(self.registry_path.exists())
        self.assertFalse(self.lock_path.exists())

    def test_operator_cas_mismatch_is_non_mutating(self) -> None:
        with self.assertRaisesRegex(quarantine.QuarantineError, "generation CAS mismatch"):
            self._execute(expected_generation_cas="0" * 64)
        self.assertEqual(self.registry_path.read_bytes(), self.registry_bytes)
        self.assertFalse(self.lock_path.exists())
        self.assertFalse(any(self.quarantine_root.glob("*")))

    def test_registry_lock_contention_blocks(self) -> None:
        self.lock_path.write_text("held", encoding="utf-8")
        expected = quarantine.snapshot_registry_generation(self.paths).generation_cas
        with self.assertRaisesRegex(quarantine.QuarantineError, "REGISTRY_LOCKED"):
            self._execute(expected_generation_cas=expected)
        self.assertEqual(self.registry_path.read_bytes(), self.registry_bytes)
        self.assertEqual(self.lock_path.read_text(encoding="utf-8"), "held")

    def test_commit_preflight_blocks_before_archive_publication(self) -> None:
        run_id = "commit-blocked"
        blocked = dict(
            allowed_preflight(run_id), ok=False, verified=False, decision="BLOCK"
        )
        with self.assertRaisesRegex(quarantine.QuarantineError, "commit preflight"):
            self._execute(
                run_id=run_id,
                preflight_side_effect=[allowed_preflight(run_id), blocked],
            )
        self.assertTrue(self.registry_path.exists())
        self.assertFalse(self.lock_path.exists())
        self.assertEqual(list(self.quarantine_root.glob("[!.]*")), [])

    def test_second_cas_detects_mutation_after_staging(self) -> None:
        run_id = "cas-race"

        def preflight(*, write_class: str, run_id: str):
            self.assertEqual(write_class, quarantine.WRITE_CLASS)
            if preflight.calls == 1:
                self.summary_path.write_bytes(self.summary_bytes + b"external-change")
            preflight.calls += 1
            return allowed_preflight(run_id)

        preflight.calls = 0
        expected = quarantine.snapshot_registry_generation(self.paths).generation_cas
        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(risk_gate, "preflight", side_effect=preflight):
            with self.assertRaisesRegex(quarantine.QuarantineError, "changed before commit"):
                quarantine.quarantine_registry(
                    run_id=run_id,
                    reason="fixture",
                    expected_generation_cas=expected,
                )
        self.assertTrue(self.registry_path.exists())
        self.assertFalse(self.lock_path.exists())
        self.assertEqual(list(self.quarantine_root.glob("[!.]*")), [])

    def test_global_writer_claim_race_blocks_before_archive_publication(self) -> None:
        expected = quarantine.snapshot_registry_generation(self.paths).generation_cas
        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(
            risk_gate,
            "preflight",
            side_effect=[allowed_preflight("claim-race"), allowed_preflight("claim-race")],
        ), mock.patch.object(
            quarantine.writer_claim,
            "claim_global_market_writer",
            side_effect=quarantine.writer_claim.GlobalMarketWriterClaimError(
                "GLOBAL_MARKET_WRITER_CLAIM_EXISTS"
            ),
        ):
            with self.assertRaisesRegex(
                quarantine.QuarantineError, "global market writer claim blocked"
            ):
                quarantine.quarantine_registry(
                    run_id="claim-race",
                    reason="fixture",
                    expected_generation_cas=expected,
                )

        self.assertEqual(self.registry_path.read_bytes(), self.registry_bytes)
        self.assertFalse(self.lock_path.exists())
        self.assertEqual(list(self.quarantine_root.glob("[!.]*")), [])

    def test_failed_stage_cleanup_retains_registry_lock_and_recovery_identity(self) -> None:
        run_id = "stage-cleanup-failure"
        blocked = dict(
            allowed_preflight(run_id), ok=False, verified=False, decision="BLOCK"
        )
        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(
            risk_gate,
            "preflight",
            side_effect=[allowed_preflight(run_id), blocked],
        ), mock.patch.object(
            quarantine, "_remove_stage", side_effect=OSError("injected cleanup failure")
        ):
            with self.assertRaises(quarantine.QuarantineRecoveryRequired) as caught:
                quarantine.quarantine_registry(
                    run_id=run_id,
                    reason="fixture",
                    expected_generation_cas=quarantine.snapshot_registry_generation(
                        self.paths
                    ).generation_cas,
                )

        self.assertTrue(self.lock_path.exists())
        self.assertTrue(caught.exception.transaction_id)
        self.assertTrue(caught.exception.archive_path.exists())
        self.assertEqual(self.registry_path.read_bytes(), self.registry_bytes)

    def test_initial_and_commit_preflight_are_both_required(self) -> None:
        run_id = "two-preflights"
        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(
            risk_gate,
            "preflight",
            side_effect=[allowed_preflight(run_id), allowed_preflight(run_id)],
        ) as preflight:
            quarantine.quarantine_registry(
                run_id=run_id,
                reason="fixture",
                expected_generation_cas=quarantine.snapshot_registry_generation(
                    self.paths
                ).generation_cas,
            )
        self.assertEqual(preflight.call_count, 2)
        self.assertEqual(
            preflight.call_args_list,
            [
                mock.call(write_class=quarantine.WRITE_CLASS, run_id=run_id),
                mock.call(write_class=quarantine.WRITE_CLASS, run_id=run_id),
            ],
        )


class TransactionArchiveTests(QuarantineFixture):
    def test_lock_proof_digest_failure_happens_before_final_lock_move(self) -> None:
        owner = registry.acquire_registry_lock(
            self.lock_path,
            run_id="digest-before-move",
            plan_hash=trust_root.PLAN_HASH,
        )
        expected_lock_bytes = self.lock_path.read_bytes()
        archive = self.quarantine_root / "digest-before-move"
        archive.mkdir(parents=True)

        with mock.patch.object(
            quarantine.hashlib,
            "sha256",
            side_effect=MemoryError("injected digest failure"),
        ):
            with self.assertRaises(MemoryError):
                quarantine._release_registry_lock_to_archive(
                    owner,
                    archive_path=archive,
                    expected_lock_bytes=expected_lock_bytes,
                )

        self.assertTrue(self.lock_path.exists())
        self.assertFalse((archive / quarantine.LOCK_RELEASE_PROOF_FILE).exists())
        registry.release_registry_lock(owner)

    def test_success_archives_exact_bytes_before_deactivating_registry_last(self) -> None:
        removal_order: list[str] = []
        original = quarantine._deactivate_source_component

        def record(
            role: str,
            path: Path,
            expected: bytes | tuple[quarantine.RawReceipt, ...],
            *,
            transaction_id: str,
        ) -> Path:
            removal_order.append(role)
            return original(
                role,
                path,
                expected,
                transaction_id=transaction_id,
            )

        with mock.patch.object(
            quarantine, "_deactivate_source_component", side_effect=record
        ):
            result = self._execute(run_id="success")

        self.assertEqual(result["status"], "QUARANTINED")
        self.assertEqual(removal_order[-1], "registry")
        self.assertFalse(self.registry_path.exists())
        self.assertFalse(self.summary_path.exists())
        self.assertFalse(self.receipt_dir.exists())
        self.assertFalse(self.lock_path.exists())

        archive = Path(result["archive_path"])
        self.assertEqual((archive / "registry.bin").read_bytes(), self.registry_bytes)
        self.assertEqual((archive / "summary.bin").read_bytes(), self.summary_bytes)
        rows = [json.loads(line) for line in (
            archive / "mutation-receipts.jsonl"
        ).read_text(encoding="utf-8").splitlines()]
        restored = {
            row["original_name"]: base64.b64decode(row["content_b64"], validate=True)
            for row in rows
        }
        self.assertEqual(restored, self.raw_receipts)
        self.assertTrue((archive / "000-PREPARED.json").is_file())
        self.assertTrue((archive / "001-ARCHIVE_DURABLE.json").is_file())
        self.assertTrue((archive / "002-SOURCE_DEACTIVATED.json").is_file())
        self.assertTrue((archive / quarantine.LOCK_RELEASE_PROOF_FILE).is_file())
        source_deactivated = json.loads(
            (archive / "002-SOURCE_DEACTIVATED.json").read_text(encoding="utf-8")
        )
        tombstones = source_deactivated["details"]["retained_source_tombstones"]
        self.assertEqual(
            set(tombstones), {"registry", "summary", "mutation_receipts"}
        )
        for name in tombstones.values():
            self.assertTrue((self.tmp / name).exists(), name)
        self.assertEqual(
            hashlib.sha256(
                (archive / quarantine.LOCK_RELEASE_PROOF_FILE).read_bytes()
            ).hexdigest(),
            result["lock_release_proof_sha256"],
        )
        with self._production_paths():
            status = quarantine.quarantine_transaction_status(
                result["transaction_id"]
            )
        self.assertEqual(status["status"], "COMPLETED")
        self.assertEqual(status["state"], quarantine.STATE_LOCK_RELEASED)

    def test_archive_manifest_contains_no_absolute_source_paths(self) -> None:
        result = self._execute(run_id="relative-paths")
        manifest_bytes = (
            Path(result["archive_path"]) / "archive-manifest.json"
        ).read_bytes()
        self.assertNotIn(str(self.tmp).encode("utf-8"), manifest_bytes)
        manifest = json.loads(manifest_bytes)
        self.assertEqual(manifest["source"]["registry_name"], self.registry_path.name)
        self.assertNotIn("quarantine_dir", manifest)

    def test_completed_transaction_allows_verified_tombstones_to_be_cleaned(self) -> None:
        result = self._execute(run_id="completed-cleanup")
        archive = Path(result["archive_path"])
        source_deactivated = json.loads(
            (archive / "002-SOURCE_DEACTIVATED.json").read_text(encoding="utf-8")
        )
        for name in source_deactivated["details"]["retained_source_tombstones"].values():
            path = self.tmp / name
            if path.is_dir():
                for child in path.iterdir():
                    child.unlink()
                path.rmdir()
            else:
                path.unlink()

        with self._production_paths():
            status = quarantine.quarantine_transaction_status(result["transaction_id"])
        self.assertEqual(status["status"], "COMPLETED", status["problems"])

    def test_partial_tombstone_cleanup_remains_invalid(self) -> None:
        result = self._execute(run_id="partial-cleanup")
        archive = Path(result["archive_path"])
        source_deactivated = json.loads(
            (archive / "002-SOURCE_DEACTIVATED.json").read_text(encoding="utf-8")
        )
        registry_tombstone = self.tmp / source_deactivated["details"][
            "retained_source_tombstones"
        ]["registry"]
        registry_tombstone.unlink()

        with self._production_paths():
            status = quarantine.quarantine_transaction_status(result["transaction_id"])
        self.assertEqual(status["status"], "INVALID_ARCHIVE_FAIL_CLOSED")
        self.assertTrue(
            any("partial tombstone cleanup" in problem for problem in status["problems"]),
            status["problems"],
        )

    def test_archive_tampering_is_invalid_and_fail_closed(self) -> None:
        result = self._execute(run_id="archive-tamper")
        archive = Path(result["archive_path"])
        (archive / "registry.bin").write_bytes(self.registry_bytes + b"tamper")
        with self._production_paths():
            status = quarantine.quarantine_transaction_status(
                result["transaction_id"]
            )
        self.assertEqual(status["status"], "INVALID_ARCHIVE_FAIL_CLOSED")
        self.assertTrue(any("registry.bin" in problem for problem in status["problems"]))

    def test_published_registry_corruption_blocks_source_deactivation(self) -> None:
        run_id = "published-registry-corruption"
        expected = quarantine.snapshot_registry_generation(self.paths).generation_cas
        real_verify = quarantine._verify_published_archive_before_deactivation

        def corrupt_then_verify(archive_path: Path, **kwargs) -> str:
            (archive_path / "registry.bin").write_bytes(b"corrupt-after-publish")
            return real_verify(archive_path, **kwargs)

        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(
            risk_gate,
            "preflight",
            side_effect=[allowed_preflight(run_id), allowed_preflight(run_id)],
        ), mock.patch.object(
            quarantine,
            "_verify_published_archive_before_deactivation",
            side_effect=corrupt_then_verify,
        ):
            with self.assertRaises(quarantine.QuarantineRecoveryRequired) as caught:
                quarantine.quarantine_registry(
                    run_id=run_id,
                    reason="fixture",
                    expected_generation_cas=expected,
                )

        self.assertEqual(self.registry_path.read_bytes(), self.registry_bytes)
        self.assertEqual(self.summary_path.read_bytes(), self.summary_bytes)
        self.assertTrue(self.receipt_dir.is_dir())
        self.assertTrue(self.lock_path.exists())
        self.assertTrue(self.paths.shared_writer_claim_path.exists())
        self.assertFalse(
            (caught.exception.archive_path / "001-ARCHIVE_DURABLE.json").exists()
        )
        with self._production_paths():
            status = quarantine.quarantine_transaction_status(
                caught.exception.transaction_id
            )
        self.assertEqual(status["status"], "INVALID_ARCHIVE_FAIL_CLOSED")

    def test_missing_published_prepared_state_blocks_source_deactivation(self) -> None:
        run_id = "published-prepared-missing"
        expected = quarantine.snapshot_registry_generation(self.paths).generation_cas
        real_verify = quarantine._verify_published_archive_before_deactivation

        def remove_prepared_then_verify(archive_path: Path, **kwargs) -> str:
            (archive_path / quarantine.STATE_FILES[quarantine.STATE_PREPARED]).unlink()
            return real_verify(archive_path, **kwargs)

        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(
            risk_gate,
            "preflight",
            side_effect=[allowed_preflight(run_id), allowed_preflight(run_id)],
        ), mock.patch.object(
            quarantine,
            "_verify_published_archive_before_deactivation",
            side_effect=remove_prepared_then_verify,
        ):
            with self.assertRaises(quarantine.QuarantineRecoveryRequired) as caught:
                quarantine.quarantine_registry(
                    run_id=run_id,
                    reason="fixture",
                    expected_generation_cas=expected,
                )

        self.assertEqual(self.registry_path.read_bytes(), self.registry_bytes)
        self.assertEqual(self.summary_path.read_bytes(), self.summary_bytes)
        self.assertTrue(self.receipt_dir.is_dir())
        self.assertTrue(self.lock_path.exists())
        self.assertTrue(self.paths.shared_writer_claim_path.exists())
        self.assertFalse(
            (caught.exception.archive_path / "001-ARCHIVE_DURABLE.json").exists()
        )
        with self._production_paths():
            status = quarantine.quarantine_transaction_status(
                caught.exception.transaction_id
            )
        self.assertEqual(status["status"], "INVALID_ARCHIVE_FAIL_CLOSED")

    def test_unexpected_published_entry_blocks_source_deactivation(self) -> None:
        run_id = "published-unexpected-entry"
        expected = quarantine.snapshot_registry_generation(self.paths).generation_cas
        real_verify = quarantine._verify_published_archive_before_deactivation

        def add_unexpected_then_verify(archive_path: Path, **kwargs) -> str:
            (archive_path / "unexpected.bin").write_bytes(b"not-declared")
            return real_verify(archive_path, **kwargs)

        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(
            risk_gate,
            "preflight",
            side_effect=[allowed_preflight(run_id), allowed_preflight(run_id)],
        ), mock.patch.object(
            quarantine,
            "_verify_published_archive_before_deactivation",
            side_effect=add_unexpected_then_verify,
        ):
            with self.assertRaises(quarantine.QuarantineRecoveryRequired) as caught:
                quarantine.quarantine_registry(
                    run_id=run_id,
                    reason="fixture",
                    expected_generation_cas=expected,
                )

        self.assertEqual(self.registry_path.read_bytes(), self.registry_bytes)
        self.assertEqual(self.summary_path.read_bytes(), self.summary_bytes)
        self.assertTrue(self.receipt_dir.is_dir())
        self.assertTrue(self.lock_path.exists())
        self.assertTrue(self.paths.shared_writer_claim_path.exists())
        with self._production_paths():
            status = quarantine.quarantine_transaction_status(
                caught.exception.transaction_id
            )
        self.assertEqual(status["status"], "INVALID_ARCHIVE_FAIL_CLOSED")
        self.assertTrue(
            any("unexpected" in problem for problem in status["problems"])
        )

    def test_global_writer_release_failure_retains_both_locks(self) -> None:
        run_id = "global-release-failure"
        expected = quarantine.snapshot_registry_generation(self.paths).generation_cas
        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(
            risk_gate,
            "preflight",
            side_effect=[allowed_preflight(run_id), allowed_preflight(run_id)],
        ), mock.patch.object(
            quarantine.writer_claim,
            "release_global_market_writer",
            side_effect=OSError("injected global writer release failure"),
        ):
            with self.assertRaises(quarantine.QuarantineRecoveryRequired) as caught:
                quarantine.quarantine_registry(
                    run_id=run_id,
                    reason="fixture",
                    expected_generation_cas=expected,
                )

        self.assertFalse(self.registry_path.exists())
        self.assertFalse(self.summary_path.exists())
        self.assertFalse(self.receipt_dir.exists())
        self.assertTrue(self.lock_path.exists())
        self.assertTrue(self.paths.shared_writer_claim_path.exists())
        self.assertFalse(
            (caught.exception.archive_path / quarantine.LOCK_RELEASE_PROOF_FILE).exists()
        )
        source_deactivated = json.loads(
            (
                caught.exception.archive_path
                / quarantine.STATE_FILES[quarantine.STATE_SOURCE_DEACTIVATED]
            ).read_text(encoding="utf-8")
        )
        for name in source_deactivated["details"][
            "retained_source_tombstones"
        ].values():
            self.assertTrue((self.tmp / name).exists(), name)

    def test_global_writer_claim_is_released_before_registry_lock(self) -> None:
        release_order: list[str] = []
        real_global_release = quarantine.writer_claim.release_global_market_writer
        real_registry_release = quarantine._release_registry_lock_to_archive

        def release_global(*args, **kwargs):  # noqa: ANN002, ANN003
            release_order.append("global_writer_claim")
            return real_global_release(*args, **kwargs)

        def release_registry(*args, **kwargs):  # noqa: ANN002, ANN003
            release_order.append("registry_lock")
            return real_registry_release(*args, **kwargs)

        with mock.patch.object(
            quarantine.writer_claim,
            "release_global_market_writer",
            side_effect=release_global,
        ), mock.patch.object(
            quarantine,
            "_release_registry_lock_to_archive",
            side_effect=release_registry,
        ):
            result = self._execute(run_id="release-order")

        self.assertEqual(result["status"], "QUARANTINED")
        self.assertEqual(release_order, ["global_writer_claim", "registry_lock"])

    def test_originally_absent_sources_reappearing_before_first_boundary_retain_both_locks(
        self,
    ) -> None:
        self.summary_path.unlink()
        for receipt in self.receipt_dir.iterdir():
            receipt.unlink()
        self.receipt_dir.rmdir()
        run_id = "absent-sources-reappear-before-boundary"
        expected = quarantine.snapshot_registry_generation(self.paths).generation_cas
        real_archive_state = quarantine._archive_state

        def write_state_then_reappear(*args, **kwargs):  # noqa: ANN002, ANN003
            result = real_archive_state(*args, **kwargs)
            if kwargs.get("state") == quarantine.STATE_SOURCE_DEACTIVATED:
                self.summary_path.write_bytes(b'{"late":"summary"}\n')
                self.receipt_dir.mkdir()
            return result

        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(
            risk_gate,
            "preflight",
            side_effect=[allowed_preflight(run_id), allowed_preflight(run_id)],
        ), mock.patch.object(
            quarantine,
            "_archive_state",
            side_effect=write_state_then_reappear,
        ):
            with self.assertRaises(quarantine.QuarantineRecoveryRequired) as caught:
                quarantine.quarantine_registry(
                    run_id=run_id,
                    reason="fixture",
                    expected_generation_cas=expected,
                )

        self.assertTrue(self.paths.shared_writer_claim_path.exists())
        self.assertTrue(self.lock_path.exists())
        self.assertFalse(
            (caught.exception.archive_path / quarantine.LOCK_RELEASE_PROOF_FILE).exists()
        )
        with self._production_paths():
            status = quarantine.quarantine_transaction_status(
                caught.exception.transaction_id
            )
        self.assertEqual(status["status"], "INVALID_ARCHIVE_FAIL_CLOSED")
        self.assertTrue(
            any("canonical summary source exists" in problem for problem in status["problems"])
        )
        self.assertTrue(
            any(
                "canonical mutation_receipts source exists" in problem
                for problem in status["problems"]
            )
        )

    def test_originally_absent_sources_reappearing_during_global_release_retain_registry_lock(
        self,
    ) -> None:
        self.summary_path.unlink()
        for receipt in self.receipt_dir.iterdir():
            receipt.unlink()
        self.receipt_dir.rmdir()
        run_id = "absent-sources-reappear-during-global-release"
        expected = quarantine.snapshot_registry_generation(self.paths).generation_cas
        real_global_release = quarantine.writer_claim.release_global_market_writer

        def release_then_reappear(*args, **kwargs):  # noqa: ANN002, ANN003
            result = real_global_release(*args, **kwargs)
            self.summary_path.write_bytes(b'{"late":"summary"}\n')
            self.receipt_dir.mkdir()
            return result

        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(
            risk_gate,
            "preflight",
            side_effect=[allowed_preflight(run_id), allowed_preflight(run_id)],
        ), mock.patch.object(
            quarantine.writer_claim,
            "release_global_market_writer",
            side_effect=release_then_reappear,
        ):
            with self.assertRaises(quarantine.QuarantineRecoveryRequired) as caught:
                quarantine.quarantine_registry(
                    run_id=run_id,
                    reason="fixture",
                    expected_generation_cas=expected,
                )

        self.assertFalse(self.paths.shared_writer_claim_path.exists())
        self.assertTrue(self.lock_path.exists())
        self.assertFalse(
            (caught.exception.archive_path / quarantine.LOCK_RELEASE_PROOF_FILE).exists()
        )
        with self._production_paths():
            status = quarantine.quarantine_transaction_status(
                caught.exception.transaction_id
            )
        self.assertEqual(status["status"], "INVALID_ARCHIVE_FAIL_CLOSED")
        self.assertTrue(
            any("canonical summary source exists" in problem for problem in status["problems"])
        )
        self.assertTrue(
            any(
                "canonical mutation_receipts source exists" in problem
                for problem in status["problems"]
            )
        )

    def test_archive_change_during_global_claim_release_retains_registry_lock(self) -> None:
        run_id = "archive-change-during-global-release"
        expected = quarantine.snapshot_registry_generation(self.paths).generation_cas
        real_global_release = quarantine.writer_claim.release_global_market_writer

        def release_then_tamper(*args, **kwargs):  # noqa: ANN002, ANN003
            result = real_global_release(*args, **kwargs)
            archive = next(
                path
                for path in self.quarantine_root.iterdir()
                if path.is_dir() and path.name != ".transactions"
            )
            (archive / "unexpected-after-global-release.bin").write_bytes(b"tamper")
            return result

        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(
            risk_gate,
            "preflight",
            side_effect=[allowed_preflight(run_id), allowed_preflight(run_id)],
        ), mock.patch.object(
            quarantine.writer_claim,
            "release_global_market_writer",
            side_effect=release_then_tamper,
        ):
            with self.assertRaises(quarantine.QuarantineRecoveryRequired) as caught:
                quarantine.quarantine_registry(
                    run_id=run_id,
                    reason="fixture",
                    expected_generation_cas=expected,
                )

        self.assertFalse(self.paths.shared_writer_claim_path.exists())
        self.assertTrue(self.lock_path.exists())
        self.assertFalse(
            (caught.exception.archive_path / quarantine.LOCK_RELEASE_PROOF_FILE).exists()
        )
        with self._production_paths():
            status = quarantine.quarantine_transaction_status(
                caught.exception.transaction_id
            )
        self.assertEqual(status["status"], "INVALID_ARCHIVE_FAIL_CLOSED")

    def test_late_unarchived_bytes_are_restored_not_deleted(self) -> None:
        expected = quarantine.snapshot_registry_generation(self.paths).generation_cas
        run_id = "late-writer"
        original = quarantine._deactivate_source_component
        late_registry = b'{"record":"late-unarchived"}\n'
        late_receipt = b"late-unarchived-receipt\x00"
        injected = False

        def inject_then_deactivate(
            role: str,
            path: Path,
            expected_component: bytes | tuple[quarantine.RawReceipt, ...],
            *,
            transaction_id: str,
        ) -> Path:
            nonlocal injected
            if not injected:
                self.registry_path.write_bytes(late_registry)
                (self.receipt_dir / "late.json").write_bytes(late_receipt)
                injected = True
            return original(
                role,
                path,
                expected_component,
                transaction_id=transaction_id,
            )

        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(
            risk_gate,
            "preflight",
            side_effect=[allowed_preflight(run_id), allowed_preflight(run_id)],
        ), mock.patch.object(
            quarantine,
            "_deactivate_source_component",
            side_effect=inject_then_deactivate,
        ):
            with self.assertRaises(quarantine.QuarantineRecoveryRequired) as caught:
                quarantine.quarantine_registry(
                    run_id=run_id,
                    reason="fixture",
                    expected_generation_cas=expected,
                )

        self.assertEqual(self.registry_path.read_bytes(), late_registry)
        self.assertEqual((self.receipt_dir / "late.json").read_bytes(), late_receipt)
        self.assertTrue(self.lock_path.exists())
        self.assertEqual(
            (caught.exception.archive_path / "registry.bin").read_bytes(),
            self.registry_bytes,
        )

    def test_release_failure_is_not_reported_completed(self) -> None:
        expected = quarantine.snapshot_registry_generation(self.paths).generation_cas
        run_id = "release-failure"
        real_release = registry.release_registry_lock

        def fail_release(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
            raise OSError("injected release failure")

        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(
            risk_gate,
            "preflight",
            side_effect=[allowed_preflight(run_id), allowed_preflight(run_id)],
        ), mock.patch.object(
            quarantine, "_release_registry_lock_to_archive", side_effect=fail_release
        ):
            with self.assertRaises(quarantine.QuarantineRecoveryRequired) as caught:
                quarantine.quarantine_registry(
                    run_id=run_id,
                    reason="fixture",
                    expected_generation_cas=expected,
                )

        error = caught.exception
        self.assertTrue(self.lock_path.exists())
        self.assertFalse(
            (error.archive_path / quarantine.LOCK_RELEASE_PROOF_FILE).exists()
        )
        with self._production_paths():
            status = quarantine.quarantine_transaction_status(error.transaction_id)
        self.assertEqual(status["status"], "RECOVERY_REQUIRED_FAIL_CLOSED")
        self.assertEqual(status["state"], quarantine.STATE_SOURCE_DEACTIVATED)
        # Clean only the exact test lock after proving the failure state.
        owner_payload = json.loads(self.lock_path.read_text(encoding="utf-8"))
        owner = registry.RegistryLockOwner(
            path=self.lock_path,
            owner_pid=owner_payload["owner_pid"],
            owner_host=owner_payload["owner_host"],
            run_id=owner_payload["run_id"],
            nonce=owner_payload["nonce"],
            plan_hash=owner_payload["plan_hash"],
            acquired_at_utc=owner_payload["acquired_at_utc"],
        )
        real_release(owner)

    def test_failure_after_durable_archive_leaves_lock_and_recovery_boundary(self) -> None:
        expected = quarantine.snapshot_registry_generation(self.paths).generation_cas
        run_id = "crash-boundary"
        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(
            risk_gate,
            "preflight",
            side_effect=[allowed_preflight(run_id), allowed_preflight(run_id)],
        ), mock.patch.object(
            quarantine,
            "_deactivate_source_component",
            side_effect=OSError("injected deactivation failure"),
        ):
            with self.assertRaises(quarantine.QuarantineRecoveryRequired) as caught:
                quarantine.quarantine_registry(
                    run_id=run_id,
                    reason="fixture",
                    expected_generation_cas=expected,
                )

        error = caught.exception
        self.assertTrue(error.archive_path.is_dir())
        self.assertTrue((error.archive_path / "001-ARCHIVE_DURABLE.json").is_file())
        self.assertTrue(self.registry_path.is_file())
        self.assertTrue(self.lock_path.is_file())
        with self._production_paths():
            status = quarantine.quarantine_transaction_status(error.transaction_id)
        self.assertEqual(status["status"], "RECOVERY_REQUIRED_FAIL_CLOSED")

    def test_automatic_recovery_is_explicitly_disabled_and_non_mutating(self) -> None:
        expected = quarantine.snapshot_registry_generation(self.paths).generation_cas
        run_id = "no-auto-recovery"
        with self._production_paths(), mock.patch.object(
            registry, "verify_registry", return_value=dict(RECOVERY_REPORT)
        ), mock.patch.object(
            risk_gate,
            "preflight",
            side_effect=[allowed_preflight(run_id), allowed_preflight(run_id)],
        ), mock.patch.object(
            quarantine,
            "_deactivate_source_component",
            side_effect=OSError("injected deactivation failure"),
        ):
            with self.assertRaises(quarantine.QuarantineRecoveryRequired) as caught:
                quarantine.quarantine_registry(
                    run_id=run_id,
                    reason="fixture",
                    expected_generation_cas=expected,
                )
        before = self.registry_path.read_bytes()
        with self._production_paths():
            with self.assertRaisesRegex(
                quarantine.QuarantineRecoveryRequired,
                "automatic recovery is disabled",
            ):
                quarantine.recover_quarantine_transaction(
                    caught.exception.transaction_id
                )
        self.assertEqual(self.registry_path.read_bytes(), before)
        self.assertTrue(self.lock_path.exists())


if __name__ == "__main__":
    unittest.main()
