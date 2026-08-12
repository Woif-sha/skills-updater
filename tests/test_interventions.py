import json
import hashlib
import os
import tempfile
import unittest
import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


class InterventionTests(unittest.TestCase):
    def test_inventory_is_read_only_and_ignores_legacy_backups(self):
        from scripts.interventions import inventory_interventions

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "interventions"
            record = root / "demo-content-conflict-0123456789abcdef"
            record.mkdir(parents=True)
            manifest = {
                "schemaVersion": 1,
                "artifactId": record.name,
                "recordType": "content-conflict",
                "skillName": "demo",
                "createdAt": "2026-08-01T00:00:00Z",
                "resolutionState": "unresolved",
                "recoveryState": None,
                "retentionStartedAt": None,
                "retentionExpiresAt": None,
                "retentionGroup": [
                    {"role": "intervention-record", "path": record.name},
                ],
                "diagnosticReferences": ["conflicts/manifest.json"],
            }
            (record / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            legacy = root / ".backup-20260801-000000"
            legacy.mkdir()
            (legacy / "do-not-touch.txt").write_text("legacy", encoding="utf-8")
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            items = inventory_interventions(root)

            self.assertEqual(
                items,
                [
                    {
                        "artifact_id": record.name,
                        "record_type": "content-conflict",
                        "skill_name": "demo",
                        "resolution_state": "unresolved",
                        "recovery_state": None,
                        "retention_started_at": None,
                        "retention_expires_at": None,
                        "retention_group": [
                            {"role": "intervention-record", "path": record.name},
                        ],
                        "diagnostic_references": ["conflicts/manifest.json"],
                        "cleanup_eligible": False,
                    }
                ],
            )
            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
            self.assertTrue((legacy / "do-not-touch.txt").is_file())

    def test_unresolved_records_cannot_claim_an_expired_retention_window(self):
        from scripts.interventions import InterventionError, inventory_interventions

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "interventions"
            artifact_id = "demo-content-conflict-invalid-retention"
            record = root / artifact_id
            record.mkdir(parents=True)
            (record / "manifest.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "artifactId": artifact_id,
                        "recordType": "content-conflict",
                        "skillName": "demo",
                        "createdAt": "2026-08-01T00:00:00Z",
                        "resolutionState": "unresolved",
                        "recoveryState": None,
                        "retentionStartedAt": "2026-07-01T00:00:00Z",
                        "retentionExpiresAt": "2026-07-02T00:00:00Z",
                        "retentionGroup": [
                            {"role": "intervention-record", "path": artifact_id},
                        ],
                        "diagnosticReferences": ["conflicts/SKILL.md"],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(InterventionError):
                inventory_interventions(
                    root,
                    now=datetime(2026, 8, 11, tzinfo=timezone.utc),
                )

    def test_malformed_record_type_is_a_domain_error(self):
        from scripts.interventions import InterventionError, inventory_interventions

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "interventions"
            record = root / "malformed-record-type"
            record.mkdir(parents=True)
            (record / "manifest.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "artifactId": record.name,
                        "recordType": [],
                        "skillName": "demo",
                        "createdAt": "2026-08-01T00:00:00Z",
                        "resolutionState": None,
                        "recoveryState": None,
                        "retentionStartedAt": None,
                        "retentionExpiresAt": None,
                        "retentionGroup": [
                            {"role": "intervention-record", "path": record.name},
                        ],
                        "diagnosticReferences": ["conflicts/SKILL.md"],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(InterventionError):
                inventory_interventions(root)

    def test_resolved_conflict_starts_exact_fifteen_day_retention(self):
        from scripts.interventions import mark_content_conflict

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "interventions"
            artifact_id = "demo-content-conflict-0123456789abcdef"
            record = root / artifact_id
            record.mkdir(parents=True)
            (record / "manifest.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "artifactId": artifact_id,
                        "recordType": "content-conflict",
                        "skillName": "demo",
                        "createdAt": "2026-08-01T00:00:00Z",
                        "resolutionState": "unresolved",
                        "recoveryState": None,
                        "retentionStartedAt": None,
                        "retentionExpiresAt": None,
                        "retentionGroup": [
                            {"role": "intervention-record", "path": artifact_id},
                        ],
                        "diagnosticReferences": ["conflicts/manifest.json"],
                    }
                ),
                encoding="utf-8",
            )
            resolved_at = datetime(2026, 8, 11, 12, 30, tzinfo=timezone.utc)

            item = mark_content_conflict(root, artifact_id, "resolved", now=resolved_at)

            self.assertEqual(item["resolution_state"], "resolved")
            self.assertEqual(item["retention_started_at"], "2026-08-11T12:30:00Z")
            self.assertEqual(item["retention_expires_at"], "2026-08-26T12:30:00Z")
            self.assertFalse(item["cleanup_eligible"])

    def test_recovery_retention_starts_only_after_validator_proves_settlement(self):
        from scripts.interventions import (
            InterventionError,
            publish_recovery_required,
            validate_recovery_required,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "interventions"
            journal = Path(temp_dir) / "skills" / ".demo.transaction-uncertain"
            journal.mkdir(parents=True)
            (journal / "state.json").write_text("{}", encoding="utf-8")
            (journal / ".skills-updater-transaction").write_text("1\n", encoding="utf-8")
            record = publish_recovery_required(
                root,
                "demo",
                journal,
                now=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
            manifest_path = record / "manifest.json"
            before = manifest_path.read_bytes()

            with self.assertRaises(InterventionError):
                validate_recovery_required(
                    root,
                    record.name,
                    lambda _: "uncertain",
                    now=datetime(2026, 8, 11, 12, 30, tzinfo=timezone.utc),
                )

            self.assertEqual(manifest_path.read_bytes(), before)
            observed_journals: list[Path] = []

            def prove_committed(path: Path) -> str:
                observed_journals.append(path)
                return "committed"

            item = validate_recovery_required(
                root,
                record.name,
                prove_committed,
                now=datetime(2026, 8, 11, 12, 30, tzinfo=timezone.utc),
            )

            self.assertEqual(observed_journals, [journal.resolve()])
            self.assertEqual(item["recovery_state"], "committed")
            self.assertEqual(item["retention_started_at"], "2026-08-11T12:30:00Z")
            self.assertEqual(item["retention_expires_at"], "2026-08-26T12:30:00Z")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(manifest["validatedJournalEvidence"]),
                {"markerSha256", "stateSha256"},
            )

    def test_cleanup_rejects_journal_changed_after_validation(self):
        from scripts.interventions import (
            InterventionError,
            cleanup_intervention,
            publish_recovery_required,
            validate_recovery_required,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "interventions"
            journal = Path(temp_dir) / "skills" / ".demo.transaction-changed"
            journal.mkdir(parents=True)
            (journal / "state.json").write_text("{}", encoding="utf-8")
            (journal / ".skills-updater-transaction").write_text("1\n", encoding="utf-8")
            record = publish_recovery_required(
                root,
                "demo",
                journal,
                now=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
            validate_recovery_required(
                root,
                record.name,
                lambda _: "committed",
                now=datetime(2026, 8, 2, tzinfo=timezone.utc),
            )
            (journal / "state.json").write_text('{"changed":true}', encoding="utf-8")

            with self.assertRaisesRegex(InterventionError, "identity changed"):
                cleanup_intervention(
                    root,
                    record.name,
                    now=datetime(2026, 8, 18, tzinfo=timezone.utc),
                )

            self.assertTrue(record.is_dir())
            self.assertTrue(journal.is_dir())

    def test_cleanup_rejects_unsafe_selectors_and_cleans_expired_record_idempotently(self):
        from scripts.interventions import InterventionError, cleanup_intervention

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "interventions"
            artifact_id = "demo-content-conflict-0123456789abcdef"
            record = root / artifact_id
            record.mkdir(parents=True)
            (record / "manifest.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "artifactId": artifact_id,
                        "recordType": "content-conflict",
                        "skillName": "demo",
                        "createdAt": "2026-08-01T00:00:00Z",
                        "resolutionState": "resolved",
                        "recoveryState": None,
                        "retentionStartedAt": "2026-08-01T00:00:00Z",
                        "retentionExpiresAt": "2026-08-16T00:00:00Z",
                        "retentionGroup": [
                            {"role": "intervention-record", "path": artifact_id},
                        ],
                        "diagnosticReferences": ["conflicts/SKILL.md"],
                    }
                ),
                encoding="utf-8",
            )
            for unsafe in ("../demo", str(record), "demo*", ""):
                with self.subTest(selector=unsafe):
                    with self.assertRaises(InterventionError):
                        cleanup_intervention(
                            root,
                            unsafe,
                            now=datetime(2026, 8, 17, tzinfo=timezone.utc),
                        )
                    self.assertTrue(record.is_dir())

            result = cleanup_intervention(
                root,
                artifact_id,
                now=datetime(2026, 8, 17, tzinfo=timezone.utc),
            )
            repeated = cleanup_intervention(
                root,
                artifact_id,
                now=datetime(2026, 8, 17, tzinfo=timezone.utc),
            )

            self.assertEqual(result["status"], "cleaned")
            self.assertTrue(result["cleaned"])
            self.assertEqual(repeated["status"], "already_cleaned")
            self.assertFalse(record.exists())
            self.assertEqual(list(root.glob(f".tombstone-{artifact_id}*")), [])

    def test_cleanup_failure_preserves_proven_recovery_state_and_resumes_tombstone(self):
        from scripts.interventions import (
            cleanup_intervention,
            publish_recovery_required,
            validate_recovery_required,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "interventions"
            journal = Path(temp_dir) / "skills" / ".demo.transaction-uncertain"
            journal.mkdir(parents=True)
            (journal / "state.json").write_text("{}", encoding="utf-8")
            (journal / ".skills-updater-transaction").write_text("1\n", encoding="utf-8")
            record = publish_recovery_required(
                root,
                "demo",
                journal,
                now=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
            validate_recovery_required(
                root,
                record.name,
                lambda _: "rolled_back",
                now=datetime(2026, 8, 2, tzinfo=timezone.utc),
            )

            with mock.patch(
                "scripts.interventions._remove_tombstone",
                side_effect=OSError("injected cleanup failure"),
            ):
                failed = cleanup_intervention(
                    root,
                    record.name,
                    now=datetime(2026, 8, 18, tzinfo=timezone.utc),
                )

            self.assertEqual(failed["status"], "error")
            self.assertEqual(failed["installed_state"], "rolled_back")
            self.assertFalse(failed["cleaned"])
            self.assertIn("injected cleanup failure", failed["error_message"])
            residue = Path(failed["cleanup_residue"])
            self.assertTrue(residue.is_dir())
            self.assertFalse(record.exists())
            self.assertFalse(journal.exists())
            from scripts.interventions import inventory_interventions

            residue_inventory = inventory_interventions(root)
            self.assertEqual(len(residue_inventory), 1)
            self.assertEqual(residue_inventory[0]["artifact_id"], record.name)
            self.assertEqual(residue_inventory[0]["recovery_state"], "rolled_back")
            self.assertEqual(residue_inventory[0]["cleanup_status"], "residue")
            self.assertEqual(residue_inventory[0]["cleanup_residue"], str(residue))

            resumed = cleanup_intervention(
                root,
                record.name,
                now=datetime(2026, 8, 18, tzinfo=timezone.utc),
            )
            self.assertEqual(resumed["status"], "cleaned")
            self.assertEqual(resumed["installed_state"], "rolled_back")
            self.assertFalse(residue.exists())

    def test_partial_tombstone_deletion_keeps_intent_and_resumes(self):
        from scripts.interventions import (
            cleanup_intervention,
            publish_recovery_required,
            validate_recovery_required,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "interventions"
            journal = Path(temp_dir) / "skills" / ".demo.transaction-partial-delete"
            journal.mkdir(parents=True)
            (journal / "state.json").write_text("{}", encoding="utf-8")
            (journal / ".skills-updater-transaction").write_text("1\n", encoding="utf-8")
            record = publish_recovery_required(
                root,
                "demo",
                journal,
                now=datetime(2026, 7, 31, tzinfo=timezone.utc),
            )
            validate_recovery_required(
                root,
                record.name,
                lambda _: "committed",
                now=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
            real_rmtree = shutil.rmtree

            def fail_on_journal(path, *args, **kwargs):
                if Path(path).name == "diagnostic-journal":
                    raise OSError("injected member deletion failure")
                return real_rmtree(path, *args, **kwargs)

            with mock.patch("scripts.interventions.shutil.rmtree", side_effect=fail_on_journal):
                failed = cleanup_intervention(
                    root,
                    record.name,
                    now=datetime(2026, 8, 17, tzinfo=timezone.utc),
                )

            self.assertEqual(failed["status"], "error")
            residue = Path(failed["cleanup_residue"])
            self.assertTrue((residue / "tombstone.json").is_file())
            resumed = cleanup_intervention(
                root,
                record.name,
                now=datetime(2026, 8, 17, tzinfo=timezone.utc),
            )
            self.assertEqual(resumed["status"], "cleaned")
            self.assertFalse(residue.exists())

    def test_final_tombstone_removal_failure_keeps_completion_receipt_and_resumes(self):
        from scripts.interventions import cleanup_intervention, inventory_interventions

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "interventions"
            artifact_id = "demo-content-conflict-final-rmdir"
            record = root / artifact_id
            record.mkdir(parents=True)
            (record / "manifest.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "artifactId": artifact_id,
                        "recordType": "content-conflict",
                        "skillName": "demo",
                        "createdAt": "2026-07-31T00:00:00Z",
                        "resolutionState": "resolved",
                        "recoveryState": None,
                        "retentionStartedAt": "2026-08-01T00:00:00Z",
                        "retentionExpiresAt": "2026-08-16T00:00:00Z",
                        "retentionGroup": [
                            {"role": "intervention-record", "path": artifact_id},
                        ],
                        "diagnosticReferences": ["conflicts/SKILL.md"],
                    }
                ),
                encoding="utf-8",
            )
            tombstone = root / f".tombstone-{artifact_id}"
            completion = Path(f"{tombstone}.complete.json")
            real_rmdir = os.rmdir

            def fail_final_rmdir(path, *args, **kwargs):
                if Path(path) == tombstone:
                    raise OSError("injected final tombstone removal failure")
                return real_rmdir(path, *args, **kwargs)

            with mock.patch("scripts.interventions.os.rmdir", side_effect=fail_final_rmdir):
                failed = cleanup_intervention(
                    root,
                    artifact_id,
                    now=datetime(2026, 8, 17, tzinfo=timezone.utc),
                )

            self.assertEqual(failed["status"], "error")
            self.assertTrue(tombstone.is_dir())
            self.assertTrue(completion.is_file())
            self.assertFalse((tombstone / "tombstone.json").exists())

            inventory = inventory_interventions(root)
            self.assertEqual(len(inventory), 1)
            self.assertEqual(inventory[0]["artifact_id"], artifact_id)
            self.assertEqual(inventory[0]["cleanup_status"], "residue")

            resumed = cleanup_intervention(
                root,
                artifact_id,
                now=datetime(2026, 8, 17, tzinfo=timezone.utc),
            )
            self.assertEqual(resumed["status"], "cleaned")
            self.assertFalse(tombstone.exists())
            self.assertFalse(completion.exists())

    def test_inventory_rejects_tombstone_identity_mismatch(self):
        from scripts.interventions import InterventionError, inventory_interventions

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "interventions"
            tombstone = root / ".tombstone-wrong-artifact"
            tombstone.mkdir(parents=True)
            (tombstone / "tombstone.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "artifactId": "right-artifact",
                        "recordType": "content-conflict",
                        "installedState": "unchanged",
                        "inventory": {},
                        "members": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(InterventionError, "identity mismatch"):
                inventory_interventions(root)

    def test_cleanup_rejects_recovery_group_when_journal_is_missing(self):
        from scripts.interventions import (
            InterventionError,
            cleanup_intervention,
            publish_recovery_required,
            validate_recovery_required,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "interventions"
            journal = Path(temp_dir) / "skills" / ".demo.transaction-missing"
            journal.mkdir(parents=True)
            (journal / "state.json").write_text("{}", encoding="utf-8")
            (journal / ".skills-updater-transaction").write_text("1\n", encoding="utf-8")
            record = publish_recovery_required(
                root,
                "demo",
                journal,
                now=datetime(2026, 7, 31, tzinfo=timezone.utc),
            )
            validate_recovery_required(
                root,
                record.name,
                lambda _: "rolled_back",
                now=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
            shutil.rmtree(journal)

            with self.assertRaises(InterventionError):
                cleanup_intervention(
                    root,
                    record.name,
                    now=datetime(2026, 8, 17, tzinfo=timezone.utc),
                )

            self.assertTrue(record.is_dir())

    def test_forged_tombstone_cannot_delete_an_arbitrary_source(self):
        from scripts.interventions import cleanup_intervention

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "interventions"
            artifact_id = "recovery-required-0123456789abcdef"
            tombstone = root / f".tombstone-{artifact_id}"
            tombstone.mkdir(parents=True)
            arbitrary = Path(temp_dir) / "unrelated-data"
            arbitrary.mkdir()
            (arbitrary / "keep.txt").write_text("keep", encoding="utf-8")
            (tombstone / "tombstone.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "artifactId": artifact_id,
                        "recordType": "recovery-required",
                        "installedState": "rolled_back",
                        "inventory": {
                            "artifact_id": artifact_id,
                            "record_type": "recovery-required",
                            "skill_name": "demo",
                            "resolution_state": None,
                            "recovery_state": "rolled_back",
                            "retention_started_at": "2026-08-01T00:00:00Z",
                            "retention_expires_at": "2026-08-16T00:00:00Z",
                            "retention_group": [],
                            "diagnostic_references": [str(arbitrary)],
                            "cleanup_eligible": True,
                        },
                        "members": [
                            {
                                "role": "intervention-record",
                                "source": str(arbitrary),
                                "destination": str(tombstone / "intervention-record"),
                                "required": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = cleanup_intervention(root, artifact_id)

            self.assertEqual(result["status"], "error")
            self.assertTrue((arbitrary / "keep.txt").is_file())

    def test_real_recovery_validator_reads_only_the_referenced_journal(self):
        from scripts.agent_skill_updater import validate_diagnostic_journal
        from scripts.interventions import (
            publish_recovery_required,
            validate_recovery_required,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_root = root / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            original = b'{"installedBaseVersion":"old"}\n'
            expected = b'{"installedBaseVersion":"new"}\n'
            metadata_path.write_bytes(original)
            journal = skills_root / ".demo.metadata-update-recovery"
            journal.mkdir()
            (journal / "metadata.before").write_bytes(original)
            (journal / "metadata.expected").write_bytes(expected)
            (journal / "metadata.publish").write_bytes(expected)
            (journal / "metadata.displaced").write_bytes(original)
            (journal / "state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "transactionType": "metadata",
                        "skillName": "demo",
                        "skillDir": str(skill_dir.resolve()),
                        "phase": "rolled_back",
                        "metadataPhase": "prepared",
                        "originalMetadataPresent": True,
                        "originalMetadataSha256": hashlib.sha256(original).hexdigest(),
                        "expectedMetadataPresent": True,
                        "expectedMetadataSha256": hashlib.sha256(expected).hexdigest(),
                        "targetVersion": "new",
                    }
                ),
                encoding="utf-8",
            )
            (journal / ".skills-updater-transaction").write_text("1\n", encoding="utf-8")
            record = publish_recovery_required(root / "interventions", "demo", journal)

            item = validate_recovery_required(
                root / "interventions",
                record.name,
                validate_diagnostic_journal,
                now=datetime(2026, 8, 11, tzinfo=timezone.utc),
            )

            self.assertEqual(item["recovery_state"], "rolled_back")
            self.assertEqual(metadata_path.read_bytes(), original)
            self.assertTrue(journal.exists())


if __name__ == "__main__":
    unittest.main()
