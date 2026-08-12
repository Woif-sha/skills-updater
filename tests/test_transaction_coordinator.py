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
    def test_snapshot_startup_cleanup_failure_preserves_proven_installed_state(self):
        import scripts.agent_skill_updater as updater

        for phase, installed_state, applied in (
            (updater.TRANSACTION_PHASE_COMMITTED, "committed", True),
            (updater.TRANSACTION_PHASE_ROLLED_BACK, "rolled_back", False),
        ):
            with self.subTest(phase=phase):
                with tempfile.TemporaryDirectory() as temp_dir:
                    skills_root = Path(temp_dir) / "skills"
                    skill_dir = skills_root / "demo"
                    journal = skills_root / f".demo.update-{phase}"
                    skill_dir.mkdir(parents=True)
                    journal.mkdir()
                    payload = "new\n" if applied else "old\n"
                    (skill_dir / "SKILL.md").write_text(payload, encoding="utf-8")
                    metadata = b'{"installedBaseVersion":"bbbbbbbbbbbb"}'
                    (skill_dir / ".openskills.json").write_bytes(metadata)
                    signature = updater.directory_signature(skill_dir)
                    state = {
                        "version": updater.TRANSACTION_STATE_VERSION,
                        "transactionType": updater.SNAPSHOT_TRANSACTION_TYPE,
                        "skillName": "demo",
                        "skillDir": str(skill_dir.resolve()),
                        "phase": phase,
                        "metadataPhase": updater.METADATA_PHASE_PUBLISHED,
                        "originalSignature": signature,
                        "expectedSignature": signature,
                        "originalMetadataPresent": True,
                        "originalMetadataSha256": hashlib.sha256(metadata).hexdigest(),
                        "expectedMetadataPresent": True,
                        "expectedMetadataSha256": hashlib.sha256(metadata).hexdigest(),
                        "rollbackProven": True,
                    }
                    (journal / "state.json").write_text(json.dumps(state), encoding="utf-8")

                    with mock.patch(
                        "scripts.agent_skill_updater._remove_transaction_tree",
                        side_effect=updater.AgentSkillUpdaterError("cleanup interrupted"),
                    ):
                        outcome = updater.recover_updates(skills_root)[0]

                    self.assertEqual(outcome.installed_state, installed_state)
                    self.assertEqual(outcome.applied, applied)
                    self.assertEqual(outcome.cleanup_residue, journal)
                    self.assertIn("cleanup interrupted", outcome.error_message)
                    self.assertIsNone(outcome.intervention_record)
                    self.assertTrue(journal.is_dir())

    def test_committed_snapshot_preserves_outcome_when_workspace_cleanup_fails(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("old\n", encoding="utf-8")
            (skill_dir / ".openskills.json").write_text(
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
            workspace = root / "workspace"
            payload = workspace / "payload"
            payload.mkdir(parents=True)
            (payload / "SKILL.md").write_text("new\n", encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            prepared = updater.PreparedPayload(
                payload_dir=payload,
                payload_signature=updater.directory_signature(payload),
                original_signature=updater.directory_signature(skill_dir),
                exact_base_revision=base,
                workspace_root=workspace,
            )
            real_rmtree = updater.shutil.rmtree

            def fail_workspace_cleanup(path, *args, **kwargs):
                if Path(path) == workspace:
                    raise OSError("injected workspace cleanup failure")
                return real_rmtree(path, *args, **kwargs)

            with mock.patch.object(updater.shutil, "rmtree", side_effect=fail_workspace_cleanup):
                outcome = updater.apply_observed_update(
                    source,
                    updater.RemoteObservation.from_source(
                        source,
                        revision=remote,
                        version=remote,
                    ),
                    installed_base_version=base,
                    prepared_payload=prepared,
                )

            self.assertEqual(outcome.status, "error")
            self.assertEqual(outcome.installed_state, "committed")
            self.assertTrue(outcome.applied)
            self.assertEqual(outcome.action, "payload_merged")
            self.assertEqual(outcome.cleanup_residue, workspace)
            self.assertIn("injected workspace cleanup failure", outcome.error_message)

    def test_snapshot_clean_update_commits_without_successful_backup(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("old\n", encoding="utf-8")
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
            expected_dir = Path(temp_dir) / "prepared"
            expected_dir.mkdir()
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            prepared = updater.PreparedPayload(
                payload_dir=expected_dir,
                payload_signature=updater.directory_signature(expected_dir),
                original_signature=updater.directory_signature(skill_dir),
                exact_base_revision=base,
            )

            outcome = updater.apply_observed_update(
                source,
                updater.RemoteObservation.from_source(
                    source,
                    revision=remote,
                    version=remote,
                ),
                installed_base_version=base,
                prepared_payload=prepared,
            )

            self.assertEqual(outcome.status, "up_to_date")
            self.assertEqual(outcome.installed_state, "committed")
            self.assertTrue(outcome.applied)
            self.assertEqual(outcome.action, "payload_merged")
            self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), "new\n")
            self.assertEqual(
                json.loads(metadata_path.read_text(encoding="utf-8"))["installedBaseVersion"],
                remote,
            )
            self.assertEqual(list(skills_root.glob(".backup-*")), [])
            self.assertEqual(list(skills_root.glob(".demo.transaction-*")), [])

    def test_snapshot_content_conflict_promotes_complete_intervention_record(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_root = root / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("local\n", encoding="utf-8")
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
            materials = root / "materials"
            for name, content in (
                ("base", "base\n"),
                ("local", "local\n"),
                ("remote", "remote\n"),
                ("conflicts", "<<<<<<< local\n=======\n>>>>>>> remote\n"),
            ):
                directory = materials / name
                directory.mkdir(parents=True)
                (directory / "SKILL.md").write_text(content, encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            prepared = updater.PreparedPayload(
                payload_dir=None,
                payload_signature=None,
                original_signature=updater.directory_signature(skill_dir),
                exact_base_revision=base,
                base_dir=materials / "base",
                local_dir=materials / "local",
                remote_dir=materials / "remote",
                conflict_dir=materials / "conflicts",
                conflicts=("SKILL.md",),
            )
            interventions_root = root / "interventions"

            with mock.patch.object(
                updater,
                "get_interventions_dir",
                return_value=interventions_root,
            ):
                outcome = updater.apply_observed_update(
                    source,
                    updater.RemoteObservation.from_source(
                        source,
                        revision=remote,
                        version=remote,
                    ),
                    installed_base_version=base,
                    prepared_payload=prepared,
                )

            self.assertEqual(outcome.status, "error")
            self.assertEqual(outcome.installed_state, "unchanged")
            self.assertFalse(outcome.applied)
            self.assertEqual(outcome.action, "intervention_required")
            self.assertIsNotNone(outcome.intervention_record)
            self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), "local\n")
            record = outcome.intervention_record
            self.assertEqual(
                {path.name for path in record.iterdir()},
                {"manifest.json", "base", "local", "remote", "conflicts"},
            )
            manifest = json.loads((record / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["kind"], "content-conflict")
            self.assertEqual(manifest["installedState"], "unchanged")
            self.assertEqual(manifest["exactBaseRevision"], base)
            self.assertEqual(manifest["targetRevision"], remote)
            self.assertEqual(manifest["conflicts"], ["SKILL.md"])
            from scripts.interventions import inventory_interventions

            inventory = inventory_interventions(interventions_root)
            self.assertEqual(len(inventory), 1)
            self.assertEqual(inventory[0]["artifact_id"], manifest["artifactId"])
            self.assertEqual(inventory[0]["record_type"], "content-conflict")
            self.assertEqual(inventory[0]["resolution_state"], "unresolved")
            self.assertIsNone(inventory[0]["retention_expires_at"])
            self.assertEqual(
                inventory[0]["diagnostic_references"],
                ["conflicts/SKILL.md"],
            )
            self.assertEqual(list(interventions_root.glob(".draft-*")), [])
            self.assertEqual(list(skills_root.glob("*.merge-conflicts")), [])

    def test_snapshot_concurrent_change_cancels_before_mutation(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text("old\n", encoding="utf-8")
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
            expected_dir = Path(temp_dir) / "prepared"
            expected_dir.mkdir()
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            prepared = updater.PreparedPayload(
                payload_dir=expected_dir,
                payload_signature=updater.directory_signature(expected_dir),
                original_signature=updater.directory_signature(skill_dir),
                exact_base_revision=base,
            )
            skill_file.write_text("concurrent\n", encoding="utf-8")

            outcome = updater.apply_observed_update(
                source,
                updater.RemoteObservation.from_source(
                    source,
                    revision=remote,
                    version=remote,
                ),
                installed_base_version=base,
                prepared_payload=prepared,
            )

            self.assertEqual(outcome.status, "error")
            self.assertEqual(outcome.installed_state, "unchanged")
            self.assertFalse(outcome.applied)
            self.assertEqual(skill_file.read_text(encoding="utf-8"), "concurrent\n")
            self.assertEqual(list(skills_root.glob(".demo.transaction-*")), [])

    def test_snapshot_metadata_failure_returns_verified_rollback(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text("old\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            original_metadata = json.dumps(
                {
                    "source": "example/demo",
                    "sourceType": "git",
                    "repoUrl": "https://github.com/example/demo",
                    "subpath": ".",
                    "installedBaseVersion": base,
                }
            ).encode("utf-8")
            metadata_path.write_bytes(original_metadata)
            expected_dir = Path(temp_dir) / "prepared"
            expected_dir.mkdir()
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            prepared = updater.PreparedPayload(
                payload_dir=expected_dir,
                payload_signature=updater.directory_signature(expected_dir),
                original_signature=updater.directory_signature(skill_dir),
                exact_base_revision=base,
            )
            real_link = updater.os.link

            def fail_metadata_publication(source_path, destination_path):
                if Path(source_path).name == "metadata.publish":
                    raise OSError("injected metadata publication failure")
                return real_link(source_path, destination_path)

            with mock.patch.object(updater.os, "link", side_effect=fail_metadata_publication):
                outcome = updater.apply_observed_update(
                    source,
                    updater.RemoteObservation.from_source(
                        source,
                        revision=remote,
                        version=remote,
                    ),
                    installed_base_version=base,
                    prepared_payload=prepared,
                )

            self.assertEqual(outcome.status, "error")
            self.assertEqual(outcome.installed_state, "rolled_back")
            self.assertFalse(outcome.applied)
            self.assertIn("injected metadata publication failure", outcome.error_message)
            self.assertEqual(skill_file.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(metadata_path.read_bytes(), original_metadata)
            self.assertEqual(list(skills_root.glob(".demo.transaction-*")), [])

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
                        updater.RemoteObservation.from_source(
                            source, revision="b" * 40, version="b" * 40
                        ),
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
                        updater.RemoteObservation.from_source(
                            source, revision="b" * 40, version="b" * 40
                        ),
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
                RemoteObservation.from_source(source, revision=remote, version=remote),
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

    def test_generated_observation_keeps_revision_separate_from_version(self):
        import scripts.agent_skill_updater as updater

        revision = "b" * 40
        version = "2.0.0"
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "openspec-explore"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: openspec-explore\ngeneratedBy: 1.0.0\n---\n",
                encoding="utf-8",
            )
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "source": "Fission-AI/OpenSpec",
                        "sourceType": "git-generated",
                        "repoUrl": "https://github.com/Fission-AI/OpenSpec",
                        "subpath": ".",
                        "generator": "dist/core/shared/skill-generation.js",
                        "workflowId": "explore",
                        "installedBaseVersion": "1.0.0",
                    }
                ),
                encoding="utf-8",
            )
            source = updater.load_agent_skill_source(skill_dir)

            with mock.patch(
                "scripts.agent_skill_updater._commit_transaction_metadata",
                side_effect=KeyboardInterrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    updater.apply_observed_update(
                        source,
                        updater.RemoteObservation.from_source(
                            source,
                            revision=revision,
                            version=version,
                        ),
                        installed_base_version="1.0.0",
                    )

            transaction = next(skills_root.glob(".openspec-explore.transaction-*"))
            state = json.loads((transaction / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["targetRevision"], revision)
            self.assertEqual(state["targetVersion"], version)

            outcomes = updater.recover_updates(skills_root)

            self.assertEqual(outcomes[0].version, version)
            self.assertEqual(outcomes[0].installed_state, "unchanged")

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
                    updater.RemoteObservation.from_source(
                        source, revision=remote, version=remote
                    ),
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

            interventions_root = Path(temp_dir) / "interventions"
            with mock.patch(
                "scripts.agent_skill_updater.get_interventions_dir",
                return_value=interventions_root,
            ):
                outcomes = recover_updates(skills_root)

            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0].status, "error")
            self.assertEqual(outcomes[0].installed_state, "uncertain")
            self.assertEqual(outcomes[0].diagnostic_journal, transaction)
            self.assertIsNotNone(outcomes[0].intervention_record)
            self.assertTrue(transaction.exists())
            record = outcomes[0].intervention_record
            self.assertEqual({path.name for path in record.iterdir()}, {"manifest.json"})
            from scripts.interventions import inventory_interventions

            inventory = inventory_interventions(interventions_root)
            self.assertEqual(inventory[0]["record_type"], "recovery-required")
            self.assertEqual(inventory[0]["recovery_state"], "required")
            self.assertIsNone(inventory[0]["retention_expires_at"])
            self.assertEqual(
                inventory[0]["diagnostic_references"],
                [str(transaction.resolve())],
            )

    def test_registry_recovery_maps_missing_coordinator_state_to_uncertain(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            transaction = skills_root / ".demo.transaction-missing-state"
            transaction.mkdir()
            (transaction / ".skills-updater-transaction").write_text(
                "1\n",
                encoding="utf-8",
            )

            with self.assertRaises(updater.AgentSkillRecoveryUncertainError) as error:
                updater.recover_incomplete_skill_transactions(skills_root)

            outcome = error.exception.outcome
            self.assertEqual(outcome.installed_state, "uncertain")
            self.assertEqual(outcome.diagnostic_journal, transaction)
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
            def interrupt_after_durable_intent(transaction_root, state, *_args, **_kwargs):
                updater._set_coordinator_phase(
                    transaction_root,
                    state,
                    updater.COORDINATOR_PHASE_CAPTURING_METADATA,
                )
                raise KeyboardInterrupt()

            with mock.patch(
                "scripts.agent_skill_updater._commit_transaction_metadata",
                side_effect=interrupt_after_durable_intent,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    updater.apply_observed_update(
                        source,
                        updater.RemoteObservation.from_source(
                            source, revision=remote, version=remote
                        ),
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
                        updater.RemoteObservation.from_source(
                            source, revision=remote, version=remote
                        ),
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
                        return_value=cli.EntryProbe(
                            "up_to_date",
                            remote[:12],
                            remote,
                            remote_observation=mock.sentinel.remote_observation,
                        ),
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

    def test_json_cli_preserves_uncertain_startup_recovery_journal(self):
        import scripts.update_agent_skills as cli
        from scripts.agent_skill_updater import (
            AgentSkillRecoveryUncertainError,
            TransactionOutcome,
        )

        journal = Path(r"C:\skills\.demo.transaction-damaged")
        outcome = TransactionOutcome(
            name="demo",
            status="error",
            installed_state="uncertain",
            applied=False,
            action="none",
            error_message="Recovery evidence is damaged.",
            diagnostic_journal=journal,
        )
        output = io.StringIO()

        with mock.patch.object(sys, "argv", ["update_agent_skills.py", "--json"]):
            with mock.patch.object(
                cli,
                "sync_registry",
                side_effect=AgentSkillRecoveryUncertainError(outcome),
            ):
                with self.assertRaises(SystemExit) as exit_info:
                    with redirect_stdout(output):
                        cli.main()

        self.assertEqual(exit_info.exception.code, 1)
        item = json.loads(output.getvalue())[0]
        self.assertEqual(item["installed_state"], "uncertain")
        self.assertEqual(item["diagnostic_journal"], str(journal))


if __name__ == "__main__":
    unittest.main()
