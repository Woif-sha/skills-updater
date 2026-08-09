import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


class TransactionCoordinatorTests(unittest.TestCase):
    def test_apply_rejects_local_only_before_lock_or_journal(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "demo"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(
                json.dumps({"updatePolicy": "local-only"}),
                encoding="utf-8",
            )
            source = updater.load_agent_skill_source(skill_dir)

            with mock.patch(
                "scripts.agent_skill_updater.skill_update_lock",
                side_effect=AssertionError("local-only must fail before locking"),
            ):
                with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "local-only"):
                    updater.apply_observed_update(
                        source,
                        updater.RemoteObservation(revision="b" * 40, version="b" * 40),
                        installed_base_version="a" * 40,
                    )

            self.assertEqual(list(skill_dir.parent.glob(".demo.transaction-*")), [])

    def test_apply_rejects_missing_provenance_before_lock_or_journal(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "demo"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "sourceType": "git",
                        "installedBaseVersion": "a" * 40,
                    }
                ),
                encoding="utf-8",
            )
            source = updater.AgentSkillSource(
                "demo",
                skill_dir,
                None,
                "git",
                None,
                None,
                None,
                None,
                metadata_path,
                entry_type="single-skill",
            )

            with mock.patch(
                "scripts.agent_skill_updater.skill_update_lock",
                side_effect=AssertionError("invalid provenance must fail before locking"),
            ):
                with self.assertRaisesRegex(
                    updater.AgentSkillUpdaterError,
                    "missing remote source fields",
                ):
                    updater.apply_observed_update(
                        source,
                        updater.RemoteObservation(revision="b" * 40, version="b" * 40),
                        installed_base_version="a" * 40,
                    )

            self.assertEqual(list(skill_dir.parent.glob(".demo.transaction-*")), [])

    def test_metadata_only_update_commits_through_coordinator(self):
        from scripts.agent_skill_updater import (
            AgentSkillSource,
            RemoteObservation,
            apply_observed_update,
        )

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "source": "example/demo",
                        "sourceType": "git",
                        "repoUrl": "https://github.com/example/demo",
                        "subpath": ".",
                        "installedBaseVersion": base,
                    }
                ),
                encoding="utf-8",
            )
            source = AgentSkillSource(
                "demo",
                skill_dir,
                "example/demo",
                "git",
                "https://github.com/example/demo",
                ".",
                None,
                None,
                metadata_path,
                entry_type="single-skill",
            )

            outcome = apply_observed_update(
                source,
                RemoteObservation(revision=remote, version=remote),
                installed_base_version=base,
            )

            self.assertEqual(outcome.status, "up_to_date")
            self.assertEqual(outcome.installed_state, "committed")
            self.assertTrue(outcome.applied)
            self.assertEqual(outcome.action, "metadata_refreshed")
            self.assertEqual(outcome.version, remote)
            self.assertEqual(
                json.loads(metadata_path.read_text(encoding="utf-8"))["installedBaseVersion"],
                remote,
            )
            self.assertEqual(list(skills_root.glob(".demo.transaction-*")), [])

    def test_metadata_publish_failure_returns_verified_rollback(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            original = json.dumps(
                {
                    "source": "example/demo",
                    "sourceType": "git",
                    "repoUrl": "https://github.com/example/demo",
                    "subpath": ".",
                    "installedBaseVersion": base,
                }
            ).encode("utf-8")
            metadata_path.write_bytes(original)
            source = updater.AgentSkillSource(
                "demo",
                skill_dir,
                "example/demo",
                "git",
                "https://github.com/example/demo",
                ".",
                None,
                None,
                metadata_path,
                entry_type="single-skill",
            )
            real_publish = updater._publish_metadata_file_if_absent
            failed = {"value": False}

            def fail_expected_publication(source_path, destination_path):
                if Path(source_path).name == "metadata.publish" and not failed["value"]:
                    failed["value"] = True
                    raise updater.AgentSkillUpdaterError("injected publication failure")
                return real_publish(source_path, destination_path)

            with mock.patch(
                "scripts.agent_skill_updater._publish_metadata_file_if_absent",
                side_effect=fail_expected_publication,
            ):
                outcome = updater.apply_observed_update(
                    source,
                    updater.RemoteObservation(revision=remote, version=remote),
                    installed_base_version=base,
                )

            self.assertTrue(failed["value"])
            self.assertEqual(outcome.status, "error")
            self.assertEqual(outcome.installed_state, "rolled_back")
            self.assertFalse(outcome.applied)
            self.assertIn("injected publication failure", outcome.error_message)
            self.assertEqual(metadata_path.read_bytes(), original)
            self.assertEqual(list(skills_root.glob(".demo.transaction-*")), [])

    def test_recover_updates_decodes_metadata_v1_without_rewriting_it(self):
        from scripts.agent_skill_updater import recover_updates

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            original = json.dumps({"installedBaseVersion": base}).encode("utf-8")
            expected = json.dumps({"installedBaseVersion": remote}).encode("utf-8")
            transaction = skills_root / ".demo.metadata-update-crash"
            transaction.mkdir()
            (transaction / "metadata.before").write_bytes(original)
            (transaction / "metadata.expected").write_bytes(expected)
            (transaction / "metadata.publish").write_bytes(expected)
            (transaction / "metadata.displaced").write_bytes(original)
            legacy_state = {
                "version": 1,
                "transactionType": "metadata",
                "skillName": "demo",
                "skillDir": str(skill_dir.resolve()),
                "phase": "applying",
                "metadataPhase": "captured",
                "originalMetadataPresent": True,
                "originalMetadataSha256": hashlib.sha256(original).hexdigest(),
                "expectedMetadataPresent": True,
                "expectedMetadataSha256": hashlib.sha256(expected).hexdigest(),
                "targetVersion": remote,
            }
            state_path = transaction / "state.json"
            state_path.write_text(json.dumps(legacy_state), encoding="utf-8")
            (transaction / ".skills-updater-transaction").write_text("1\n", encoding="utf-8")

            with mock.patch(
                "scripts.agent_skill_updater._write_json_atomic",
                side_effect=AssertionError("legacy decoder must not rewrite v1 state"),
            ):
                outcomes = recover_updates(skills_root)

            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0].installed_state, "rolled_back")
            self.assertEqual(outcomes[0].status, "recovered")
            self.assertEqual((skill_dir / ".openskills.json").read_bytes(), original)
            self.assertFalse(transaction.exists())

    def test_recover_updates_preserves_unknown_journal_as_uncertain(self):
        from scripts.agent_skill_updater import recover_updates

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            transaction = skills_root / ".demo.transaction-unknown"
            transaction.mkdir()
            (transaction / "state.json").write_text(
                json.dumps(
                    {
                        "version": 999,
                        "transactionType": "coordinator",
                        "skillName": "demo",
                        "skillDir": str(skill_dir.resolve()),
                    }
                ),
                encoding="utf-8",
            )
            (transaction / ".skills-updater-transaction").write_text("1\n", encoding="utf-8")

            outcomes = recover_updates(skills_root)

            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0].status, "error")
            self.assertEqual(outcomes[0].installed_state, "uncertain")
            self.assertEqual(outcomes[0].diagnostic_journal, transaction)
            self.assertTrue(transaction.exists())

    def test_interrupted_metadata_transaction_uses_single_phase_envelope(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            original = json.dumps(
                {
                    "source": "example/demo",
                    "sourceType": "git",
                    "repoUrl": "https://github.com/example/demo",
                    "subpath": ".",
                    "installedBaseVersion": base,
                }
            ).encode("utf-8")
            metadata_path.write_bytes(original)
            source = updater.AgentSkillSource(
                "demo",
                skill_dir,
                "example/demo",
                "git",
                "https://github.com/example/demo",
                ".",
                None,
                None,
                metadata_path,
                entry_type="single-skill",
            )
            real_set_phase = updater._set_coordinator_phase

            def interrupt_after_durable_intent(transaction_root, state, phase):
                real_set_phase(transaction_root, state, phase)
                if phase == updater.COORDINATOR_PHASE_CAPTURING_METADATA:
                    raise KeyboardInterrupt()

            with mock.patch(
                "scripts.agent_skill_updater._set_coordinator_phase",
                side_effect=interrupt_after_durable_intent,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    updater.apply_observed_update(
                        source,
                        updater.RemoteObservation(revision=remote, version=remote),
                        installed_base_version=base,
                    )

            transactions = list(skills_root.glob(".demo.transaction-*"))
            self.assertEqual(len(transactions), 1)
            state = json.loads((transactions[0] / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], updater.COORDINATOR_PHASE_CAPTURING_METADATA)
            self.assertNotIn("metadataPhase", state)
            self.assertEqual(
                set(state["evidence"]),
                {"beforeMetadata", "expectedMetadata"},
            )
            self.assertFalse((transactions[0] / "original").exists())
            self.assertFalse((transactions[0] / "incoming").exists())

            outcomes = updater.recover_updates(skills_root)

            self.assertEqual(outcomes[0].installed_state, "rolled_back")
            self.assertEqual(metadata_path.read_bytes(), original)
            self.assertFalse(transactions[0].exists())

    def test_recover_updates_preserves_damaged_evidence_as_uncertain(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"installedBaseVersion": base}), encoding="utf-8")
            transaction = skills_root / ".demo.transaction-damaged"
            transaction.mkdir()
            (transaction / "metadata.before").write_bytes(b"before")
            (transaction / "metadata.expected").write_bytes(b"expected")
            (transaction / "metadata.publish").write_bytes(b"expected")
            (transaction / "state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "transactionType": "coordinator",
                        "transactionKind": "metadata-only",
                        "skillName": "demo",
                        "skillDir": str(skill_dir.resolve()),
                        "phase": updater.COORDINATOR_PHASE_CAPTURING_METADATA,
                        "targetRevision": remote,
                        "targetVersion": remote,
                        "evidence": {
                            "beforeMetadata": {
                                "present": True,
                                "sha256": "0" * 64,
                            },
                            "expectedMetadata": {
                                "present": True,
                                "sha256": hashlib.sha256(b"expected").hexdigest(),
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            (transaction / ".skills-updater-transaction").write_text("1\n", encoding="utf-8")

            outcome = updater.recover_updates(skills_root)[0]

            self.assertEqual(outcome.installed_state, "uncertain")
            self.assertEqual(outcome.diagnostic_journal, transaction)
            self.assertTrue(transaction.exists())

    def test_committed_recovery_cleanup_failure_remains_committed(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "source": "example/demo",
                        "sourceType": "git",
                        "repoUrl": "https://github.com/example/demo",
                        "subpath": ".",
                        "installedBaseVersion": base,
                    }
                ),
                encoding="utf-8",
            )
            source = updater.AgentSkillSource(
                "demo",
                skill_dir,
                "example/demo",
                "git",
                "https://github.com/example/demo",
                ".",
                None,
                None,
                metadata_path,
                entry_type="single-skill",
            )
            real_set_phase = updater._set_coordinator_phase

            def interrupt_after_commit(transaction_root, state, phase):
                real_set_phase(transaction_root, state, phase)
                if phase == updater.COORDINATOR_PHASE_COMMITTED:
                    raise KeyboardInterrupt()

            with mock.patch(
                "scripts.agent_skill_updater._set_coordinator_phase",
                side_effect=interrupt_after_commit,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    updater.apply_observed_update(
                        source,
                        updater.RemoteObservation(revision=remote, version=remote),
                        installed_base_version=base,
                    )
            transaction = next(skills_root.glob(".demo.transaction-*"))

            with mock.patch(
                "scripts.agent_skill_updater._remove_transaction_tree",
                side_effect=updater.AgentSkillUpdaterError("cleanup interrupted"),
            ):
                outcome = updater.recover_updates(skills_root)[0]

            self.assertEqual(outcome.installed_state, "committed")
            self.assertTrue(outcome.applied)
            self.assertEqual(outcome.cleanup_residue, transaction)
            self.assertTrue(transaction.exists())

    def test_json_cli_consumes_coordinator_outcome_additively(self):
        import scripts.update_agent_skills as cli
        from scripts.agent_skill_updater import TransactionOutcome

        base = "a" * 40
        remote = "b" * 40
        entry = {
            "name": "demo",
            "entryType": "single-skill",
            "updateMode": "snapshot",
            "path": r"C:\skills\demo",
            "repoUrl": "https://github.com/example/demo",
            "source": "example/demo",
            "sourceType": "git",
            "subpath": ".",
            "installedBaseVersion": base,
            "localVersion": remote,
            "managed": True,
        }
        registry = {
            "version": 2,
            "generatedAt": "2026-01-01T00:00:00+00:00",
            "skillsRoot": r"C:\skills",
            "entries": {"demo": entry},
        }
        outcome = TransactionOutcome(
            name="demo",
            status="up_to_date",
            installed_state="committed",
            applied=True,
            action="metadata_refreshed",
            version=remote,
        )
        output = io.StringIO()

        with mock.patch.object(
            sys,
            "argv",
            ["update_agent_skills.py", "--skill", "demo", "--json"],
        ):
            with mock.patch.object(cli, "sync_registry", side_effect=[registry, registry]):
                with mock.patch.object(cli, "update_registry_entries"):
                    with mock.patch.object(
                        cli,
                        "_probe_entry",
                        return_value=cli.EntryProbe("up_to_date", remote[:12], remote),
                    ):
                        with mock.patch.object(
                            cli,
                            "apply_observed_update",
                            return_value=outcome,
                        ) as apply:
                            with self.assertRaises(SystemExit) as exit_info:
                                with redirect_stdout(output):
                                    cli.main()

        self.assertEqual(exit_info.exception.code, 0)
        item = json.loads(output.getvalue())[0]
        self.assertEqual(item["action"], "metadata_refreshed")
        self.assertEqual(item["installed_state"], "committed")
        self.assertTrue(item["applied"])
        apply.assert_called_once()


if __name__ == "__main__":
    unittest.main()
