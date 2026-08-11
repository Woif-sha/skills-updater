import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock


def _prepared_payload(updater, skill_dir: Path, expected_dir: Path, base: str, observation):
    local_dir = expected_dir.parent / "prepared-local"
    updater._copy_directory_contents(skill_dir, local_dir)
    return updater.PreparedPayload(
        expected_dir,
        updater.directory_signature(expected_dir),
        updater.directory_signature(local_dir),
        base,
        remote_observation=observation,
        local_dir=local_dir,
    )


def _conflict_payload(updater, root: Path, skill_dir: Path, base: str, observation, local: str):
    materials = root
    for name, content in (
        ("base", "base\n"),
        ("local", local),
        ("remote", "remote\n"),
        ("conflicts", "conflict\n"),
    ):
        directory = materials / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(content, encoding="utf-8")
    return updater.PreparedPayload(
        None,
        None,
        updater.directory_signature(skill_dir),
        base,
        remote_observation=observation,
        base_signature=updater.directory_signature(materials / "base"),
        remote_signature=updater.directory_signature(materials / "remote"),
        base_dir=materials / "base",
        local_dir=materials / "local",
        remote_dir=materials / "remote",
        conflict_dir=materials / "conflicts",
        conflicts=("SKILL.md",),
        workspace_root=materials,
    )


class TransactionCoordinatorTests(unittest.TestCase):
    def test_snapshot_local_merge_commits_through_prepared_payload(self):
        import scripts.agent_skill_updater as updater

        base_revision = "a" * 40
        remote_revision = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "demo"
            remote_dir = root / "remote"
            base_dir = root / "base"
            skill_dir.mkdir(parents=True)
            remote_dir.mkdir()
            base_dir.mkdir()
            base_text = "# Demo\n\nRemote: old\n\nLocal: old\n"
            (base_dir / "SKILL.md").write_text(base_text, encoding="utf-8")
            (skill_dir / "SKILL.md").write_text(
                "# Demo\n\nRemote: old\n\nLocal: changed\n",
                encoding="utf-8",
            )
            (remote_dir / "SKILL.md").write_text(
                "# Demo\n\nRemote: changed\n\nLocal: old\n",
                encoding="utf-8",
            )
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base_revision}), encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(
                source,
                revision=remote_revision,
                version=remote_revision,
            )
            update = updater.AgentSkillUpdate(
                source=source,
                staged_dir=remote_dir,
                status="update_available",
                installed_base_version=base_revision,
                local_version=base_revision,
                remote_version=remote_revision,
                remote_observation=observation,
            )

            with mock.patch.object(updater, "_stage_git_skill_at_ref", return_value=base_dir):
                prepared = updater.prepare_snapshot_payload(update)
            workspace_root = prepared.workspace_root
            outcome = updater.apply_observed_update(
                source,
                observation,
                installed_base_version=base_revision,
                prepared_payload=prepared,
            )

            merged = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            self.assertEqual(outcome.installed_state, "committed")
            self.assertIn("Remote: changed", merged)
            self.assertIn("Local: changed", merged)
            self.assertFalse(workspace_root.exists())
            self.assertEqual(list(skill_dir.parent.glob(".backup-*")), [])

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
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            expected_dir = Path(temp_dir) / "prepared"
            expected_dir.mkdir()
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = _prepared_payload(updater, skill_dir, expected_dir, base, observation)

            outcome = updater.apply_observed_update(source, observation, installed_base_version=base, prepared_payload=prepared)

            self.assertEqual(outcome.installed_state, "committed")
            self.assertTrue(outcome.applied)
            self.assertEqual(outcome.action, "payload_merged")
            self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), "new\n")
            self.assertEqual(json.loads(metadata_path.read_text(encoding="utf-8"))["installedBaseVersion"], remote)
            self.assertEqual(list(skills_root.glob(".backup-*")), [])
            self.assertEqual(list(skills_root.glob(".demo.transaction-*")), [])

    def test_snapshot_equal_payload_routes_to_metadata_coordinator(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "demo"
            expected_dir = root / "prepared"
            skill_dir.mkdir(parents=True)
            expected_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("same\n", encoding="utf-8")
            (expected_dir / "SKILL.md").write_text("same\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = _prepared_payload(updater, skill_dir, expected_dir, base, observation)

            outcome = updater.apply_observed_update(
                source,
                observation,
                installed_base_version=base,
                prepared_payload=prepared,
            )

            self.assertEqual(outcome.installed_state, "committed")
            self.assertEqual(outcome.action, "metadata_refreshed")
            self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), "same\n")
            self.assertEqual(json.loads(metadata_path.read_text(encoding="utf-8"))["installedBaseVersion"], remote)

    def test_prepared_workspace_cleanup_failure_preserves_committed_state(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "demo"
            expected_dir = root / "prepared"
            skill_dir.mkdir(parents=True)
            expected_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("old\n", encoding="utf-8")
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            workspace = root / "prepared-workspace"
            prepared = replace(
                _prepared_payload(updater, skill_dir, expected_dir, base, observation),
                workspace_root=workspace,
            )

            with mock.patch.object(
                updater,
                "_remove_prepared_workspace",
                side_effect=OSError("cleanup denied"),
            ):
                outcome = updater.apply_observed_update(
                    source,
                    observation,
                    installed_base_version=base,
                    prepared_payload=prepared,
                )

            self.assertEqual(outcome.installed_state, "committed")
            self.assertTrue(outcome.applied)
            self.assertEqual(outcome.cleanup_residue, workspace)
            self.assertIn("cleanup denied", outcome.error_message)

    def test_prepared_payload_rejects_a_different_remote_observation(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "demo"
            expected_dir = root / "prepared"
            skill_dir.mkdir(parents=True)
            expected_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("old\n", encoding="utf-8")
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            prepared_observation = updater.RemoteObservation.from_source(
                source, revision="b" * 40, version="b" * 40
            )
            applied_observation = updater.RemoteObservation.from_source(
                source, revision="c" * 40, version="c" * 40
            )
            prepared = _prepared_payload(
                updater, skill_dir, expected_dir, base, prepared_observation
            )

            with self.assertRaisesRegex(
                updater.AgentSkillUpdaterError,
                "Prepared Payload Remote Observation",
            ):
                updater.apply_observed_update(
                    source,
                    applied_observation,
                    installed_base_version=base,
                    prepared_payload=prepared,
                )

            self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), "old\n")

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
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            materials = root / "materials"
            for name, content in (("base","base\n"),("local","local\n"),("remote","remote\n"),("conflicts","conflict\n")):
                directory = materials / name
                directory.mkdir(parents=True)
                (directory / "SKILL.md").write_text(content, encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = updater.PreparedPayload(None, None, updater.directory_signature(skill_dir), base, remote_observation=observation, base_signature=updater.directory_signature(materials/"base"), remote_signature=updater.directory_signature(materials/"remote"), base_dir=materials/"base", local_dir=materials/"local", remote_dir=materials/"remote", conflict_dir=materials/"conflicts", conflicts=("SKILL.md",))
            interventions_root = root / "interventions"

            with mock.patch.object(updater, "get_interventions_dir", return_value=interventions_root):
                outcome = updater.apply_observed_update(source, observation, installed_base_version=base, prepared_payload=prepared)

            self.assertEqual(outcome.installed_state, "unchanged")
            self.assertFalse(outcome.applied)
            self.assertEqual(outcome.action, "intervention_required")
            record = outcome.intervention_record
            self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), "local\n")
            self.assertEqual({path.name for path in record.iterdir()}, {"manifest.json","base","local","remote","conflicts"})
            manifest = json.loads((record / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["kind"], "content-conflict")
            self.assertEqual(manifest["installedState"], "unchanged")
            self.assertEqual(manifest["exactBaseRevision"], base)
            self.assertEqual(manifest["targetRevision"], remote)
            self.assertEqual(manifest["conflicts"], ["SKILL.md"])
            self.assertEqual(list(interventions_root.glob(".draft-*")), [])

    def test_snapshot_content_conflict_crash_resumes_from_coordinator_journal(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("local\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            materials = root / "materials"
            for name, content in (("base","base\n"),("local","local\n"),("remote","remote\n"),("conflicts","conflict\n")):
                directory = materials / name
                directory.mkdir(parents=True)
                (directory / "SKILL.md").write_text(content, encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = updater.PreparedPayload(None, None, updater.directory_signature(skill_dir), base, remote_observation=observation, base_signature=updater.directory_signature(materials/"base"), remote_signature=updater.directory_signature(materials/"remote"), base_dir=materials/"base", local_dir=materials/"local", remote_dir=materials/"remote", conflict_dir=materials/"conflicts", conflicts=("SKILL.md",), workspace_root=materials)
            interventions_root = root / "interventions"
            real_set_phase = updater._set_coordinator_phase

            def interrupt_before_promotion(transaction_root, state, phase):
                real_set_phase(transaction_root, state, phase)
                if phase == updater.COORDINATOR_PHASE_PUBLISHING_INTERVENTION:
                    raise KeyboardInterrupt()

            with mock.patch.object(updater, "get_interventions_dir", return_value=interventions_root):
                with mock.patch.object(updater, "_set_coordinator_phase", side_effect=interrupt_before_promotion):
                    with self.assertRaises(KeyboardInterrupt):
                        updater.apply_observed_update(source, observation, installed_base_version=base, prepared_payload=prepared)
                transaction = next(skill_dir.parent.glob(".demo.transaction-*"))
                state = json.loads((transaction / "state.json").read_text(encoding="utf-8"))
                self.assertEqual(state["transactionKind"], "snapshot-intervention")
                outcomes = updater.recover_updates(skill_dir.parent)

            self.assertEqual(outcomes[0].installed_state, "unchanged")
            self.assertEqual(outcomes[0].action, "intervention_required")
            self.assertTrue(outcomes[0].intervention_record.is_dir())
            self.assertFalse(transaction.exists())

    def test_snapshot_content_conflict_recovery_rejects_stale_local_evidence(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("local\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = _conflict_payload(
                updater, root / "materials", skill_dir, base, observation, "local\n"
            )
            interventions_root = root / "interventions"
            real_set_phase = updater._set_coordinator_phase

            def interrupt_before_promotion(transaction_root, state, phase):
                real_set_phase(transaction_root, state, phase)
                if phase == updater.COORDINATOR_PHASE_PUBLISHING_INTERVENTION:
                    raise KeyboardInterrupt()

            with mock.patch.object(updater, "get_interventions_dir", return_value=interventions_root):
                with mock.patch.object(updater, "_set_coordinator_phase", side_effect=interrupt_before_promotion):
                    with self.assertRaises(KeyboardInterrupt):
                        updater.apply_observed_update(source, observation, installed_base_version=base, prepared_payload=prepared)
                transaction = next(skill_dir.parent.glob(".demo.transaction-*"))
                (skill_dir / "SKILL.md").write_text("concurrent\n", encoding="utf-8")
                outcomes = updater.recover_updates(skill_dir.parent)

            self.assertEqual(outcomes[0].installed_state, "unchanged")
            self.assertEqual(outcomes[0].action, "none")
            self.assertIn("Concurrent Change", outcomes[0].error_message)
            self.assertEqual(list(interventions_root.iterdir()), [])
            self.assertFalse(transaction.exists())

    def test_content_conflict_artifact_identity_binds_all_materials(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "demo"
            skill_dir.mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text("local one\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            interventions_root = root / "interventions"

            with mock.patch.object(updater, "get_interventions_dir", return_value=interventions_root):
                first = updater.apply_observed_update(
                    source,
                    observation,
                    installed_base_version=base,
                    prepared_payload=_conflict_payload(
                        updater, root / "materials-one", skill_dir, base, observation, "local one\n"
                    ),
                )
                skill_file.write_text("local two\n", encoding="utf-8")
                second = updater.apply_observed_update(
                    source,
                    observation,
                    installed_base_version=base,
                    prepared_payload=_conflict_payload(
                        updater, root / "materials-two", skill_dir, base, observation, "local two\n"
                    ),
                )

            self.assertNotEqual(first.intervention_record, second.intervention_record)
            self.assertEqual(
                (second.intervention_record / "local" / "SKILL.md").read_text(encoding="utf-8"),
                "local two\n",
            )

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
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            expected_dir = Path(temp_dir) / "prepared"
            expected_dir.mkdir()
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = _prepared_payload(updater, skill_dir, expected_dir, base, observation)
            skill_file.write_text("concurrent\n", encoding="utf-8")

            outcome = updater.apply_observed_update(source, observation, installed_base_version=base, prepared_payload=prepared)

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
            original_metadata = json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}).encode("utf-8")
            metadata_path.write_bytes(original_metadata)
            expected_dir = Path(temp_dir) / "prepared"
            expected_dir.mkdir()
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = _prepared_payload(updater, skill_dir, expected_dir, base, observation)
            real_link = updater.os.link

            def fail_metadata_publication(source_path, destination_path):
                if Path(source_path).name == "metadata.publish":
                    raise OSError("injected metadata publication failure")
                return real_link(source_path, destination_path)

            with mock.patch.object(updater.os, "link", side_effect=fail_metadata_publication):
                outcome = updater.apply_observed_update(source, observation, installed_base_version=base, prepared_payload=prepared)

            self.assertEqual(outcome.installed_state, "rolled_back")
            self.assertFalse(outcome.applied)
            self.assertIn("injected metadata publication failure", outcome.error_message)
            self.assertEqual(skill_file.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(metadata_path.read_bytes(), original_metadata)
            self.assertEqual(list(skills_root.glob(".demo.transaction-*")), [])

    def test_snapshot_partial_permission_failure_restores_exact_payload(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "demo"
            expected_dir = root / "prepared"
            skill_dir.mkdir(parents=True)
            expected_dir.mkdir()
            for name, content in (("SKILL.md","old\n"),("a.txt","old-a\n"),("b.txt","old-b\n")):
                (skill_dir / name).write_text(content, encoding="utf-8")
            for name, content in (("SKILL.md","new\n"),("a.txt","new-a\n"),("b.txt","new-b\n")):
                (expected_dir / name).write_text(content, encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = _prepared_payload(updater, skill_dir, expected_dir, base, observation)
            original_signature = updater.directory_signature(skill_dir)
            real_copy = updater._copy_payload_file_exclusive
            injected = False

            def fail_second_incoming(source_path, destination_path):
                nonlocal injected
                if Path(source_path).parent.name == "incoming" and Path(source_path).name == "b.txt" and not injected:
                    injected = True
                    raise PermissionError("injected partial apply failure")
                return real_copy(Path(source_path), Path(destination_path))

            with mock.patch.object(updater, "_copy_payload_file_exclusive", side_effect=fail_second_incoming):
                outcome = updater.apply_observed_update(source, observation, installed_base_version=base, prepared_payload=prepared)

            self.assertTrue(injected)
            self.assertEqual(outcome.installed_state, "rolled_back")
            self.assertEqual(updater.directory_signature(skill_dir), original_signature)

    def test_snapshot_same_path_concurrent_file_is_preserved(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "demo"
            expected_dir = root / "prepared"
            skill_dir.mkdir(parents=True)
            expected_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("old\n", encoding="utf-8")
            (skill_dir / "target.txt").write_text("old target\n", encoding="utf-8")
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            (expected_dir / "target.txt").write_text("remote target\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = _prepared_payload(updater, skill_dir, expected_dir, base, observation)
            real_set_phase = updater._set_coordinator_phase

            def inject_same_path_file(transaction_root, state, phase):
                real_set_phase(transaction_root, state, phase)
                if phase == updater.COORDINATOR_PHASE_INSTALLING_PAYLOAD:
                    (skill_dir / "target.txt").write_text("concurrent unique data\n", encoding="utf-8")

            with mock.patch.object(updater, "_set_coordinator_phase", side_effect=inject_same_path_file):
                outcome = updater.apply_observed_update(source, observation, installed_base_version=base, prepared_payload=prepared)

            self.assertEqual(outcome.installed_state, "uncertain")
            self.assertEqual((skill_dir / "target.txt").read_text(encoding="utf-8"), "old target\n")
            recovery_files = list(skill_dir.parent.glob(".recovery-demo.transaction-*/target.txt"))
            self.assertEqual(len(recovery_files), 1)
            self.assertEqual(recovery_files[0].read_text(encoding="utf-8"), "concurrent unique data\n")

    def test_snapshot_file_to_directory_transition_rolls_back_exactly(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "demo"
            expected_dir = root / "prepared"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("old\n", encoding="utf-8")
            (skill_dir / "node").write_text("old file\n", encoding="utf-8")
            (expected_dir / "node").mkdir(parents=True)
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            (expected_dir / "node" / "child.txt").write_text("new child\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = _prepared_payload(updater, skill_dir, expected_dir, base, observation)

            with mock.patch.object(
                updater,
                "_commit_transaction_metadata",
                side_effect=PermissionError("injected metadata failure"),
            ):
                outcome = updater.apply_observed_update(source, observation, installed_base_version=base, prepared_payload=prepared)

            self.assertEqual(outcome.installed_state, "rolled_back")
            self.assertTrue((skill_dir / "node").is_file())
            self.assertEqual((skill_dir / "node").read_text(encoding="utf-8"), "old file\n")

    def test_snapshot_success_preserves_empty_payload_directory(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "demo"
            expected_dir = root / "prepared"
            skill_dir.mkdir(parents=True)
            expected_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("old\n", encoding="utf-8")
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            (expected_dir / "empty").mkdir()
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = _prepared_payload(updater, skill_dir, expected_dir, base, observation)

            outcome = updater.apply_observed_update(source, observation, installed_base_version=base, prepared_payload=prepared)

            self.assertEqual(outcome.installed_state, "committed")
            self.assertTrue((skill_dir / "empty").is_dir())
            self.assertEqual(list((skill_dir / "empty").iterdir()), [])

    def test_snapshot_git_control_appearing_at_commit_forces_rollback(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "demo"
            expected_dir = root / "prepared"
            skill_dir.mkdir(parents=True)
            expected_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("old\n", encoding="utf-8")
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = _prepared_payload(updater, skill_dir, expected_dir, base, observation)
            real_commit_metadata = updater._commit_transaction_metadata

            def add_git_control_after_metadata(*args, **kwargs):
                result = real_commit_metadata(*args, **kwargs)
                (skill_dir / ".git").mkdir()
                return result

            with mock.patch.object(updater, "_commit_transaction_metadata", side_effect=add_git_control_after_metadata):
                outcome = updater.apply_observed_update(source, observation, installed_base_version=base, prepared_payload=prepared)

            self.assertEqual(outcome.installed_state, "rolled_back")
            self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), "old\n")

    def test_snapshot_metadata_compare_and_swap_preserves_concurrent_write(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "demo"
            expected_dir = root / "prepared"
            skill_dir.mkdir(parents=True)
            expected_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("old\n", encoding="utf-8")
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = _prepared_payload(updater, skill_dir, expected_dir, base, observation)
            concurrent_metadata = json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base,"updatePolicy":"local-only","concurrent":True}).encode("utf-8")
            real_set_phase = updater._set_coordinator_phase
            injected = False

            def inject_concurrent_metadata(transaction_root, state, phase):
                nonlocal injected
                real_set_phase(transaction_root, state, phase)
                if phase == updater.COORDINATOR_PHASE_PUBLISHING_METADATA and not injected:
                    metadata_path.write_bytes(concurrent_metadata)
                    injected = True

            with mock.patch.object(updater, "_set_coordinator_phase", side_effect=inject_concurrent_metadata):
                outcome = updater.apply_observed_update(source, observation, installed_base_version=base, prepared_payload=prepared)

            self.assertTrue(injected)
            self.assertEqual(outcome.installed_state, "uncertain")
            self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), "old\n")
            self.assertEqual(metadata_path.read_bytes(), concurrent_metadata)

    def test_snapshot_crash_recovery_uses_coordinator_snapshot_evidence(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("old\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            expected_dir = Path(temp_dir) / "prepared"
            expected_dir.mkdir()
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = _prepared_payload(updater, skill_dir, expected_dir, base, observation)
            real_set_phase = updater._set_coordinator_phase

            def interrupt_before_install(transaction_root, state, phase):
                real_set_phase(transaction_root, state, phase)
                if phase == updater.COORDINATOR_PHASE_INSTALLING_PAYLOAD:
                    raise KeyboardInterrupt()

            with mock.patch.object(updater, "_set_coordinator_phase", side_effect=interrupt_before_install):
                with self.assertRaises(KeyboardInterrupt):
                    updater.apply_observed_update(source, observation, installed_base_version=base, prepared_payload=prepared)

            transaction = next(skills_root.glob(".demo.transaction-*"))
            state_path = transaction / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["transactionType"], "coordinator")
            self.assertEqual(state["transactionKind"], "snapshot")
            self.assertNotIn("metadataPhase", state)
            self.assertEqual(set(state["evidence"]), {"beforeMetadata", "expectedMetadata", "snapshot"})

            outcome = updater.recover_updates(skills_root)[0]

            self.assertEqual(outcome.installed_state, "rolled_back")
            self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), "old\n")
            self.assertFalse(transaction.exists())

    def test_snapshot_crash_recovery_retains_late_restore_collision(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "skills" / "demo"
            expected_dir = root / "prepared"
            skill_dir.mkdir(parents=True)
            expected_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("old\n", encoding="utf-8")
            (skill_dir / "target.txt").write_text("old target\n", encoding="utf-8")
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            (expected_dir / "target.txt").write_text("remote target\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = _prepared_payload(updater, skill_dir, expected_dir, base, observation)

            with mock.patch.object(updater, "_commit_transaction_metadata", side_effect=KeyboardInterrupt()):
                with self.assertRaises(KeyboardInterrupt):
                    updater.apply_observed_update(source, observation, installed_base_version=base, prepared_payload=prepared)
            transaction = next(skill_dir.parent.glob(".demo.transaction-*"))
            real_copy = updater._copy_payload_file_exclusive
            injected = False

            def collide_during_restore(source_path, destination_path):
                nonlocal injected
                if Path(source_path).parent.name == "original" and Path(source_path).name == "target.txt" and not injected:
                    Path(destination_path).write_text("late concurrent data\n", encoding="utf-8")
                    injected = True
                return real_copy(Path(source_path), Path(destination_path))

            with mock.patch.object(updater, "_copy_payload_file_exclusive", side_effect=collide_during_restore):
                first_outcome = updater.recover_updates(skill_dir.parent)[0]
            second_outcome = updater.recover_updates(skill_dir.parent)[0]

            self.assertTrue(injected)
            self.assertEqual(first_outcome.installed_state, "uncertain")
            self.assertEqual(second_outcome.installed_state, "uncertain")
            self.assertEqual((skill_dir / "target.txt").read_text(encoding="utf-8"), "old target\n")
            recovery_files = list(skill_dir.parent.glob(".recovery-demo.transaction-*/target.txt"))
            self.assertEqual(len(recovery_files), 1)
            self.assertEqual(recovery_files[0].read_text(encoding="utf-8"), "late concurrent data\n")
            self.assertTrue(transaction.exists())

    def test_snapshot_rollback_uncertainty_references_retained_journal(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_root = root / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("old\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(json.dumps({"source":"example/demo","sourceType":"git","repoUrl":"https://github.com/example/demo","subpath":".","installedBaseVersion":base}), encoding="utf-8")
            expected_dir = root / "prepared"
            expected_dir.mkdir()
            (expected_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            source = updater.load_agent_skill_source(skill_dir)
            observation = updater.RemoteObservation.from_source(source, revision=remote, version=remote)
            prepared = _prepared_payload(updater, skill_dir, expected_dir, base, observation)
            interventions_root = root / "interventions"
            real_set_phase = updater._set_coordinator_phase

            def corrupt_displaced_payload(transaction_root, state, phase):
                real_set_phase(transaction_root, state, phase)
                if phase == updater.COORDINATOR_PHASE_INSTALLING_PAYLOAD:
                    (transaction_root / "displaced" / "SKILL.md").write_text("concurrent\n", encoding="utf-8")
                    raise updater.AgentSkillUpdaterError("injected apply failure")

            with mock.patch.object(updater, "get_interventions_dir", return_value=interventions_root):
                with mock.patch.object(updater, "_set_coordinator_phase", side_effect=corrupt_displaced_payload):
                    outcome = updater.apply_observed_update(source, observation, installed_base_version=base, prepared_payload=prepared)

            self.assertEqual(outcome.installed_state, "uncertain")
            self.assertEqual(outcome.action, "intervention_required")
            self.assertTrue(outcome.diagnostic_journal.is_dir())
            self.assertTrue(outcome.intervention_record.is_dir())
            self.assertEqual({path.name for path in outcome.intervention_record.iterdir()}, {"manifest.json"})
            manifest = json.loads((outcome.intervention_record / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["kind"], "recovery-required")
            self.assertEqual(manifest["diagnosticJournal"], str(outcome.diagnostic_journal))
            self.assertEqual(manifest["installedState"], "uncertain")
            self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), "old\n")

    def test_snapshot_v4_decoder_settles_without_rewriting_legacy_journal(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("new\n", encoding="utf-8")
            original_metadata = json.dumps({"installedBaseVersion": base}).encode("utf-8")
            expected_metadata = json.dumps({"installedBaseVersion": remote}).encode("utf-8")
            (skill_dir / ".openskills.json").write_bytes(original_metadata)
            transaction = skills_root / ".demo.update-legacy"
            for name, content in (("original","old\n"),("incoming","new\n"),("displaced","old\n")):
                directory = transaction / name
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "SKILL.md").write_text(content, encoding="utf-8")
            (transaction / "failed").mkdir()
            (transaction / "metadata.before").write_bytes(original_metadata)
            (transaction / "metadata.expected").write_bytes(expected_metadata)
            (transaction / "metadata.publish").write_bytes(expected_metadata)
            state = {
                "version": 4,
                "transactionType": "snapshot",
                "skillName": "demo",
                "skillDir": str(skill_dir.resolve()),
                "phase": "installing",
                "metadataPhase": "prepared",
                "originalSignature": updater.directory_signature(transaction / "original"),
                "expectedSignature": updater.directory_signature(transaction / "incoming"),
                "originalMetadataPresent": True,
                "originalMetadataSha256": hashlib.sha256(original_metadata).hexdigest(),
                "expectedMetadataPresent": True,
                "expectedMetadataSha256": hashlib.sha256(expected_metadata).hexdigest(),
            }
            state_path = transaction / "state.json"
            state_bytes = json.dumps(state).encode("utf-8")
            state_path.write_bytes(state_bytes)
            (transaction / ".skills-updater-transaction").write_text("1\n", encoding="utf-8")

            real_remove = updater._remove_transaction_tree

            def assert_legacy_state_then_remove(path):
                self.assertEqual(state_path.read_bytes(), state_bytes)
                return real_remove(path)

            with mock.patch.object(updater, "_remove_transaction_tree", side_effect=assert_legacy_state_then_remove):
                outcome = updater.recover_updates(skills_root)[0]

            self.assertEqual(outcome.installed_state, "rolled_back")
            self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), "old\n")
            self.assertFalse(transaction.exists())

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

            outcomes = recover_updates(skills_root)

            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0].status, "error")
            self.assertEqual(outcomes[0].installed_state, "uncertain")
            self.assertEqual(outcomes[0].diagnostic_journal, transaction)
            self.assertTrue(transaction.exists())

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

    def test_json_cli_reports_snapshot_preparation_permission_error(self):
        import scripts.update_agent_skills as cli

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
            "localVersion": base,
            "managed": True,
        }
        registry = {
            "version": 2,
            "generatedAt": "2026-01-01T00:00:00+00:00",
            "skillsRoot": r"C:\skills",
            "entries": {"demo": entry},
        }
        observation = mock.sentinel.remote_observation
        resolved = mock.Mock(
            status="update_available",
            installed_base_version=base,
            local_version=base,
            remote_version=remote,
            remote_observation=observation,
        )
        output = io.StringIO()

        with mock.patch.object(sys, "argv", ["update_agent_skills.py", "--skill", "demo", "--json"]):
            with mock.patch.object(cli, "sync_registry", side_effect=[registry, registry]):
                with mock.patch.object(cli, "update_registry_entries"):
                    with mock.patch.object(cli, "_probe_entry", return_value=cli.EntryProbe("update_available", base, remote, remote_observation=observation)):
                        with mock.patch.object(cli, "resolve_skill_update", return_value=resolved):
                            with mock.patch.object(cli, "prepare_snapshot_payload", side_effect=PermissionError("access denied")):
                                with self.assertRaises(SystemExit) as exit_info:
                                    with redirect_stdout(output):
                                        cli.main()

        self.assertEqual(exit_info.exception.code, 1)
        item = json.loads(output.getvalue())[0]
        self.assertEqual(item["installed_state"], "unchanged")
        self.assertFalse(item["applied"])
        self.assertIn("access denied", item["error_message"])

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
