import io
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def run_git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def configure_git(repo: Path) -> None:
    run_git(repo, "config", "user.name", "Skills Updater Tests")
    run_git(repo, "config", "user.email", "skills-updater@example.invalid")
    run_git(repo, "config", "core.autocrlf", "false")


def snapshot_metadata(installed_base: str = "a" * 40, **extra) -> dict:
    return {
        "source": "example/demo",
        "sourceType": "git",
        "repoUrl": "https://github.com/example/demo",
        "subpath": ".",
        "installedBaseVersion": installed_base,
        **extra,
    }


class PayloadIntegrityTests(unittest.TestCase):
    def test_signature_records_cannot_collide_through_newline_paths(self):
        import scripts.agent_skill_updater as updater

        left = hashlib.sha256()
        updater._update_signature_record(left, b"directory", "x\ndirectory y")
        right = hashlib.sha256()
        updater._update_signature_record(right, b"directory", "x")
        updater._update_signature_record(right, b"directory", "y")

        self.assertNotEqual(left.hexdigest(), right.hexdigest())

    def test_apply_contract_change_leaves_payload_and_backup_untouched(self):
        import scripts.agent_skill_updater as updater

        mutations = (
            snapshot_metadata("c" * 40),
            snapshot_metadata(updatePolicy="local-only"),
            snapshot_metadata(
                source="other/repo",
                repoUrl="https://github.com/other/repo",
            ),
        )
        for concurrent_metadata in mutations:
            with self.subTest(concurrent_metadata=concurrent_metadata):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    local = root / "skills" / "demo"
                    base = root / "base"
                    remote = root / "remote"
                    backup_root = root / "backup"
                    for directory in (local, base, remote, backup_root):
                        directory.mkdir(parents=True)
                    (local / "SKILL.md").write_text("old\n", encoding="utf-8")
                    (base / "SKILL.md").write_text("old\n", encoding="utf-8")
                    (remote / "SKILL.md").write_text("new\n", encoding="utf-8")
                    metadata_path = local / ".openskills.json"
                    metadata_path.write_text(json.dumps(snapshot_metadata()), encoding="utf-8")
                    source = updater.AgentSkillSource(
                        "demo",
                        local,
                        "example/demo",
                        "git",
                        "https://github.com/example/demo",
                        ".",
                        None,
                        None,
                        metadata_path,
                        entry_type="single-skill",
                    )
                    update = updater.AgentSkillUpdate(
                        source,
                        remote,
                        "update_available",
                        "a" * 40,
                        "a" * 40,
                        "b" * 40,
                    )

                    class ContractChangingLock:
                        def __enter__(self):
                            metadata_path.write_text(
                                json.dumps(concurrent_metadata),
                                encoding="utf-8",
                            )

                        def __exit__(self, *_args):
                            return False

                    with mock.patch(
                        "scripts.agent_skill_updater._stage_git_skill_at_ref",
                        return_value=base,
                    ):
                        with mock.patch(
                            "scripts.agent_skill_updater.skill_update_lock",
                            return_value=ContractChangingLock(),
                        ):
                            with self.assertRaises(updater.AgentSkillUpdaterError):
                                updater.update_skill_from_staged(update, backup_root)

                    self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "old\n")
                    self.assertEqual(
                        json.loads(metadata_path.read_text(encoding="utf-8")),
                        concurrent_metadata,
                    )
                    self.assertEqual(list(backup_root.iterdir()), [])

    def test_existing_backup_is_never_overwritten(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "skills" / "demo"
            remote = root / "remote"
            backup_root = root / "backup"
            existing = backup_root / "demo"
            for directory in (local, remote, existing):
                directory.mkdir(parents=True)
            (local / "SKILL.md").write_text("old\n", encoding="utf-8")
            (remote / "SKILL.md").write_text("new\n", encoding="utf-8")
            sentinel = existing / "sentinel.txt"
            sentinel.write_text("keep\n", encoding="utf-8")
            metadata_path = local / ".openskills.json"
            metadata_path.write_text(json.dumps(snapshot_metadata()), encoding="utf-8")
            source = updater.AgentSkillSource(
                "demo",
                local,
                "example/demo",
                "git",
                "https://github.com/example/demo",
                ".",
                None,
                None,
                metadata_path,
                entry_type="single-skill",
            )
            update = updater.AgentSkillUpdate(
                source,
                remote,
                "update_available",
                "a" * 40,
                "a" * 40,
                "b" * 40,
            )

            with mock.patch(
                "scripts.agent_skill_updater._stage_git_skill_at_ref",
                side_effect=AssertionError("staging should not start"),
            ):
                with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "overwrite"):
                    updater.update_skill_from_staged(update, backup_root)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_backup_is_published_before_committed_phase_and_crash_recovery(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "skills" / "demo"
            base = root / "base"
            remote = root / "remote"
            backup_root = root / "backup"
            for directory in (local, base, remote, backup_root):
                directory.mkdir(parents=True)
            (local / "SKILL.md").write_text("old\n", encoding="utf-8")
            (base / "SKILL.md").write_text("old\n", encoding="utf-8")
            (remote / "SKILL.md").write_text("new\n", encoding="utf-8")
            metadata_path = local / ".openskills.json"
            metadata_path.write_text(json.dumps(snapshot_metadata()), encoding="utf-8")
            source = updater.AgentSkillSource(
                "demo",
                local,
                "example/demo",
                "git",
                "https://github.com/example/demo",
                ".",
                None,
                None,
                metadata_path,
                entry_type="single-skill",
            )
            update = updater.AgentSkillUpdate(
                source,
                remote,
                "update_available",
                "a" * 40,
                "a" * 40,
                "b" * 40,
            )
            real_set_phase = updater._set_transaction_phase

            def interrupt_committed_phase(transaction_root, state, phase):
                if phase == updater.TRANSACTION_PHASE_COMMITTED:
                    self.assertEqual(
                        (backup_root / "demo" / "SKILL.md").read_text(encoding="utf-8"),
                        "old\n",
                    )
                    raise SystemExit("injected crash")
                return real_set_phase(transaction_root, state, phase)

            with mock.patch(
                "scripts.agent_skill_updater._stage_git_skill_at_ref",
                return_value=base,
            ):
                with mock.patch(
                    "scripts.agent_skill_updater._set_transaction_phase",
                    side_effect=interrupt_committed_phase,
                ):
                    with self.assertRaisesRegex(SystemExit, "injected crash"):
                        updater.update_skill_from_staged(update, backup_root)

            transactions = list(local.parent.glob(".demo.update-*"))
            self.assertEqual(len(transactions), 1)
            updater.recover_incomplete_skill_transactions(local.parent)
            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "old\n")
            self.assertEqual(
                json.loads(metadata_path.read_text(encoding="utf-8"))["installedBaseVersion"],
                "a" * 40,
            )
            self.assertTrue((backup_root / "demo" / "SKILL.md").is_file())
            self.assertFalse(transactions[0].exists())

    def test_snapshot_rolls_back_if_git_control_appears_during_metadata_commit(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "skills" / "demo"
            merged = root / "merged"
            for directory in (local, merged):
                directory.mkdir(parents=True)
            (local / "SKILL.md").write_text("old\n", encoding="utf-8")
            (merged / "SKILL.md").write_text("new\n", encoding="utf-8")
            metadata_path = local / ".openskills.json"
            original_metadata = json.dumps(snapshot_metadata()).encode("utf-8")
            metadata_path.write_bytes(original_metadata)
            source = updater.AgentSkillSource(
                "demo",
                local,
                "example/demo",
                "git",
                "https://github.com/example/demo",
                ".",
                None,
                None,
                metadata_path,
                entry_type="single-skill",
            )
            update = updater.AgentSkillUpdate(
                source,
                merged,
                "update_available",
                "a" * 40,
                "a" * 40,
                "b" * 40,
            )
            real_commit = updater._commit_transaction_metadata

            def add_git_control(*args, **kwargs):
                real_commit(*args, **kwargs)
                (local / ".git").mkdir()

            with mock.patch(
                "scripts.agent_skill_updater._commit_transaction_metadata",
                side_effect=add_git_control,
            ):
                with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "Git worktree"):
                    updater._apply_payload_transaction(update, merged)

            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "old\n")
            self.assertEqual(metadata_path.read_bytes(), original_metadata)
            self.assertTrue((local / ".git").is_dir())
            self.assertEqual(list(local.parent.glob(".demo.update-*")), [])

    def test_snapshot_rechecks_commit_state_after_backup_publication(self):
        import scripts.agent_skill_updater as updater

        for mutation in ("git", "local-only", "payload"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    local = root / "skills" / "demo"
                    merged = root / "merged"
                    backup_dir = root / "backup" / "demo"
                    local.mkdir(parents=True)
                    merged.mkdir()
                    backup_dir.parent.mkdir()
                    (local / "SKILL.md").write_text("old\n", encoding="utf-8")
                    (merged / "SKILL.md").write_text("new\n", encoding="utf-8")
                    metadata_path = local / ".openskills.json"
                    original_metadata = json.dumps(snapshot_metadata()).encode("utf-8")
                    metadata_path.write_bytes(original_metadata)
                    source = updater.AgentSkillSource(
                        "demo",
                        local,
                        "example/demo",
                        "git",
                        "https://github.com/example/demo",
                        ".",
                        None,
                        None,
                        metadata_path,
                        entry_type="single-skill",
                    )
                    update = updater.AgentSkillUpdate(
                        source,
                        merged,
                        "update_available",
                        "a" * 40,
                        "a" * 40,
                        "b" * 40,
                    )
                    real_publish = updater._publish_payload_backup

                    def mutate_after_backup(*args):
                        real_publish(*args)
                        if mutation == "git":
                            (local / ".git").mkdir()
                        elif mutation == "local-only":
                            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                            metadata["updatePolicy"] = "local-only"
                            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                        else:
                            (local / "SKILL.md").write_text("concurrent\n", encoding="utf-8")

                    with mock.patch(
                        "scripts.agent_skill_updater._publish_payload_backup",
                        side_effect=mutate_after_backup,
                    ):
                        with self.assertRaises(updater.AgentSkillUpdaterError):
                            updater._apply_payload_transaction(
                                update,
                                merged,
                                backup_dir=backup_dir,
                            )

                    self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "old\n")
                    self.assertEqual(
                        (backup_dir / "SKILL.md").read_text(encoding="utf-8"),
                        "old\n",
                    )
                    if mutation == "local-only":
                        self.assertEqual(
                            json.loads(metadata_path.read_text(encoding="utf-8"))["updatePolicy"],
                            "local-only",
                        )
                    else:
                        self.assertEqual(metadata_path.read_bytes(), original_metadata)
                    self.assertEqual((local / ".git").is_dir(), mutation == "git")
                    self.assertEqual(list(local.parent.glob(".demo.update-*")), [])

    def test_low_level_staging_rejects_incomplete_source_and_non_commit_ref(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            incomplete = updater.AgentSkillSource(
                "demo",
                root / "demo",
                None,
                "git",
                "https://github.com/example/demo",
                None,
                None,
                None,
                entry_type="single-skill",
            )
            complete = updater.AgentSkillSource(
                "demo",
                root / "demo",
                "example/demo",
                "git",
                "https://github.com/example/demo",
                ".",
                None,
                None,
                entry_type="single-skill",
            )
            mismatched = updater.AgentSkillSource(
                "demo",
                root / "demo",
                "other/repo",
                "git",
                "https://github.com/example/demo",
                ".",
                None,
                None,
                entry_type="single-skill",
            )
            with mock.patch(
                "scripts.agent_skill_updater._download_repo_archive",
                side_effect=AssertionError("network access occurred"),
            ):
                with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "missing remote"):
                    updater.stage_remote_skill(incomplete, root / "stage", "a" * 40)
                with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "exact 12-40"):
                    updater.stage_remote_skill(complete, root / "stage", "../main")
                with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "does not match"):
                    updater.stage_remote_skill(mismatched, root / "stage", "a" * 40)

    def test_git_fetch_uses_explicit_repo_url_and_upstream_is_required(self):
        import scripts.agent_skill_updater as updater

        repo = Path("repo")
        repo_url = "https://github.com/example/demo"
        with mock.patch("scripts.agent_skill_updater._run") as run:
            updater._git_fetch_remote_branch(
                repo,
                repo_url,
                "main",
                "refs/remotes/origin/main",
            )
        command = run.call_args.args[0]
        self.assertEqual(command[-2], repo_url)

        with mock.patch("scripts.agent_skill_updater._git_config_value", return_value=None):
            with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "explicit upstream"):
                updater._git_remote_ref(repo, "main")

    def test_payload_signature_paths_and_copy_ignore_control_entries(self):
        from scripts.agent_skill_updater import (
            _copy_directory_contents,
            _relative_file_paths,
            directory_signature,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "local"
            remote = root / "remote"
            backup = root / "backup"
            for directory in (local, remote):
                directory.mkdir()
                (directory / "SKILL.md").write_text("same\n", encoding="utf-8")
            (local / ".git" / "objects").mkdir(parents=True)
            (local / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (local / ".git" / "objects" / "sentinel").write_bytes(b"git-object")
            (local / ".openskills.json").write_text('{"installedAt":"old"}', encoding="utf-8")
            (remote / ".openskills.json").write_text('{"installedAt":"new"}', encoding="utf-8")

            self.assertEqual(directory_signature(local), directory_signature(remote))
            self.assertEqual(_relative_file_paths(local), {Path("SKILL.md")})

            (local / ".git" / "objects" / "sentinel").write_bytes(b"changed-object")
            self.assertEqual(directory_signature(local), directory_signature(remote))

            _copy_directory_contents(local, backup)
            self.assertTrue((backup / "SKILL.md").exists())
            self.assertFalse((backup / ".git").exists())
            self.assertFalse((backup / ".openskills.json").exists())

    def test_partial_apply_permission_error_restores_exact_payload(self):
        from scripts.agent_skill_updater import (
            AgentSkillSource,
            AgentSkillUpdate,
            AgentSkillUpdaterError,
            _copy_payload_file_exclusive,
            directory_signature,
            update_skill_from_staged,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "skills" / "demo"
            base = root / "base"
            remote = root / "remote"
            backup_root = root / "backup"
            for directory in (local, base, remote):
                directory.mkdir(parents=True)
            backup_root.mkdir()
            for directory in (local, base):
                (directory / "SKILL.md").write_text("base\n", encoding="utf-8")
                (directory / "a.txt").write_text("old-a\n", encoding="utf-8")
                (directory / "b.txt").write_text("old-b\n", encoding="utf-8")
            (remote / "SKILL.md").write_text("base\n", encoding="utf-8")
            (remote / "a.txt").write_text("new-a\n", encoding="utf-8")
            (remote / "b.txt").write_text("new-b\n", encoding="utf-8")
            metadata = {
                "source": "example/demo",
                "sourceType": "git",
                "repoUrl": "https://github.com/example/demo",
                "subpath": ".",
                "installedBaseVersion": "a" * 40,
            }
            metadata_path = local / ".openskills.json"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            original_signature = directory_signature(local)
            original_metadata = metadata_path.read_bytes()

            source = AgentSkillSource(
                name="demo",
                local_dir=local,
                source="example/demo",
                source_type="git",
                repo_url="https://github.com/example/demo",
                subpath=".",
                generator=None,
                workflow_id=None,
                metadata_path=metadata_path,
                entry_type="single-skill",
            )
            update = AgentSkillUpdate(
                source=source,
                staged_dir=remote,
                status="update_available",
                installed_base_version="a" * 40,
                local_version="a" * 40,
                remote_version="b" * 40,
            )

            injected = {"raised": False}

            def fail_after_first_incoming(source_path, destination_path):
                source_path = Path(source_path)
                if (
                    source_path.parent.name == "incoming"
                    and source_path.name == "b.txt"
                    and not injected["raised"]
                ):
                    self.assertEqual(
                        (local / "a.txt").read_text(encoding="utf-8"),
                        "new-a\n",
                    )
                    injected["raised"] = True
                    raise PermissionError("injected partial apply failure")
                return _copy_payload_file_exclusive(source_path, Path(destination_path))

            with mock.patch("scripts.agent_skill_updater._stage_git_skill_at_ref", return_value=base):
                with mock.patch(
                    "scripts.agent_skill_updater._copy_payload_file_exclusive",
                    side_effect=fail_after_first_incoming,
                ):
                    with self.assertRaisesRegex(AgentSkillUpdaterError, "original payload was restored"):
                        update_skill_from_staged(update, backup_root)

            self.assertTrue(injected["raised"])
            self.assertEqual(directory_signature(local), original_signature)
            self.assertEqual(metadata_path.read_bytes(), original_metadata)
            self.assertEqual((local / "a.txt").read_text(encoding="utf-8"), "old-a\n")
            self.assertEqual((local / "b.txt").read_text(encoding="utf-8"), "old-b\n")
            self.assertFalse((backup_root / "demo" / ".openskills.json").exists())
            self.assertEqual(list(local.parent.glob(".demo.update-*")), [])

    def test_metadata_write_failure_rolls_back_payload(self):
        from scripts.agent_skill_updater import (
            AgentSkillSource,
            AgentSkillUpdate,
            AgentSkillUpdaterError,
            directory_signature,
            update_skill_from_staged,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "skills" / "demo"
            base = root / "base"
            remote = root / "remote"
            for directory in (local, base, remote):
                directory.mkdir(parents=True)
            (local / "SKILL.md").write_text("old\n", encoding="utf-8")
            (base / "SKILL.md").write_text("old\n", encoding="utf-8")
            (remote / "SKILL.md").write_text("new\n", encoding="utf-8")
            metadata_path = local / ".openskills.json"
            metadata_path.write_text(
                json.dumps(snapshot_metadata()),
                encoding="utf-8",
            )
            original_signature = directory_signature(local)
            original_metadata = metadata_path.read_bytes()
            backup_root = root / "backup"
            backup_root.mkdir()
            source = AgentSkillSource(
                "demo",
                local,
                "example/demo",
                "git",
                "https://github.com/example/demo",
                ".",
                None,
                None,
                metadata_path,
                entry_type="single-skill",
            )
            update = AgentSkillUpdate(
                source,
                remote,
                "update_available",
                "a" * 40,
                "a" * 40,
                "b" * 40,
            )

            with mock.patch("scripts.agent_skill_updater._stage_git_skill_at_ref", return_value=base):
                with mock.patch(
                    "scripts.agent_skill_updater._commit_transaction_metadata",
                    side_effect=PermissionError("injected metadata failure"),
                ):
                    with self.assertRaisesRegex(AgentSkillUpdaterError, "original payload was restored"):
                        update_skill_from_staged(update, backup_root)

            self.assertEqual(directory_signature(local), original_signature)
            self.assertEqual(metadata_path.read_bytes(), original_metadata)
            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "old\n")
            self.assertEqual(list(local.parent.glob(".demo.update-*")), [])

    def test_concurrent_same_path_file_is_not_overwritten_and_is_preserved(self):
        from scripts.agent_skill_updater import (
            AgentSkillSource,
            AgentSkillUpdate,
            AgentSkillUpdaterError,
            _apply_payload_transaction,
            _copy_payload_file_exclusive,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "skills" / "demo"
            merged = root / "merged"
            local.mkdir(parents=True)
            merged.mkdir()
            (local / "SKILL.md").write_text("old\n", encoding="utf-8")
            (local / "target.txt").write_text("old target\n", encoding="utf-8")
            (merged / "SKILL.md").write_text("new\n", encoding="utf-8")
            (merged / "target.txt").write_text("remote target\n", encoding="utf-8")
            metadata_path = local / ".openskills.json"
            metadata_path.write_text(json.dumps(snapshot_metadata()), encoding="utf-8")
            source = AgentSkillSource(
                "demo", local, "example/demo", "git", "https://github.com/example/demo", ".", None, None, metadata_path,
                entry_type="single-skill",
            )
            update = AgentSkillUpdate(
                source, merged, "update_available", "a" * 40, "a" * 40, "b" * 40
            )
            injected = {"value": False}

            def create_concurrent_file_before_install(source_path, destination_path):
                destination_path = Path(destination_path)
                if Path(source_path).name == "target.txt" and not injected["value"]:
                    destination_path.write_text("concurrent unique user data\n", encoding="utf-8")
                    injected["value"] = True
                return _copy_payload_file_exclusive(Path(source_path), destination_path)

            with mock.patch(
                "scripts.agent_skill_updater._copy_payload_file_exclusive",
                side_effect=create_concurrent_file_before_install,
            ):
                with self.assertRaisesRegex(AgentSkillUpdaterError, "preserved at"):
                    _apply_payload_transaction(update, merged)

            self.assertTrue(injected["value"])
            self.assertEqual((local / "target.txt").read_text(encoding="utf-8"), "old target\n")
            recovery_roots = list(local.parent.glob(".recovery-demo.update-*"))
            self.assertEqual(len(recovery_roots), 1)
            self.assertEqual(
                (recovery_roots[0] / "target.txt").read_text(encoding="utf-8"),
                "concurrent unique user data\n",
            )

    def test_process_interrupt_and_late_restore_collision_remain_recoverable(self):
        from scripts.agent_skill_updater import (
            AgentSkillSource,
            AgentSkillUpdate,
            AgentSkillUpdaterError,
            _apply_payload_transaction,
            _copy_payload_file_exclusive,
            recover_incomplete_skill_transactions,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "skills" / "demo"
            merged = root / "merged"
            local.mkdir(parents=True)
            merged.mkdir()
            (local / "SKILL.md").write_text("old\n", encoding="utf-8")
            (local / "target.txt").write_text("old target\n", encoding="utf-8")
            (merged / "SKILL.md").write_text("new\n", encoding="utf-8")
            (merged / "target.txt").write_text("remote target\n", encoding="utf-8")
            metadata_path = local / ".openskills.json"
            original_metadata = json.dumps(snapshot_metadata()).encode("utf-8")
            metadata_path.write_bytes(original_metadata)
            source = AgentSkillSource(
                "demo", local, "example/demo", "git", "https://github.com/example/demo", ".", None, None, metadata_path,
                entry_type="single-skill",
            )
            update = AgentSkillUpdate(
                source, merged, "update_available", "a" * 40, "a" * 40, "b" * 40
            )

            with mock.patch(
                "scripts.agent_skill_updater._commit_transaction_metadata",
                side_effect=KeyboardInterrupt("simulated process interruption"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    _apply_payload_transaction(update, merged)

            self.assertEqual((local / "target.txt").read_text(encoding="utf-8"), "remote target\n")
            transactions = list(local.parent.glob(".demo.update-*"))
            self.assertEqual(len(transactions), 1)
            injected = {"value": False}

            def create_late_file_during_restore(source_path, destination_path):
                destination_path = Path(destination_path)
                if Path(source_path).name == "target.txt" and not injected["value"]:
                    destination_path.write_text("late concurrent unique data\n", encoding="utf-8")
                    injected["value"] = True
                return _copy_payload_file_exclusive(Path(source_path), destination_path)

            with mock.patch(
                "scripts.agent_skill_updater._copy_payload_file_exclusive",
                side_effect=create_late_file_during_restore,
            ):
                with self.assertRaises(FileExistsError):
                    recover_incomplete_skill_transactions(local.parent)

            self.assertTrue(injected["value"])
            self.assertTrue(transactions[0].exists())
            with self.assertRaisesRegex(AgentSkillUpdaterError, "preserved at"):
                recover_incomplete_skill_transactions(local.parent)

            self.assertEqual((local / "target.txt").read_text(encoding="utf-8"), "old target\n")
            self.assertEqual(metadata_path.read_bytes(), original_metadata)
            recovery_roots = list(local.parent.glob(".recovery-demo.update-*"))
            self.assertEqual(len(recovery_roots), 1)
            self.assertEqual(
                (recovery_roots[0] / "target.txt").read_text(encoding="utf-8"),
                "late concurrent unique data\n",
            )

    def test_file_to_directory_transition_rolls_back_after_metadata_failure(self):
        from scripts.agent_skill_updater import (
            AgentSkillSource,
            AgentSkillUpdate,
            AgentSkillUpdaterError,
            _apply_payload_transaction,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "skills" / "demo"
            merged = root / "merged"
            local.mkdir(parents=True)
            merged.mkdir()
            (local / "SKILL.md").write_text("old\n", encoding="utf-8")
            (local / "node").write_text("old file\n", encoding="utf-8")
            (merged / "SKILL.md").write_text("new\n", encoding="utf-8")
            (merged / "node").mkdir()
            (merged / "node" / "child.txt").write_text("new child\n", encoding="utf-8")
            metadata_path = local / ".openskills.json"
            metadata_path.write_text(json.dumps(snapshot_metadata()), encoding="utf-8")
            source = AgentSkillSource(
                "demo", local, "example/demo", "git", "https://github.com/example/demo", ".", None, None, metadata_path,
                entry_type="single-skill",
            )
            update = AgentSkillUpdate(
                source, merged, "update_available", "a" * 40, "a" * 40, "b" * 40
            )

            def fail_after_payload_is_installed(*_args, **_kwargs):
                self.assertEqual(
                    (local / "node" / "child.txt").read_text(encoding="utf-8"),
                    "new child\n",
                )
                raise PermissionError("injected metadata failure")

            with mock.patch(
                "scripts.agent_skill_updater._commit_transaction_metadata",
                side_effect=fail_after_payload_is_installed,
            ):
                with self.assertRaisesRegex(AgentSkillUpdaterError, "original payload was restored"):
                    _apply_payload_transaction(update, merged)

            self.assertTrue((local / "node").is_file())
            self.assertEqual((local / "node").read_text(encoding="utf-8"), "old file\n")
            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "old\n")

    def test_registry_sync_recovers_interrupted_transaction_before_scanning(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError, directory_signature
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            local = root / "demo"
            transaction = root / ".demo.update-interrupted"
            original = transaction / "original"
            incoming = transaction / "incoming"
            displaced = transaction / "displaced"
            failed = transaction / "failed"
            for directory in (local, original, incoming, displaced, failed):
                directory.mkdir(parents=True)

            (original / "SKILL.md").write_text("old\n", encoding="utf-8")
            (original / "node").write_text("old file\n", encoding="utf-8")
            (incoming / "SKILL.md").write_text("new\n", encoding="utf-8")
            (incoming / "node").mkdir()
            (incoming / "node" / "child.txt").write_text("new child\n", encoding="utf-8")
            (local / "node").mkdir()
            (local / "node" / "child.txt").write_text("partially installed\n", encoding="utf-8")
            (local / "user.txt").write_text("concurrent user data\n", encoding="utf-8")
            (local / "z.txt").write_text("last partial file\n", encoding="utf-8")
            original_metadata = json.dumps(
                {
                    "source": "example/demo",
                    "sourceType": "git",
                    "repoUrl": "https://github.com/example/demo",
                    "subpath": ".",
                    "installedBaseVersion": "a" * 40,
                }
            ).encode("utf-8")
            (transaction / "metadata.before").write_bytes(original_metadata)
            partial_metadata = json.dumps({"installedBaseVersion": "b" * 40}).encode("utf-8")
            (local / ".openskills.json").write_bytes(partial_metadata)
            (transaction / "metadata.expected").write_bytes(partial_metadata)
            (transaction / "metadata.publish").write_bytes(partial_metadata)
            state = {
                "version": 4,
                "transactionType": "snapshot",
                "skillName": "demo",
                "skillDir": str(local.resolve()),
                "phase": "installing",
                "metadataPhase": "published",
                "originalSignature": directory_signature(original),
                "expectedSignature": directory_signature(incoming),
                "originalMetadataPresent": True,
                "originalMetadataSha256": hashlib.sha256(original_metadata).hexdigest(),
                "expectedMetadataPresent": True,
                "expectedMetadataSha256": hashlib.sha256(partial_metadata).hexdigest(),
            }
            (transaction / "state.json").write_text(json.dumps(state), encoding="utf-8")

            real_replace = os.replace
            failed_once = {"value": False}

            def interrupt_first_recovery(source_path, destination_path):
                source_path = Path(source_path)
                if source_path.name == "z.txt" and not failed_once["value"]:
                    self.assertFalse((local / "user.txt").exists())
                    failed_once["value"] = True
                    raise PermissionError("interrupted rollback")
                return real_replace(source_path, destination_path)

            with mock.patch(
                "scripts.agent_skill_updater.os.replace",
                side_effect=interrupt_first_recovery,
            ):
                with self.assertRaisesRegex(PermissionError, "interrupted rollback"):
                    sync_registry(root)

            self.assertTrue(transaction.exists())
            self.assertTrue(failed_once["value"])
            self.assertTrue(any((attempt / "user.txt").is_file() for attempt in failed.iterdir()))
            recovery_root = root / ".recovery-demo.update-interrupted"
            self.assertEqual(
                (recovery_root / "user.txt").read_text(encoding="utf-8"),
                "concurrent user data\n",
            )
            with self.assertRaisesRegex(AgentSkillUpdaterError, "preserved at"):
                sync_registry(root)
            self.assertEqual(
                (recovery_root / "user.txt").read_text(encoding="utf-8"),
                "concurrent user data\n",
            )
            registry = sync_registry(root)

            self.assertIn("demo", registry["entries"])
            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "old\n")
            self.assertTrue((local / "node").is_file())
            self.assertEqual((local / "node").read_text(encoding="utf-8"), "old file\n")
            self.assertEqual((local / ".openskills.json").read_bytes(), original_metadata)
            self.assertFalse(transaction.exists())

    def test_same_skill_update_lock_rejects_a_second_writer(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError, skill_update_lock
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            local = Path(temp_dir) / "skills" / "demo"
            local.mkdir(parents=True)
            (local / "SKILL.md").write_text("demo\n", encoding="utf-8")

            with skill_update_lock(local):
                with self.assertRaisesRegex(AgentSkillUpdaterError, "already being updated"):
                    with skill_update_lock(local):
                        self.fail("a second writer acquired the same skill lock")
                with self.assertRaisesRegex(AgentSkillUpdaterError, "already being updated"):
                    sync_registry(local.parent)

    def test_lock_directory_link_is_rejected_before_lock_file_creation(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            local = Path(temp_dir) / "skills" / "demo"
            lock_root = local.parent / ".skills-updater-locks"
            local.mkdir(parents=True)
            lock_root.mkdir()
            real_is_link = updater._is_filesystem_link

            def mark_lock_root_unsafe(path):
                return Path(path) == lock_root or real_is_link(Path(path))

            with mock.patch(
                "scripts.agent_skill_updater._is_filesystem_link",
                side_effect=mark_lock_root_unsafe,
            ):
                with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "Lock directory is unsafe"):
                    with updater.skill_update_lock(local):
                        self.fail("an unsafe lock directory was accepted")

            self.assertEqual(list(lock_root.iterdir()), [])

    def test_rolled_back_recovery_rejects_recovery_directory_link(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            local = root / "demo"
            transaction = root / ".demo.update-rolled-back"
            recovery_root = root / ".recovery-demo.update-rolled-back"
            local.mkdir(parents=True)
            transaction.mkdir()
            recovery_root.mkdir()
            (local / "SKILL.md").write_text("old\n", encoding="utf-8")
            signature = updater.directory_signature(local)
            expected_metadata = b"{}"
            (transaction / "metadata.expected").write_bytes(expected_metadata)
            (transaction / "metadata.publish").write_bytes(expected_metadata)
            state = {
                "version": 4,
                "transactionType": "snapshot",
                "skillName": "demo",
                "skillDir": str(local.resolve()),
                "phase": "rolled_back",
                "metadataPhase": "prepared",
                "originalSignature": signature,
                "expectedSignature": signature,
                "originalMetadataPresent": False,
                "originalMetadataSha256": None,
                "expectedMetadataPresent": True,
                "expectedMetadataSha256": hashlib.sha256(expected_metadata).hexdigest(),
            }
            (transaction / "state.json").write_text(json.dumps(state), encoding="utf-8")
            real_is_link = updater._is_filesystem_link

            def mark_recovery_root_unsafe(path):
                return Path(path) == recovery_root or real_is_link(Path(path))

            with mock.patch(
                "scripts.agent_skill_updater._is_filesystem_link",
                side_effect=mark_recovery_root_unsafe,
            ):
                with self.assertRaisesRegex(
                    updater.AgentSkillUpdaterError,
                    "Recovery directory is unsafe",
                ):
                    updater.recover_incomplete_skill_transactions(root)

            self.assertTrue(transaction.is_dir())

    def test_payload_recovery_rejects_nested_directory_link(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            local = root / "demo"
            transaction = root / ".demo.update-nested-link"
            quarantine = transaction / "failed" / "attempt-1"
            original = transaction / "original"
            incoming = transaction / "incoming"
            recovery_root = root / ".recovery-demo.update-nested-link"
            unsafe_nested = recovery_root / "nested"
            for directory in (local, quarantine, original, incoming, unsafe_nested):
                directory.mkdir(parents=True)
            for directory in (original, incoming):
                (directory / "SKILL.md").write_text("same\n", encoding="utf-8")
            (quarantine / "nested").mkdir()
            (quarantine / "nested" / "user.txt").write_text("user\n", encoding="utf-8")
            real_is_link = updater._is_filesystem_link

            def mark_nested_unsafe(path):
                return Path(path) == unsafe_nested or real_is_link(Path(path))

            with mock.patch(
                "scripts.agent_skill_updater._is_filesystem_link",
                side_effect=mark_nested_unsafe,
            ):
                with self.assertRaisesRegex(
                    updater.AgentSkillUpdaterError,
                    "Recovery destination is unsafe",
                ):
                    updater._preserve_unexpected_payload(
                        transaction,
                        quarantine,
                        original,
                        incoming,
                        local,
                    )

            self.assertFalse((unsafe_nested / "user.txt").exists())

    def test_transaction_root_link_is_rejected_before_skill_access(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            local = root / "demo"
            transaction = root / ".demo.update-root-link"
            local.mkdir(parents=True)
            transaction.mkdir()
            (local / "SKILL.md").write_text("unchanged\n", encoding="utf-8")
            before = (local / "SKILL.md").read_bytes()
            real_is_link = updater._is_filesystem_link

            def mark_transaction_unsafe(path):
                return Path(path) == transaction or real_is_link(Path(path))

            with mock.patch(
                "scripts.agent_skill_updater._is_filesystem_link",
                side_effect=mark_transaction_unsafe,
            ):
                with self.assertRaisesRegex(
                    updater.AgentSkillUpdaterError,
                    "Transaction root must not be a symlink or junction",
                ):
                    updater.recover_incomplete_skill_transactions(root)

            self.assertEqual((local / "SKILL.md").read_bytes(), before)

    def test_failed_transaction_directory_link_is_rejected_before_payload_move(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            local = root / "demo"
            transaction = root / ".demo.update-failed-link"
            original = transaction / "original"
            incoming = transaction / "incoming"
            failed = transaction / "failed"
            for directory in (local, original, incoming, failed):
                directory.mkdir(parents=True)
            (local / "SKILL.md").write_text("partial\n", encoding="utf-8")
            (original / "SKILL.md").write_text("old\n", encoding="utf-8")
            (incoming / "SKILL.md").write_text("new\n", encoding="utf-8")
            before = updater.directory_signature(local)
            expected_metadata = b"{}"
            (transaction / "metadata.expected").write_bytes(expected_metadata)
            (transaction / "metadata.publish").write_bytes(expected_metadata)
            state = {
                "version": 4,
                "transactionType": "snapshot",
                "skillName": "demo",
                "skillDir": str(local.resolve()),
                "phase": "installing",
                "metadataPhase": "prepared",
                "originalSignature": updater.directory_signature(original),
                "expectedSignature": updater.directory_signature(incoming),
                "originalMetadataPresent": False,
                "originalMetadataSha256": None,
                "expectedMetadataPresent": True,
                "expectedMetadataSha256": hashlib.sha256(expected_metadata).hexdigest(),
            }
            (transaction / "state.json").write_text(json.dumps(state), encoding="utf-8")
            real_is_link = updater._is_filesystem_link

            def mark_failed_unsafe(path):
                return Path(path) == failed or real_is_link(Path(path))

            with mock.patch(
                "scripts.agent_skill_updater._is_filesystem_link",
                side_effect=mark_failed_unsafe,
            ):
                with self.assertRaisesRegex(
                    updater.AgentSkillUpdaterError,
                    "Transaction directory is unsafe",
                ):
                    updater.recover_incomplete_skill_transactions(root)

            self.assertEqual(updater.directory_signature(local), before)

    def test_nested_recovery_link_is_rejected_before_payload_move(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            local = root / "demo"
            transaction = root / ".demo.update-nested-recovery-link"
            original = transaction / "original"
            incoming = transaction / "incoming"
            recovery_root = root / ".recovery-demo.update-nested-recovery-link"
            unsafe_nested = recovery_root / "nested"
            for directory in (local / "nested", original, incoming, unsafe_nested):
                directory.mkdir(parents=True)
            (local / "SKILL.md").write_text("partial\n", encoding="utf-8")
            (local / "nested" / "user.txt").write_text("user\n", encoding="utf-8")
            (original / "SKILL.md").write_text("old\n", encoding="utf-8")
            (incoming / "SKILL.md").write_text("new\n", encoding="utf-8")
            before = updater.directory_signature(local)
            expected_metadata = b"{}"
            (transaction / "metadata.expected").write_bytes(expected_metadata)
            (transaction / "metadata.publish").write_bytes(expected_metadata)
            state = {
                "version": 4,
                "transactionType": "snapshot",
                "skillName": "demo",
                "skillDir": str(local.resolve()),
                "phase": "installing",
                "metadataPhase": "prepared",
                "originalSignature": updater.directory_signature(original),
                "expectedSignature": updater.directory_signature(incoming),
                "originalMetadataPresent": False,
                "originalMetadataSha256": None,
                "expectedMetadataPresent": True,
                "expectedMetadataSha256": hashlib.sha256(expected_metadata).hexdigest(),
            }
            (transaction / "state.json").write_text(json.dumps(state), encoding="utf-8")
            real_is_link = updater._is_filesystem_link

            def mark_nested_unsafe(path):
                return Path(path) == unsafe_nested or real_is_link(Path(path))

            with mock.patch(
                "scripts.agent_skill_updater._is_filesystem_link",
                side_effect=mark_nested_unsafe,
            ):
                with self.assertRaisesRegex(
                    updater.AgentSkillUpdaterError,
                    "Recovery destination is unsafe",
                ):
                    updater.recover_incomplete_skill_transactions(root)

            self.assertEqual(updater.directory_signature(local), before)
            self.assertFalse(any((transaction / "failed").glob("attempt-*")))

    def test_missing_transaction_state_fails_closed_and_preserves_snapshot(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError, recover_incomplete_skill_transactions

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            local = root / "demo"
            transaction = root / ".demo.update-missing-state"
            original = transaction / "original"
            local.mkdir(parents=True)
            original.mkdir(parents=True)
            (local / "partial.txt").write_text("partial\n", encoding="utf-8")
            (original / "SKILL.md").write_text("recoverable\n", encoding="utf-8")
            (transaction / ".skills-updater-transaction").write_text("1\n", encoding="utf-8")

            with self.assertRaisesRegex(AgentSkillUpdaterError, "state is missing"):
                recover_incomplete_skill_transactions(root)

            self.assertTrue(transaction.exists())
            self.assertEqual(
                (original / "SKILL.md").read_text(encoding="utf-8"),
                "recoverable\n",
            )
            self.assertEqual((local / "partial.txt").read_text(encoding="utf-8"), "partial\n")

    def test_snapshot_transaction_requires_expected_metadata_snapshot(self):
        from scripts.agent_skill_updater import (
            AgentSkillUpdaterError,
            directory_signature,
            recover_incomplete_skill_transactions,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            local = root / "demo"
            transaction = root / ".demo.update-invalid-metadata-contract"
            local.mkdir(parents=True)
            transaction.mkdir()
            (local / "SKILL.md").write_text("unchanged\n", encoding="utf-8")
            signature = directory_signature(local)
            state = {
                "version": 4,
                "transactionType": "snapshot",
                "skillName": "demo",
                "skillDir": str(local.resolve()),
                "phase": "rolled_back",
                "metadataPhase": "prepared",
                "originalSignature": signature,
                "expectedSignature": signature,
                "originalMetadataPresent": False,
                "originalMetadataSha256": None,
                "expectedMetadataPresent": False,
                "expectedMetadataSha256": None,
            }
            (transaction / "state.json").write_text(json.dumps(state), encoding="utf-8")

            with self.assertRaisesRegex(AgentSkillUpdaterError, "Invalid update transaction state"):
                recover_incomplete_skill_transactions(root)

            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "unchanged\n")
            self.assertTrue(transaction.is_dir())

    def test_committed_recovery_preserves_safe_concurrent_metadata_before_cleanup(self):
        from scripts.agent_skill_updater import (
            directory_signature,
            recover_incomplete_skill_transactions,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            local = root / "demo"
            transaction = root / ".demo.update-committed"
            local.mkdir(parents=True)
            transaction.mkdir()
            (local / "SKILL.md").write_text("new\n", encoding="utf-8")
            old_metadata = b'{"installedBaseVersion":"aaaaaaaaaaaa"}'
            new_metadata = b'{"installedBaseVersion":"bbbbbbbbbbbb"}'
            (local / ".openskills.json").write_bytes(old_metadata)
            (transaction / "metadata.before").write_bytes(old_metadata)
            (transaction / "metadata.expected").write_bytes(new_metadata)
            (transaction / "metadata.publish").write_bytes(new_metadata)
            state = {
                "version": 4,
                "transactionType": "snapshot",
                "skillName": "demo",
                "skillDir": str(local.resolve()),
                "phase": "committed",
                "metadataPhase": "published",
                "originalSignature": directory_signature(local),
                "expectedSignature": directory_signature(local),
                "originalMetadataPresent": True,
                "originalMetadataSha256": hashlib.sha256(old_metadata).hexdigest(),
                "expectedMetadataPresent": True,
                "expectedMetadataSha256": hashlib.sha256(new_metadata).hexdigest(),
            }
            (transaction / "state.json").write_text(json.dumps(state), encoding="utf-8")

            recover_incomplete_skill_transactions(root)

            self.assertEqual((local / ".openskills.json").read_bytes(), old_metadata)
            self.assertFalse(transaction.exists())

    def test_empty_payload_directory_is_preserved_by_successful_update(self):
        from scripts.agent_skill_updater import (
            AgentSkillSource,
            AgentSkillUpdate,
            update_skill_from_staged,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "skills" / "demo"
            base = root / "base"
            remote = root / "remote"
            for directory in (local, base, remote):
                directory.mkdir(parents=True)
            for directory in (local, base):
                (directory / "SKILL.md").write_text("old\n", encoding="utf-8")
                (directory / "keep-empty").mkdir()
            (remote / "SKILL.md").write_text("new\n", encoding="utf-8")
            (remote / "keep-empty").mkdir()
            metadata_path = local / ".openskills.json"
            metadata_path.write_text(
                json.dumps(
                    snapshot_metadata()
                ),
                encoding="utf-8",
            )
            source = AgentSkillSource(
                "demo", local, "example/demo", "git", "https://github.com/example/demo", ".", None, None, metadata_path,
                entry_type="single-skill",
            )
            update = AgentSkillUpdate(
                source, remote, "update_available", "a" * 40, "a" * 40, "b" * 40
            )
            backup_root = root / "backup"
            backup_root.mkdir()

            with mock.patch("scripts.agent_skill_updater._stage_git_skill_at_ref", return_value=base):
                update_skill_from_staged(update, backup_root)

            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "new\n")
            self.assertTrue((local / "keep-empty").is_dir())

    def test_three_way_merge_supports_file_to_directory_transition(self):
        from scripts.agent_skill_updater import _merge_skill_directories

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = root / "base"
            local = root / "local"
            remote = root / "remote"
            merged = root / "merged"
            for directory in (base, local, remote):
                directory.mkdir()
                (directory / "SKILL.md").write_text("same\n", encoding="utf-8")
            for directory in (base, local):
                (directory / "node").write_text("old file\n", encoding="utf-8")
            (remote / "node").mkdir()
            (remote / "node" / "child.txt").write_text("new child\n", encoding="utf-8")

            _merge_skill_directories(
                base_dir=base,
                local_dir=local,
                remote_dir=remote,
                merged_dir=merged,
                conflict_root=root / "conflicts",
            )

            self.assertTrue((merged / "node").is_dir())
            self.assertEqual(
                (merged / "node" / "child.txt").read_text(encoding="utf-8"),
                "new child\n",
            )

    def test_three_way_merge_applies_remote_empty_directory_deletion(self):
        from scripts.agent_skill_updater import _merge_skill_directories

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = root / "base"
            local = root / "local"
            remote = root / "remote"
            merged = root / "merged"
            for directory in (base, local, remote):
                directory.mkdir()
                (directory / "SKILL.md").write_text("same\n", encoding="utf-8")
            (base / "obsolete").mkdir()
            (local / "obsolete").mkdir()

            _merge_skill_directories(
                base_dir=base,
                local_dir=local,
                remote_dir=remote,
                merged_dir=merged,
                conflict_root=root / "conflicts",
            )

            self.assertFalse((merged / "obsolete").exists())

    def test_json_mode_converts_permission_error_to_structured_error(self):
        import scripts.update_agent_skills as updater

        registry = {
            "version": 2,
            "generatedAt": "2026-01-01T00:00:00+00:00",
            "skillsRoot": r"C:\skills",
            "entries": {
                "demo": {
                    "name": "demo",
                    "entryType": "single-skill",
                    "updateMode": "snapshot",
                    "path": r"C:\skills\demo",
                    "repoUrl": "https://github.com/example/demo",
                    "source": "example/demo",
                    "sourceType": "git",
                    "subpath": ".",
                    "installedBaseVersion": "a" * 40,
                    "localVersion": "a" * 40,
                    "managed": True,
                }
            },
        }
        resolved = SimpleNamespace(
            status="update_available",
            error_message=None,
            installed_base_version="a" * 40,
            local_version="a" * 40,
            remote_version="b" * 40,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(updater.sys, "argv", ["update_agent_skills.py", "--skill", "demo", "--json"]):
            with mock.patch("scripts.update_agent_skills.sync_registry", side_effect=[registry, registry]) as sync:
                with mock.patch("scripts.update_agent_skills.update_registry_entries"):
                    with mock.patch(
                        "scripts.update_agent_skills._probe_entry",
                        return_value=updater.EntryProbe(
                            "update_available",
                            "a" * 40,
                            "b" * 40,
                            remote_observation=mock.sentinel.remote_observation,
                        ),
                    ):
                        with mock.patch("scripts.update_agent_skills.resolve_skill_update", return_value=resolved):
                            with mock.patch(
                                "scripts.update_agent_skills.make_backup_root",
                                return_value=Path(r"C:\backup"),
                            ):
                                with mock.patch(
                                    "scripts.update_agent_skills.update_skill_from_staged",
                                    side_effect=PermissionError("injected access denied"),
                                ):
                                    with self.assertRaises(SystemExit) as exit_info:
                                        with redirect_stdout(stdout), redirect_stderr(stderr):
                                            updater.main()

        self.assertEqual(exit_info.exception.code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["status"], "error")
        self.assertFalse(payload[0]["applied"])
        self.assertIn("injected access denied", payload[0]["error_message"])
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(sync.call_count, 2)

    def test_json_reports_committed_update_when_only_cleanup_failed(self):
        import scripts.update_agent_skills as updater

        version_a = "a" * 40
        version_b = "b" * 40
        entry = {
            "name": "demo",
            "entryType": "single-skill",
            "updateMode": "snapshot",
            "path": r"C:\skills\demo",
            "repoUrl": "https://github.com/example/demo",
            "source": "example/demo",
            "sourceType": "git",
            "subpath": ".",
            "installedBaseVersion": version_a,
            "localVersion": version_a,
            "managed": True,
        }
        registry = {
            "version": 2,
            "generatedAt": "2026-01-01T00:00:00+00:00",
            "skillsRoot": r"C:\skills",
            "entries": {"demo": entry},
        }
        resolved = SimpleNamespace(
            status="update_available",
            error_message=None,
            installed_base_version=version_a,
            local_version=version_a,
            remote_version=version_b,
        )
        committed_error = updater.AgentSkillUpdateCommittedError(
            "payload committed; cleanup failed",
            action="payload_merged",
            version=version_b,
        )
        stdout = io.StringIO()
        with mock.patch.object(
            updater.sys,
            "argv",
            ["update_agent_skills.py", "--skill", "demo", "--json"],
        ):
            with mock.patch("scripts.update_agent_skills.sync_registry", side_effect=[registry, registry]):
                with mock.patch("scripts.update_agent_skills.update_registry_entries"):
                    with mock.patch(
                        "scripts.update_agent_skills._probe_entry",
                        return_value=updater.EntryProbe(
                            "update_available",
                            version_a,
                            version_b,
                            remote_observation=mock.sentinel.remote_observation,
                        ),
                    ):
                        with mock.patch("scripts.update_agent_skills.resolve_skill_update", return_value=resolved):
                            with mock.patch(
                                "scripts.update_agent_skills.make_backup_root",
                                return_value=Path(r"C:\backup"),
                            ):
                                with mock.patch(
                                    "scripts.update_agent_skills.update_skill_from_staged",
                                    side_effect=committed_error,
                                ):
                                    with self.assertRaises(SystemExit) as exit_info:
                                        with redirect_stdout(stdout):
                                            updater.main()

        self.assertEqual(exit_info.exception.code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["status"], "error")
        self.assertTrue(payload[0]["applied"])
        self.assertEqual(payload[0]["action"], "payload_merged")
        self.assertEqual(payload[0]["installed_base_version"], version_b)

    def test_commit_prefix_comparison_is_validated_and_shared_by_probes(self):
        import scripts.check_updates as checker
        import scripts.update_agent_skills as updater
        from scripts.agent_skill_updater import same_git_commit

        full_sha = "0123456789abcdef0123456789abcdef01234567"
        self.assertTrue(same_git_commit(full_sha, full_sha[:12]))
        self.assertTrue(same_git_commit(full_sha[:12], full_sha))
        self.assertFalse(same_git_commit(full_sha, full_sha[:11]))
        self.assertFalse(same_git_commit(full_sha, "not-a-git-sha"))
        self.assertFalse(same_git_commit(full_sha, "f" * 12))

        entry = {
            "name": "demo",
            "entryType": "single-skill",
            "updateMode": "snapshot",
            "path": r"C:\skills\demo",
            "repoUrl": "https://github.com/example/demo",
            "source": "example/demo",
            "sourceType": "git",
            "installedBaseVersion": full_sha[:12],
            "localVersion": full_sha[:12],
            "managed": True,
        }
        with mock.patch("scripts.check_updates.fetch_source_remote_version", return_value=full_sha):
            checked = checker._entry_to_skill_info(entry)
        with mock.patch(
            "scripts.update_agent_skills.fetch_source_remote_observation",
            return_value=SimpleNamespace(version=full_sha),
        ):
            probed = updater._probe_entry(entry)

        self.assertEqual(checked.status, checker.UpdateStatus.UP_TO_DATE)
        self.assertEqual(probed.status, "up_to_date")

    def test_registry_update_mode_typos_and_pack_snapshot_mode_fail_closed(self):
        from scripts.agent_skill_updater import (
            AgentSkillUpdaterError,
            registry_entry_uses_git_worktree,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            skill = Path(temp_dir) / "demo"
            skill.mkdir()
            (skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            base_entry = {
                "name": "demo",
                "path": str(skill),
                "source": "example/demo",
                "sourceType": "git",
                "repoUrl": "https://github.com/example/demo",
                "subpath": ".",
                "entryType": "single-skill",
            }

            with self.assertRaisesRegex(AgentSkillUpdaterError, "unsupported updateMode"):
                registry_entry_uses_git_worktree({**base_entry, "updateMode": "snapshop"})
            with self.assertRaisesRegex(AgentSkillUpdaterError, "Skill pack"):
                registry_entry_uses_git_worktree(
                    {**base_entry, "entryType": "skill-pack", "updateMode": "snapshot"}
                )

    def test_strict_github_url_rejects_extra_path_and_query_without_api_request(self):
        import scripts.agent_skill_updater as updater

        failed_git = SimpleNamespace(returncode=1, stdout="", stderr="not found")
        invalid_urls = (
            "https://github.com/example/demo/extra",
            "https://github.com/example/demo?ref=other",
        )
        for repo_url in invalid_urls:
            with self.subTest(repo_url=repo_url):
                with mock.patch.object(updater.subprocess, "run", return_value=failed_git):
                    with mock.patch.object(updater.urllib.request, "urlopen") as urlopen:
                        with self.assertRaises(updater.AgentSkillUpdaterError):
                            updater._fetch_remote_commit_sha(repo_url)
                urlopen.assert_not_called()

        with mock.patch.object(updater.subprocess, "run", return_value=failed_git):
            with mock.patch.object(updater.urllib.request, "urlopen") as urlopen:
                with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "git ls-remote exited"):
                    updater._fetch_remote_commit_sha("https://github.com/example/demo")
        urlopen.assert_not_called()

    def test_metadata_reader_rejects_linked_control_file(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            metadata_path = Path(temp_dir) / ".openskills.json"
            metadata_path.write_text("{}", encoding="utf-8")
            with mock.patch("scripts.agent_skill_updater._is_filesystem_link", return_value=True):
                with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "regular file"):
                    updater._read_json_object(metadata_path, "Skill metadata")

    def test_transaction_metadata_verification_rejects_links_and_non_absent_paths(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "demo"
            skill_dir.mkdir()
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_bytes(b"{}")
            state = {
                "expectedMetadataPresent": True,
                "expectedMetadataSha256": hashlib.sha256(b"{}").hexdigest(),
                "originalMetadataPresent": False,
                "originalMetadataSha256": None,
            }
            with mock.patch("scripts.agent_skill_updater._is_filesystem_link", return_value=True):
                with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "verification failed"):
                    updater._validate_transaction_metadata(skill_dir, state, expected=True)

            metadata_path.unlink()
            metadata_path.mkdir()
            with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "verification failed"):
                updater._validate_transaction_metadata(skill_dir, state, expected=False)

    def test_snapshot_prefix_match_refreshes_to_full_sha_without_staging(self):
        import scripts.update_agent_skills as updater

        full_sha = "0123456789abcdef0123456789abcdef01234567"
        entry = {
            "name": "demo",
            "entryType": "single-skill",
            "updateMode": "snapshot",
            "path": r"C:\skills\demo",
            "repoUrl": "https://github.com/example/demo",
            "source": "example/demo",
            "sourceType": "git",
            "subpath": ".",
            "installedBaseVersion": full_sha[:12],
            "localVersion": full_sha[:12],
            "managed": True,
        }
        registry = {
            "version": 2,
            "generatedAt": "2026-01-01T00:00:00+00:00",
            "skillsRoot": r"C:\skills",
            "entries": {"demo": entry},
        }
        stdout = io.StringIO()
        with mock.patch.object(
            updater.sys,
            "argv",
            ["update_agent_skills.py", "--skill", "demo", "--json"],
        ):
            with mock.patch("scripts.update_agent_skills.sync_registry", side_effect=[registry, registry]):
                with mock.patch("scripts.update_agent_skills.update_registry_entries"):
                    with mock.patch(
                        "scripts.update_agent_skills._probe_entry",
                        return_value=updater.EntryProbe(
                            "up_to_date",
                            full_sha[:12],
                            full_sha,
                            remote_observation=mock.sentinel.remote_observation,
                        ),
                    ):
                        with mock.patch(
                            "scripts.update_agent_skills.apply_observed_update",
                            return_value=SimpleNamespace(
                                status="up_to_date",
                                installed_state="committed",
                                applied=True,
                                action="metadata_refreshed",
                                version=full_sha,
                                error_message=None,
                                diagnostic_journal=None,
                                cleanup_residue=None,
                            ),
                        ) as apply_observed:
                            with mock.patch("scripts.update_agent_skills.resolve_skill_update") as resolve:
                                with self.assertRaises(SystemExit) as exit_info:
                                    with redirect_stdout(stdout):
                                        updater.main()

        self.assertEqual(exit_info.exception.code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["action"], "metadata_refreshed")
        self.assertEqual(payload[0]["installed_base_version"], full_sha)
        self.assertEqual(payload[0]["local_version"], full_sha)
        self.assertEqual(payload[0]["installed_state"], "committed")
        apply_observed.assert_called_once()
        resolve.assert_not_called()

    def test_snapshot_metadata_refresh_preserves_concurrent_local_only_write(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "demo"
            skill_dir.mkdir()
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
            concurrent = b'{"updatePolicy":"local-only","concurrent":true}'
            real_set_phase = updater._set_coordinator_phase
            injected = {"value": False}

            def inject_concurrent_write(transaction_root, state, phase):
                real_set_phase(transaction_root, state, phase)
                if phase == updater.COORDINATOR_PHASE_CAPTURING_METADATA and not injected["value"]:
                    metadata_path.write_bytes(concurrent)
                    injected["value"] = True

            with mock.patch(
                "scripts.agent_skill_updater._set_coordinator_phase",
                side_effect=inject_concurrent_write,
            ):
                with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "Concurrent write"):
                    updater.refresh_skill_metadata_version(source, base, remote)

            self.assertTrue(injected["value"])
            self.assertEqual(metadata_path.read_bytes(), concurrent)

    def test_metadata_transaction_recovers_crash_between_capture_and_publish(self):
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
            expected = json.dumps(
                {
                    "source": "example/demo",
                    "sourceType": "git",
                    "repoUrl": "https://github.com/example/demo",
                    "subpath": ".",
                    "installedBaseVersion": remote,
                }
            ).encode("utf-8")
            transaction = skills_root / ".demo.metadata-update-crash"
            transaction.mkdir()
            (transaction / "metadata.before").write_bytes(original)
            (transaction / "metadata.expected").write_bytes(expected)
            (transaction / "metadata.publish").write_bytes(expected)
            (transaction / "metadata.displaced").write_bytes(original)
            state = {
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
            (transaction / "state.json").write_text(
                json.dumps(state),
                encoding="utf-8",
            )
            (transaction / ".skills-updater-transaction").write_text("1\n", encoding="utf-8")

            self.assertFalse(metadata_path.exists())
            updater.recover_incomplete_skill_transactions(skills_root)

            self.assertEqual(metadata_path.read_bytes(), original)
            self.assertFalse(transaction.exists())

    def test_metadata_refresh_cleanup_error_reports_committed_state(self):
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

            with mock.patch(
                "scripts.agent_skill_updater._remove_transaction_tree",
                side_effect=updater.AgentSkillUpdaterError("cleanup interrupted"),
            ):
                with self.assertRaises(updater.AgentSkillUpdateCommittedError) as error:
                    updater.refresh_skill_metadata_version(source, base, remote)

            self.assertEqual(error.exception.action, "metadata_refreshed")
            self.assertEqual(error.exception.version, remote)
            self.assertEqual(
                json.loads(metadata_path.read_text(encoding="utf-8"))["installedBaseVersion"],
                remote,
            )
            transactions = list(skills_root.glob(".demo.transaction-*"))
            self.assertEqual(len(transactions), 1)
            updater.recover_incomplete_skill_transactions(skills_root)
            self.assertFalse(transactions[0].exists())

    def test_snapshot_cleanup_agent_error_reports_committed_payload_state(self):
        import scripts.agent_skill_updater as updater

        base = "a" * 40
        remote = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_dir = skills_root / "demo"
            merged = Path(temp_dir) / "merged"
            skill_dir.mkdir(parents=True)
            merged.mkdir()
            (skill_dir / "SKILL.md").write_text("old\n", encoding="utf-8")
            (merged / "SKILL.md").write_text("new\n", encoding="utf-8")
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
            update = updater.AgentSkillUpdate(
                source,
                merged,
                "update_available",
                base,
                base,
                remote,
            )

            with mock.patch(
                "scripts.agent_skill_updater._remove_transaction_tree",
                side_effect=updater.AgentSkillUpdaterError("cleanup interrupted"),
            ):
                with self.assertRaises(updater.AgentSkillUpdateCommittedError) as error:
                    updater._apply_payload_transaction(update, merged)

            self.assertEqual(error.exception.action, "payload_merged")
            self.assertEqual(error.exception.version, remote)
            self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), "new\n")
            self.assertEqual(
                json.loads(metadata_path.read_text(encoding="utf-8"))["installedBaseVersion"],
                remote,
            )
            transactions = list(skills_root.glob(".demo.update-*"))
            self.assertEqual(len(transactions), 1)
            updater.recover_incomplete_skill_transactions(skills_root)
            self.assertFalse(transactions[0].exists())

    def test_metadata_publish_link_failure_restores_original_and_cleans_journal(self):
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
            real_link = os.link
            real_set_phase = updater._set_coordinator_phase
            injected = {"value": False}
            phases = []

            def fail_first_publish_link(source_path, destination_path, *args, **kwargs):
                if (
                    Path(source_path).name == "metadata.publish"
                    and Path(destination_path) == metadata_path
                    and not injected["value"]
                ):
                    injected["value"] = True
                    raise PermissionError("injected publish failure")
                return real_link(source_path, destination_path, *args, **kwargs)

            def record_phase(transaction_root, state, phase):
                phases.append(phase)
                return real_set_phase(transaction_root, state, phase)

            with mock.patch(
                "scripts.agent_skill_updater.os.link",
                side_effect=fail_first_publish_link,
            ):
                with mock.patch(
                    "scripts.agent_skill_updater._set_coordinator_phase",
                    side_effect=record_phase,
                ):
                    with self.assertRaisesRegex(
                        updater.AgentSkillUpdaterError,
                        "injected publish failure",
                    ):
                        updater.refresh_skill_metadata_version(source, base, remote)

            self.assertTrue(injected["value"])
            self.assertIn(updater.COORDINATOR_PHASE_METADATA_PUBLISH_FAILED, phases)
            self.assertEqual(metadata_path.read_bytes(), original)
            self.assertEqual(list(skills_root.glob(".demo.transaction-*")), [])

    def test_metadata_rollback_read_failure_is_recovered_from_existing_capture(self):
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
            real_validate = updater._validate_transaction_metadata
            real_read_bytes = Path.read_bytes
            injected = {"value": False}

            def fail_after_publish(skill_path, state, *, expected):
                if expected:
                    raise PermissionError("injected post-publish failure")
                return real_validate(skill_path, state, expected=expected)

            def interrupt_first_rollback_capture_read(path):
                if path.name.startswith("metadata.rollback-") and not injected["value"]:
                    injected["value"] = True
                    raise PermissionError("injected rollback capture read failure")
                return real_read_bytes(path)

            with mock.patch(
                "scripts.agent_skill_updater._validate_transaction_metadata",
                side_effect=fail_after_publish,
            ):
                with mock.patch.object(
                    Path,
                    "read_bytes",
                    autospec=True,
                    side_effect=interrupt_first_rollback_capture_read,
                ):
                    with self.assertRaisesRegex(
                        updater.AgentSkillUpdaterError,
                        "Rollback also failed",
                    ):
                        updater.refresh_skill_metadata_version(source, base, remote)

            self.assertTrue(injected["value"])
            self.assertFalse(metadata_path.exists())
            transactions = list(skills_root.glob(".demo.transaction-*"))
            self.assertEqual(len(transactions), 1)
            self.assertEqual(len(list(transactions[0].glob("metadata.rollback-*"))), 1)

            updater.recover_incomplete_skill_transactions(skills_root)

            self.assertEqual(metadata_path.read_bytes(), original)
            self.assertFalse(transactions[0].exists())

    def test_invalid_equal_git_metadata_is_rejected(self):
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            local = root / "demo"
            local.mkdir(parents=True)
            (local / "SKILL.md").write_text("demo\n", encoding="utf-8")
            (local / ".openskills.json").write_text(
                json.dumps(
                    {
                        "source": "example/demo",
                        "sourceType": "git",
                        "repoUrl": "https://github.com/example/demo",
                        "subpath": ".",
                        "installedBaseVersion": "not-a-git-sha",
                    }
                ),
                encoding="utf-8",
            )

            entry = sync_registry(root)["entries"]["demo"]

            self.assertIn("metadataError", entry)
            self.assertIn("hexadecimal Git commit SHA", entry["metadataError"])

    def test_legacy_source_commit_field_requires_explicit_metadata_migration(self):
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            local = root / "demo"
            local.mkdir(parents=True)
            (local / "SKILL.md").write_text("demo\n", encoding="utf-8")
            metadata_path = local / ".openskills.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "source": "example/demo",
                        "sourceType": "git",
                        "repoUrl": "https://github.com/example/demo",
                        "subpath": ".",
                        "sourceCommitSha": "a" * 40,
                    }
                ),
                encoding="utf-8",
            )

            entry = sync_registry(root)["entries"]["demo"]

            self.assertIn("Legacy sourceCommitSha is unsupported", entry["metadataError"])
            self.assertNotIn("installedBaseVersion", json.loads(metadata_path.read_text(encoding="utf-8")))

    def test_invalid_legacy_registry_base_is_not_written_back_to_metadata(self):
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            local = root / "demo"
            local.mkdir(parents=True)
            (local / "SKILL.md").write_text("demo\n", encoding="utf-8")
            metadata_path = local / ".openskills.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "source": "example/demo",
                        "sourceType": "git",
                        "repoUrl": "https://github.com/example/demo",
                        "subpath": ".",
                    }
                ),
                encoding="utf-8",
            )
            (root / ".skills-list.json").write_text(
                json.dumps(
                    {
                        "version": 2,
                        "entries": {
                            "demo": {
                                "name": "demo",
                                "localVersion": "not-a-valid-sha",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            entry = sync_registry(root)["entries"]["demo"]

            self.assertEqual(entry["installedBaseVersion"], "unknown")
            self.assertIn("metadataError", entry)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertNotIn("installedBaseVersion", metadata)
            self.assertNotIn("sourceCommitSha", metadata)

    def test_non_object_metadata_fails_with_controlled_error(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            local = root / "demo"
            local.mkdir(parents=True)
            (local / "SKILL.md").write_text("demo\n", encoding="utf-8")
            (local / ".openskills.json").write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(AgentSkillUpdaterError, "must be a JSON object"):
                sync_registry(root)

    def test_initial_registry_failure_still_emits_json(self):
        import scripts.update_agent_skills as updater

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(updater.sys, "argv", ["update_agent_skills.py", "--json"]):
            with mock.patch(
                "scripts.update_agent_skills.sync_registry",
                side_effect=PermissionError("registry access denied"),
            ):
                with self.assertRaises(SystemExit) as exit_info:
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        updater._run_cli()

        self.assertEqual(exit_info.exception.code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["status"], "error")
        self.assertIn("registry access denied", payload[0]["error_message"])
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_git_update_json_reports_the_refreshed_installed_base(self):
        import scripts.update_agent_skills as updater

        old_sha = "a" * 40
        new_sha = "b" * 40
        entry = {
            "name": "demo",
            "entryType": "single-skill",
            "updateMode": "git-worktree",
            "path": r"C:\skills\demo",
            "repoUrl": "https://github.com/example/demo",
            "source": "example/demo",
            "sourceType": "git",
            "subpath": ".",
            "installedBaseVersion": old_sha,
            "localVersion": new_sha,
            "managed": True,
        }
        registry = {
            "version": 2,
            "generatedAt": "2026-01-01T00:00:00+00:00",
            "skillsRoot": r"C:\skills",
            "entries": {"demo": entry},
        }
        result = SimpleNamespace(
            status="up_to_date",
            local_version=new_sha,
            remote_version=new_sha,
            relation="equal",
            working_tree_dirty=False,
            error_message=None,
            applied=True,
            action="metadata_refreshed",
            installed_state="committed",
            diagnostic_journal=None,
            cleanup_residue=None,
        )
        stdout = io.StringIO()
        with mock.patch.object(
            updater.sys,
            "argv",
            ["update_agent_skills.py", "--skill", "demo", "--json"],
        ):
            with mock.patch("scripts.update_agent_skills.sync_registry", side_effect=[registry, registry]):
                with mock.patch("scripts.update_agent_skills.update_registry_entries"):
                    with mock.patch(
                        "scripts.update_agent_skills._probe_entry",
                        return_value=updater.EntryProbe(
                            "up_to_date", new_sha, new_sha, git_relation="equal", working_tree_dirty=False
                        ),
                    ):
                        with mock.patch(
                            "scripts.update_agent_skills.update_git_worktree_skill",
                            return_value=result,
                        ):
                            with mock.patch(
                                "scripts.update_agent_skills.registry_entry_uses_git_worktree",
                                return_value=True,
                            ):
                                with self.assertRaises(SystemExit) as exit_info:
                                    with redirect_stdout(stdout):
                                        updater.main()

        self.assertEqual(exit_info.exception.code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["installed_base_version"], new_sha)

    def test_check_updates_returns_failure_for_structured_error(self):
        import scripts.check_updates as checker

        result = checker.SkillInfo(
            name="demo",
            entry_type="single-skill",
            source="example/demo",
            update_mode="git-worktree",
            installed_base_version="a" * 40,
            local_version="a" * 40,
            remote_version=None,
            status=checker.UpdateStatus.ERROR,
            install_path=r"C:\skills\demo",
            managed=True,
            error_message="detached HEAD",
        )
        with mock.patch.object(checker.sys, "argv", ["check_updates.py", "--json"]):
            with mock.patch("scripts.check_updates.probe_updates", return_value=({}, [result])):
                with mock.patch("scripts.check_updates.print_results"):
                    with self.assertRaises(SystemExit) as exit_info:
                        checker.main()

        self.assertEqual(exit_info.exception.code, 1)

    def test_missing_skill_is_structured_json_in_both_entry_points(self):
        import scripts.check_updates as checker
        import scripts.update_agent_skills as updater

        empty_registry = {
            "version": 2,
            "generatedAt": "2026-01-01T00:00:00+00:00",
            "skillsRoot": r"C:\skills",
            "entries": {},
        }
        update_stdout = io.StringIO()
        with mock.patch.object(
            updater.sys,
            "argv",
            ["update_agent_skills.py", "--skill", "missing", "--json"],
        ):
            with mock.patch(
                "scripts.update_agent_skills.sync_registry",
                side_effect=[empty_registry, empty_registry],
            ):
                with mock.patch("scripts.update_agent_skills.update_registry_entries"):
                    with self.assertRaises(SystemExit) as update_exit:
                        with redirect_stdout(update_stdout):
                            updater.main()

        check_stdout = io.StringIO()
        with mock.patch.object(
            checker.sys,
            "argv",
            ["check_updates.py", "--skill", "missing", "--json"],
        ):
            with mock.patch("scripts.check_updates.probe_updates", return_value=({}, [])):
                with self.assertRaises(SystemExit) as check_exit:
                    with redirect_stdout(check_stdout):
                        checker.main()

        self.assertEqual(update_exit.exception.code, 1)
        self.assertEqual(check_exit.exception.code, 1)
        update_item = json.loads(update_stdout.getvalue())[0]
        self.assertEqual(update_item["status"], "error")
        self.assertEqual(update_item["installed_state"], "unchanged")
        self.assertEqual(json.loads(check_stdout.getvalue())[0]["status"], "error")

    def test_snapshot_resolution_stages_the_exact_probed_revision(self):
        from scripts.agent_skill_updater import (
            AgentSkillSource,
            RemoteObservation,
            resolve_skill_update,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            (local / "SKILL.md").write_text("same\n", encoding="utf-8")
            (remote / "SKILL.md").write_text("same\n", encoding="utf-8")
            metadata_path = local / ".openskills.json"
            metadata_path.write_text(
                json.dumps(snapshot_metadata()),
                encoding="utf-8",
            )
            source = AgentSkillSource(
                "demo",
                local,
                "example/demo",
                "git",
                "https://github.com/example/demo",
                ".",
                None,
                None,
                metadata_path,
                entry_type="single-skill",
            )
            remote_sha = "a" * 40

            observation = RemoteObservation.from_source(
                source,
                revision=remote_sha,
                version=remote_sha,
            )
            with mock.patch(
                "scripts.agent_skill_updater._stage_git_skill_at_ref",
                return_value=remote,
            ) as stage:
                result = resolve_skill_update(
                    source,
                    root / "stage",
                    observation,
                )

            self.assertEqual(result.status, "up_to_date")
            self.assertEqual(result.remote_version, remote_sha)
            stage.assert_called_once_with(source, root / "stage", remote_sha)

    def test_case_insensitive_payload_name_collision_fails_closed(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError, _merge_skill_directories

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = root / "base"
            local = root / "local"
            remote = root / "remote"
            for directory in (base, local, remote):
                directory.mkdir()
                (directory / "SKILL.md").write_text("same\n", encoding="utf-8")
            (local / "Node.txt").write_text("local\n", encoding="utf-8")
            (remote / "node.txt").write_text("remote\n", encoding="utf-8")

            with self.assertRaisesRegex(AgentSkillUpdaterError, "case-insensitive"):
                _merge_skill_directories(
                    base_dir=base,
                    local_dir=local,
                    remote_dir=remote,
                    merged_dir=root / "merged",
                    conflict_root=root / "conflicts",
                )

    def test_changed_displaced_payload_is_preserved_during_rollback(self):
        from scripts.agent_skill_updater import (
            AgentSkillSource,
            AgentSkillUpdate,
            AgentSkillUpdaterError,
            directory_signature,
            update_skill_from_staged,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "skills" / "demo"
            base = root / "base"
            remote = root / "remote"
            for directory in (local, base, remote):
                directory.mkdir(parents=True)
                (directory / "SKILL.md").write_text("same\n", encoding="utf-8")
            for directory in (local, base):
                (directory / "data.txt").write_text("old\n", encoding="utf-8")
            (remote / "data.txt").write_text("new\n", encoding="utf-8")
            metadata_path = local / ".openskills.json"
            metadata_path.write_text(
                json.dumps(snapshot_metadata()),
                encoding="utf-8",
            )
            source = AgentSkillSource(
                "demo", local, "example/demo", "git", "https://github.com/example/demo",
                ".", None, None, metadata_path,
                entry_type="single-skill",
            )
            update = AgentSkillUpdate(
                source, remote, "update_available", "a" * 40, "a" * 40, "b" * 40
            )
            backup_root = root / "backup"
            backup_root.mkdir()
            real_signature = directory_signature
            injected = {"value": False}

            def mutate_displaced(path):
                path = Path(path)
                if path.name == "displaced" and not injected["value"]:
                    (path / "data.txt").write_text("concurrent\n", encoding="utf-8")
                    injected["value"] = True
                return real_signature(path)

            with mock.patch("scripts.agent_skill_updater._stage_git_skill_at_ref", return_value=base):
                with mock.patch(
                    "scripts.agent_skill_updater.directory_signature",
                    side_effect=mutate_displaced,
                ):
                    with self.assertRaisesRegex(AgentSkillUpdaterError, "original payload was restored"):
                        update_skill_from_staged(update, backup_root)

            self.assertTrue(injected["value"])
            self.assertEqual((local / "data.txt").read_text(encoding="utf-8"), "old\n")
            recovered = list(local.parent.glob(".recovery-demo.update-*/data.txt"))
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].read_text(encoding="utf-8"), "concurrent\n")

    def test_snapshot_metadata_compare_and_swap_preserves_concurrent_write(self):
        from scripts.agent_skill_updater import (
            TRANSACTION_PHASE_COMMITTING_METADATA,
            AgentSkillSource,
            AgentSkillUpdate,
            AgentSkillUpdaterError,
            _set_transaction_phase,
            update_skill_from_staged,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "skills" / "demo"
            base = root / "base"
            remote = root / "remote"
            for directory in (local, base, remote):
                directory.mkdir(parents=True)
            (local / "SKILL.md").write_text("old\n", encoding="utf-8")
            (base / "SKILL.md").write_text("old\n", encoding="utf-8")
            (remote / "SKILL.md").write_text("new\n", encoding="utf-8")
            metadata_path = local / ".openskills.json"
            original_metadata = json.dumps(snapshot_metadata()).encode("utf-8")
            concurrent_metadata = json.dumps(
                snapshot_metadata(updatePolicy="local-only", concurrent=True)
            ).encode("utf-8")
            metadata_path.write_bytes(original_metadata)
            source = AgentSkillSource(
                "demo", local, "example/demo", "git", "https://github.com/example/demo",
                ".", None, None, metadata_path,
                entry_type="single-skill",
            )
            update = AgentSkillUpdate(
                source, remote, "update_available", "a" * 40, "a" * 40, "b" * 40
            )
            backup_root = root / "backup"
            backup_root.mkdir()
            injected = {"value": False}

            def inject_metadata(transaction_root, state, phase):
                _set_transaction_phase(transaction_root, state, phase)
                if phase == TRANSACTION_PHASE_COMMITTING_METADATA and not injected["value"]:
                    metadata_path.write_bytes(concurrent_metadata)
                    injected["value"] = True

            with mock.patch("scripts.agent_skill_updater._stage_git_skill_at_ref", return_value=base):
                with mock.patch(
                    "scripts.agent_skill_updater._set_transaction_phase",
                    side_effect=inject_metadata,
                ):
                    with self.assertRaisesRegex(AgentSkillUpdaterError, "original payload was restored"):
                        update_skill_from_staged(update, backup_root)

            self.assertTrue(injected["value"])
            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "old\n")
            self.assertEqual(metadata_path.read_bytes(), concurrent_metadata)
            recovered = list(
                local.parent.glob(
                    ".recovery-demo.update-*/control-metadata/.openskills.json.concurrent-*.json"
                )
            )
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].read_bytes(), concurrent_metadata)

    def test_snapshot_apply_rechecks_local_only_after_lock_before_any_mutation(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "skills" / "demo"
            merged = root / "merged"
            local.mkdir(parents=True)
            merged.mkdir()
            (local / "SKILL.md").write_text("old\n", encoding="utf-8")
            (merged / "SKILL.md").write_text("new\n", encoding="utf-8")
            metadata_path = local / ".openskills.json"
            original_metadata = {
                "source": "example/demo",
                "sourceType": "git",
                "repoUrl": "https://github.com/example/demo",
                "subpath": ".",
                "installedBaseVersion": "a" * 40,
            }
            metadata_path.write_text(json.dumps(original_metadata), encoding="utf-8")
            source = updater.AgentSkillSource(
                "demo",
                local,
                "example/demo",
                "git",
                "https://github.com/example/demo",
                ".",
                None,
                None,
                metadata_path,
                entry_type="single-skill",
            )
            update = updater.AgentSkillUpdate(
                source,
                merged,
                "update_available",
                "a" * 40,
                "a" * 40,
                "b" * 40,
            )
            concurrent_metadata = {
                **original_metadata,
                "updatePolicy": "local-only",
            }
            concurrent_bytes = json.dumps(concurrent_metadata).encode("utf-8")

            class PolicyChangingLock:
                def __enter__(self):
                    metadata_path.write_bytes(concurrent_bytes)

                def __exit__(self, exc_type, exc_value, traceback):
                    return False

            with mock.patch(
                "scripts.agent_skill_updater.skill_update_lock",
                return_value=PolicyChangingLock(),
            ):
                with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "updatePolicy changed"):
                    updater._apply_payload_transaction(update, merged)

            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "old\n")
            self.assertEqual(metadata_path.read_bytes(), concurrent_bytes)
            self.assertFalse((root / "backup").exists())
            self.assertEqual(list(local.parent.glob(".demo.update-*")), [])

    def test_remote_probe_failures_are_errors_and_checker_does_not_rewrite_local_version(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError
        from scripts.check_updates import UpdateStatus, _entry_to_skill_info, probe_updates
        from scripts.update_agent_skills import _probe_entry

        entry = {
            "name": "demo",
            "entryType": "single-skill",
            "path": str(Path(tempfile.gettempdir()) / "not-a-worktree"),
            "source": "example/demo",
            "sourceType": "git",
            "repoUrl": "https://github.com/example/demo",
            "updateMode": "snapshot",
            "installedBaseVersion": "a" * 40,
            "localVersion": "a" * 40,
            "managed": True,
        }
        with mock.patch(
            "scripts.check_updates.fetch_source_remote_version",
            side_effect=AgentSkillUpdaterError("remote unavailable"),
        ):
            info = _entry_to_skill_info(entry)
        with mock.patch(
            "scripts.update_agent_skills.fetch_source_remote_observation",
            side_effect=AgentSkillUpdaterError("remote unavailable"),
        ):
            probe = _probe_entry(entry)
        self.assertEqual(info.status, UpdateStatus.ERROR)
        self.assertEqual(probe.status, "error")

        registry = {
            "version": 2,
            "generatedAt": "2026-01-01T00:00:00+00:00",
            "skillsRoot": str(Path(tempfile.gettempdir())),
            "entries": {"demo": entry},
        }
        captured = {}

        def capture_updates(updates, _root):
            captured.update(updates)
            return registry

        with mock.patch("scripts.check_updates.sync_registry", return_value=registry):
            with mock.patch(
                "scripts.check_updates._entry_to_skill_info",
                return_value=info,
            ):
                with mock.patch(
                    "scripts.check_updates.update_registry_entries",
                    side_effect=capture_updates,
                ):
                    probe_updates()
        self.assertNotIn("localVersion", captured["demo"])

    def test_transaction_cleanup_keeps_control_state_until_payload_cleanup_succeeds(self):
        from scripts.agent_skill_updater import (
            TRANSACTION_MARKER_FILENAME,
            TRANSACTION_STATE_FILENAME,
            _remove_transaction_tree,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            transaction = Path(temp_dir) / ".demo.update-cleanup"
            (transaction / "original").mkdir(parents=True)
            (transaction / "original" / "SKILL.md").write_text("old\n", encoding="utf-8")
            (transaction / TRANSACTION_STATE_FILENAME).write_text("{}", encoding="utf-8")
            (transaction / TRANSACTION_MARKER_FILENAME).write_text("1\n", encoding="utf-8")

            with mock.patch(
                "scripts.agent_skill_updater.shutil.rmtree",
                side_effect=PermissionError("cleanup interrupted"),
            ):
                with self.assertRaisesRegex(PermissionError, "cleanup interrupted"):
                    _remove_transaction_tree(transaction)

            self.assertTrue((transaction / TRANSACTION_STATE_FILENAME).is_file())
            self.assertTrue((transaction / TRANSACTION_MARKER_FILENAME).is_file())
            _remove_transaction_tree(transaction)
            self.assertFalse(transaction.exists())

    def test_transaction_dispatch_uses_explicit_type_not_skill_name(self):
        from scripts.agent_skill_updater import _read_transaction_type

        with tempfile.TemporaryDirectory() as temp_dir:
            transaction = Path(temp_dir) / ".foo.git-update-bar.update-test"
            transaction.mkdir()
            (transaction / "state.json").write_text(
                json.dumps({"transactionType": "snapshot"}),
                encoding="utf-8",
            )
            self.assertEqual(_read_transaction_type(transaction), "snapshot")

    def test_recovery_rejects_skill_root_rebound_to_link_before_mutation(self):
        from scripts.agent_skill_updater import (
            AgentSkillUpdaterError,
            _recover_transaction_from_state,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill = root / "demo"
            skill.mkdir()
            (skill / "SKILL.md").write_text("unchanged\n", encoding="utf-8")
            transaction = root / ".demo.update-test"
            transaction.mkdir()
            expected_metadata = b"{}"
            (transaction / "metadata.expected").write_bytes(expected_metadata)
            state = {
                "skillDir": str(skill),
                "phase": "prepared",
                "originalSignature": "unused",
                "expectedSignature": "unused",
                "originalMetadataPresent": False,
                "originalMetadataSha256": None,
                "expectedMetadataPresent": True,
                "expectedMetadataSha256": hashlib.sha256(expected_metadata).hexdigest(),
            }

            with mock.patch(
                "scripts.agent_skill_updater._is_filesystem_link",
                side_effect=lambda path: Path(path) == skill,
            ):
                with self.assertRaisesRegex(AgentSkillUpdaterError, "symlink or junction"):
                    _recover_transaction_from_state(transaction, state)

            self.assertEqual((skill / "SKILL.md").read_text(encoding="utf-8"), "unchanged\n")


class GitWorktreeIntegrityTests(unittest.TestCase):
    def create_remote_and_clone(self, root: Path) -> tuple[Path, Path, Path, str]:
        remote = root / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(remote)],
            check=True,
            capture_output=True,
        )
        seed = root / "seed"
        subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
        configure_git(seed)
        (seed / "SKILL.md").write_text("version-a\n", encoding="utf-8")
        run_git(seed, "add", "SKILL.md")
        run_git(seed, "commit", "-m", "version a")
        run_git(seed, "push", "-u", "origin", "main")
        version_a = run_git(seed, "rev-parse", "HEAD")
        local = root / "skills" / "demo"
        local.parent.mkdir()
        subprocess.run(["git", "clone", str(remote), str(local)], check=True, capture_output=True)
        configure_git(local)
        return remote, seed, local, version_a

    def advance_remote(self, seed: Path, content: str = "version-b\n") -> str:
        (seed / "SKILL.md").write_text(content, encoding="utf-8")
        run_git(seed, "add", "SKILL.md")
        run_git(seed, "commit", "-m", content.strip())
        run_git(seed, "push", "origin", "main")
        return run_git(seed, "rev-parse", "HEAD")

    def create_pack_remote_and_clone(self, root: Path) -> tuple[Path, Path, Path, str]:
        remote = root / "pack-remote.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(remote)],
            check=True,
            capture_output=True,
        )
        seed = root / "pack-seed"
        subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
        configure_git(seed)
        skill_file = seed / "skills" / "demo" / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("pack-a\n", encoding="utf-8")
        run_git(seed, "add", "skills/demo/SKILL.md")
        run_git(seed, "commit", "-m", "pack a")
        run_git(seed, "push", "-u", "origin", "main")
        version_a = run_git(seed, "rev-parse", "HEAD")
        local = root / "skills" / "demo-pack"
        local.parent.mkdir()
        subprocess.run(["git", "clone", str(remote), str(local)], check=True, capture_output=True)
        configure_git(local)
        return remote, seed, local, version_a

    def pack_source_for(self, local: Path):
        from scripts.agent_skill_updater import AgentSkillSource

        metadata_path = local / ".openskills.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "source": "local/demo-pack",
                    "sourceType": "git-pack",
                    "repoUrl": run_git(local, "config", "--get", "remote.origin.url"),
                    "subpath": ".",
                    "installedBaseVersion": run_git(local, "rev-parse", "HEAD"),
                }
            ),
            encoding="utf-8",
        )
        return AgentSkillSource(
            name="demo-pack",
            local_dir=local,
            source="local/demo-pack",
            source_type="git-pack",
            repo_url=run_git(local, "config", "--get", "remote.origin.url"),
            subpath=".",
            generator=None,
            workflow_id=None,
            metadata_path=metadata_path,
            entry_type="skill-pack",
        )

    def write_metadata(self, local: Path, remote: Path, installed_base: str) -> Path:
        metadata_path = local / ".openskills.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "source": "local/demo",
                    "sourceType": "git",
                    "repoUrl": run_git(local, "config", "--get", "remote.origin.url"),
                    "subpath": ".",
                    "installedBaseVersion": installed_base,
                }
            ),
            encoding="utf-8",
        )
        return metadata_path

    def source_for(self, local: Path, metadata_path: Path):
        from scripts.agent_skill_updater import AgentSkillSource

        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        return AgentSkillSource(
            name="demo",
            local_dir=local,
            source=metadata["source"],
            source_type="git",
            repo_url=metadata["repoUrl"],
            subpath=".",
            generator=None,
            workflow_id=None,
            metadata_path=metadata_path,
            entry_type="single-skill",
        )

    def test_git_probe_and_update_recheck_local_only_after_lock_acquisition(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, _, local, version = self.create_remote_and_clone(root)
            metadata_path = self.write_metadata(local, remote, version)
            original = json.loads(metadata_path.read_text(encoding="utf-8"))
            concurrent = {**original, "updatePolicy": "local-only"}
            concurrent_bytes = json.dumps(concurrent).encode("utf-8")

            for operation_name in ("probe", "update"):
                with self.subTest(operation=operation_name):
                    metadata_path.write_text(json.dumps(original), encoding="utf-8")
                    source = self.source_for(local, metadata_path)

                    class PolicyChangingLock:
                        def __enter__(self):
                            metadata_path.write_bytes(concurrent_bytes)

                        def __exit__(self, exc_type, exc_value, traceback):
                            return False

                    operation = (
                        updater.probe_git_worktree
                        if operation_name == "probe"
                        else updater.update_git_worktree_skill
                    )
                    with mock.patch(
                        "scripts.agent_skill_updater.skill_update_lock",
                        return_value=PolicyChangingLock(),
                    ):
                        with mock.patch(
                            "scripts.agent_skill_updater._git_fetch_remote_branch"
                        ) as fetch:
                            with self.assertRaisesRegex(
                                updater.AgentSkillUpdaterError,
                                "updatePolicy changed",
                            ):
                                operation(source)

                    fetch.assert_not_called()
                    self.assertEqual(metadata_path.read_bytes(), concurrent_bytes)

    def test_registry_separates_installed_base_from_git_head(self):
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, seed, local, version_a = self.create_remote_and_clone(root)
            version_b = self.advance_remote(seed)
            run_git(local, "pull", "--ff-only")
            metadata_path = self.write_metadata(local, remote, version_a)

            registry = sync_registry(local.parent)

            entry = registry["entries"]["demo"]
            self.assertEqual(entry["updateMode"], "git-worktree")
            self.assertEqual(entry["installedBaseVersion"], version_a)
            self.assertEqual(entry["localVersion"], version_b)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["installedBaseVersion"], version_a)

    def test_equal_head_with_stale_base_only_refreshes_metadata(self):
        from scripts.agent_skill_updater import update_git_worktree_skill

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, seed, local, version_a = self.create_remote_and_clone(root)
            version_b = self.advance_remote(seed)
            run_git(local, "pull", "--ff-only")
            metadata_path = self.write_metadata(local, remote, version_a)
            skill_bytes = (local / "SKILL.md").read_bytes()

            result = update_git_worktree_skill(self.source_for(local, metadata_path))

            self.assertEqual(result.action, "metadata_refreshed")
            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_b)
            self.assertEqual((local / "SKILL.md").read_bytes(), skill_bytes)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["installedBaseVersion"], version_b)
            self.assertNotIn("sourceCommitSha", metadata)

    def test_equal_head_expands_short_installed_base_to_full_sha(self):
        from scripts.agent_skill_updater import update_git_worktree_skill

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, _, local, version = self.create_remote_and_clone(root)
            metadata_path = self.write_metadata(local, remote, version[:12])

            result = update_git_worktree_skill(self.source_for(local, metadata_path))

            self.assertEqual(result.action, "metadata_refreshed")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["installedBaseVersion"], version)

    def test_git_equal_metadata_refresh_preserves_concurrent_local_only_write(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, _, local, version = self.create_remote_and_clone(root)
            metadata_path = self.write_metadata(local, remote, version[:12])
            source = self.source_for(local, metadata_path)
            concurrent = b'{"updatePolicy":"local-only","concurrent":true}'
            real_set_phase = updater._set_coordinator_phase
            injected = {"value": False}

            def inject_concurrent_write(transaction_root, state, phase):
                real_set_phase(transaction_root, state, phase)
                if phase == updater.COORDINATOR_PHASE_CAPTURING_METADATA and not injected["value"]:
                    metadata_path.write_bytes(concurrent)
                    injected["value"] = True

            with mock.patch(
                "scripts.agent_skill_updater._set_coordinator_phase",
                side_effect=inject_concurrent_write,
            ):
                result = updater.update_git_worktree_skill(source)

            self.assertTrue(injected["value"])
            self.assertEqual(result.status, "error")
            self.assertEqual(result.installed_state, "uncertain")
            self.assertIn("Concurrent write", result.error_message)
            self.assertIsNotNone(result.diagnostic_journal)
            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version)
            self.assertEqual(metadata_path.read_bytes(), concurrent)

    def test_git_equal_metadata_refresh_rejects_origin_change_after_publish(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, _, local, version = self.create_remote_and_clone(root)
            metadata_path = self.write_metadata(local, remote, version[:12])
            original_metadata = metadata_path.read_bytes()
            changed_origin = str(root / "other.git")
            real_commit = updater._commit_transaction_metadata

            def change_origin_after_publish(*args, **kwargs):
                real_commit(*args, **kwargs)
                run_git(local, "config", "remote.origin.url", changed_origin)

            with mock.patch(
                "scripts.agent_skill_updater._commit_transaction_metadata",
                side_effect=change_origin_after_publish,
            ):
                result = updater.update_git_worktree_skill(
                    self.source_for(local, metadata_path)
                )

            self.assertEqual(result.status, "error")
            self.assertEqual(result.installed_state, "rolled_back")
            self.assertIn("Metadata refresh failed", result.error_message)
            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version)
            self.assertEqual(metadata_path.read_bytes(), original_metadata)
            self.assertEqual(run_git(local, "config", "--get", "remote.origin.url"), changed_origin)
            self.assertEqual(list(local.parent.glob(".demo.transaction-*")), [])

    def test_clean_behind_fast_forwards_without_damaging_repository(self):
        from scripts.agent_skill_updater import update_git_worktree_skill

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, seed, local, version_a = self.create_remote_and_clone(root)
            version_b = self.advance_remote(seed)
            metadata_path = self.write_metadata(local, remote, version_a)
            origin_before = run_git(local, "config", "--get", "remote.origin.url")

            result = update_git_worktree_skill(self.source_for(local, metadata_path))

            self.assertEqual(result.action, "fast_forwarded")
            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_b)
            self.assertEqual(run_git(local, "config", "--get", "remote.origin.url"), origin_before)
            self.assertTrue((local / ".git").exists())
            run_git(local, "fsck", "--full")

    def test_git_cleanup_failure_reports_committed_update_state(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, seed, local, version_a = self.create_remote_and_clone(root)
            version_b = self.advance_remote(seed)
            metadata_path = self.write_metadata(local, remote, version_a)

            with mock.patch(
                "scripts.agent_skill_updater._remove_transaction_tree",
                side_effect=updater.AgentSkillUpdaterError("cleanup failed"),
            ):
                with self.assertRaises(updater.AgentSkillUpdateCommittedError) as raised:
                    updater.update_git_worktree_skill(self.source_for(local, metadata_path))

            self.assertEqual(raised.exception.action, "fast_forwarded")
            self.assertEqual(raised.exception.version, version_b)
            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_b)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["installedBaseVersion"], version_b)
            self.assertEqual(len(list(local.parent.glob(".demo.git-update-*"))), 1)

    def test_skill_pack_uses_the_transactional_git_worktree_engine(self):
        from scripts.agent_skill_updater import update_git_worktree_skill

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, seed, local, version_a = self.create_pack_remote_and_clone(root)
            skill_file = seed / "skills" / "demo" / "SKILL.md"
            skill_file.write_text("pack-b\n", encoding="utf-8")
            run_git(seed, "add", "skills/demo/SKILL.md")
            run_git(seed, "commit", "-m", "pack b")
            run_git(seed, "push", "origin", "main")
            version_b = run_git(seed, "rev-parse", "HEAD")
            source = self.pack_source_for(local)

            result = update_git_worktree_skill(source)

            self.assertEqual(result.action, "fast_forwarded")
            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_b)
            self.assertNotEqual(version_a, version_b)
            self.assertEqual(
                (local / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8"),
                "pack-b\n",
            )
            self.assertTrue((local / ".git").exists())
            self.assertEqual(list(local.parent.glob(".demo-pack.git-update-*")), [])
            metadata = json.loads((local / ".openskills.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["installedBaseVersion"], version_b)

    def test_skill_pack_metadata_failure_rolls_back_head_and_payload(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, seed, local, version_a = self.create_pack_remote_and_clone(root)
            skill_file = seed / "skills" / "demo" / "SKILL.md"
            skill_file.write_text("pack-b\n", encoding="utf-8")
            run_git(seed, "add", "skills/demo/SKILL.md")
            run_git(seed, "commit", "-m", "pack b")
            run_git(seed, "push", "origin", "main")
            source = self.pack_source_for(local)
            metadata_path = local / ".openskills.json"
            original_metadata = metadata_path.read_bytes()

            with mock.patch(
                "scripts.agent_skill_updater._commit_transaction_metadata",
                side_effect=PermissionError("pack metadata failure"),
            ):
                with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "were restored"):
                    updater.update_git_worktree_skill(source)

            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_a)
            self.assertEqual(
                (local / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8"),
                "pack-a\n",
            )
            self.assertEqual(metadata_path.read_bytes(), original_metadata)
            self.assertEqual(list(local.parent.glob(".demo-pack.git-update-*")), [])

    def test_skill_pack_remote_without_skills_directory_is_rejected_before_apply(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError, update_git_worktree_skill

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, seed, local, version_a = self.create_pack_remote_and_clone(root)
            shutil.rmtree(seed / "skills")
            run_git(seed, "add", "-A")
            run_git(seed, "commit", "-m", "remove skills")
            run_git(seed, "push", "origin", "main")
            source = self.pack_source_for(local)
            metadata_before = (local / ".openskills.json").read_bytes()

            with self.assertRaisesRegex(AgentSkillUpdaterError, "required skills tree"):
                update_git_worktree_skill(source)

            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_a)
            self.assertTrue((local / "skills" / "demo" / "SKILL.md").is_file())
            self.assertEqual((local / ".openskills.json").read_bytes(), metadata_before)

    def test_apply_branch_switch_cannot_rewrite_the_checked_out_branch(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, seed, local, version_a = self.create_remote_and_clone(root)
            version_b = self.advance_remote(seed)
            metadata_path = self.write_metadata(local, remote, version_a)
            run_git(local, "branch", "topic", version_a)
            original_metadata = metadata_path.read_bytes()
            real_run = updater._run
            switched = {"value": False}

            def switch_branch_after_forward_cas(command, cwd):
                real_run(command, cwd)
                if (
                    not switched["value"]
                    and "update-ref" in command
                    and command[-2:] == [version_b, version_a]
                ):
                    switched["value"] = True
                    run_git(local, "checkout", "topic")

            with mock.patch(
                "scripts.agent_skill_updater._run",
                side_effect=switch_branch_after_forward_cas,
            ):
                with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "Rollback also failed"):
                    updater.update_git_worktree_skill(self.source_for(local, metadata_path))

            self.assertTrue(switched["value"])
            self.assertEqual(run_git(local, "symbolic-ref", "--short", "HEAD"), "topic")
            self.assertEqual(run_git(local, "rev-parse", "refs/heads/main"), version_a)
            self.assertEqual(run_git(local, "rev-parse", "refs/heads/topic"), version_a)
            self.assertEqual(metadata_path.read_bytes(), original_metadata)
            transactions = list(local.parent.glob(".demo.git-update-*"))
            self.assertEqual(len(transactions), 1)

            run_git(local, "checkout", "main")
            updater.recover_incomplete_skill_transactions(local.parent)
            self.assertFalse(transactions[0].exists())
            self.assertFalse(updater._git_worktree_has_payload_changes(local))

    def test_dirty_behind_refuses_pull_and_preserves_head(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError, probe_git_worktree, update_git_worktree_skill

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, seed, local, version_a = self.create_remote_and_clone(root)
            version_b = self.advance_remote(seed)
            metadata_path = self.write_metadata(local, remote, version_a)
            (local / "SKILL.md").write_text("local dirty change\n", encoding="utf-8")
            source = self.source_for(local, metadata_path)

            probe = probe_git_worktree(source)
            self.assertEqual(probe.relation, "behind")
            self.assertTrue(probe.working_tree_dirty)
            with self.assertRaisesRegex(AgentSkillUpdaterError, "payload changes"):
                update_git_worktree_skill(source)

            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_a)
            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "local dirty change\n")

    def test_ignored_path_that_remote_will_track_blocks_fast_forward(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError, probe_git_worktree, update_git_worktree_skill

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, seed, local, version_a = self.create_remote_and_clone(root)
            metadata_path = self.write_metadata(local, remote, version_a)
            (local / ".git" / "info" / "exclude").write_text("ignored.txt\n", encoding="utf-8")
            ignored_path = local / "ignored.txt"
            ignored_path.write_text("local secret\n", encoding="utf-8")
            (seed / "ignored.txt").write_text("remote tracked value\n", encoding="utf-8")
            run_git(seed, "add", "-f", "ignored.txt")
            run_git(seed, "commit", "-m", "track formerly ignored path")
            run_git(seed, "push", "origin", "main")
            source = self.source_for(local, metadata_path)

            probe = probe_git_worktree(source)
            self.assertEqual(probe.relation, "behind")
            self.assertTrue(probe.working_tree_dirty)
            self.assertEqual(probe.ignored_conflicts, ("ignored.txt",))
            with self.assertRaisesRegex(AgentSkillUpdaterError, "Ignored paths would be overwritten"):
                update_git_worktree_skill(source)

            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_a)
            self.assertEqual(ignored_path.read_text(encoding="utf-8"), "local secret\n")

    def test_ahead_is_safe_and_diverged_fails_closed(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError, probe_git_worktree, update_git_worktree_skill

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, seed, local, version_a = self.create_remote_and_clone(root)
            metadata_path = self.write_metadata(local, remote, version_a)
            (local / "local.txt").write_text("local commit\n", encoding="utf-8")
            run_git(local, "add", "local.txt")
            run_git(local, "commit", "-m", "local ahead")
            local_head = run_git(local, "rev-parse", "HEAD")
            source = self.source_for(local, metadata_path)

            ahead = probe_git_worktree(source)
            self.assertEqual(ahead.relation, "ahead")
            self.assertEqual(ahead.status, "up_to_date")
            self.assertEqual(run_git(local, "rev-parse", "HEAD"), local_head)

            self.advance_remote(seed, "remote divergent change\n")
            diverged = probe_git_worktree(source)
            self.assertEqual(diverged.relation, "diverged")
            self.assertEqual(diverged.status, "error")
            with self.assertRaisesRegex(AgentSkillUpdaterError, "diverged"):
                update_git_worktree_skill(source)
            self.assertEqual(run_git(local, "rev-parse", "HEAD"), local_head)

    def test_detached_head_and_untracked_topic_branch_fail_closed(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError, probe_git_worktree

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, seed, local, version_a = self.create_remote_and_clone(root)
            metadata_path = self.write_metadata(local, remote, version_a)
            source = self.source_for(local, metadata_path)

            run_git(local, "checkout", "--detach", version_a)
            with self.assertRaises(AgentSkillUpdaterError):
                probe_git_worktree(source)

            run_git(local, "checkout", "-b", "topic")
            self.advance_remote(seed)
            with self.assertRaisesRegex(AgentSkillUpdaterError, "explicit upstream"):
                probe_git_worktree(source)
            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_a)

    def test_metadata_failure_after_fast_forward_restores_head_and_metadata(self):
        from scripts.agent_skill_updater import (
            AgentSkillUpdaterError,
            update_git_worktree_skill,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, seed, local, version_a = self.create_remote_and_clone(root)
            version_b = self.advance_remote(seed)
            metadata_path = self.write_metadata(local, remote, version_a)
            original_metadata = metadata_path.read_bytes()
            source = self.source_for(local, metadata_path)

            def fail_metadata_write(_transaction, _state, path, *_args, **_kwargs):
                self.assertEqual(Path(path), metadata_path)
                self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_b)
                raise PermissionError("injected metadata failure")

            with mock.patch(
                "scripts.agent_skill_updater._commit_transaction_metadata",
                side_effect=fail_metadata_write,
            ):
                with self.assertRaisesRegex(
                    AgentSkillUpdaterError,
                    "HEAD, payload, and metadata were restored",
                ):
                    update_git_worktree_skill(source)

            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_a)
            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "version-a\n")
            self.assertEqual(metadata_path.read_bytes(), original_metadata)

    def test_origin_change_after_metadata_publish_rolls_back_git_update(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, seed, local, version_a = self.create_remote_and_clone(root)
            version_b = self.advance_remote(seed)
            metadata_path = self.write_metadata(local, remote, version_a)
            original_metadata = metadata_path.read_bytes()
            source = self.source_for(local, metadata_path)
            changed_origin = str(root / "other.git")
            real_verify = updater._verify_git_source_configuration
            injected = {"value": False}

            def change_origin_after_metadata_publish(current_source, branch, remote_ref):
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if (
                    metadata.get("installedBaseVersion") == version_b
                    and run_git(local, "rev-parse", "HEAD") == version_b
                    and not injected["value"]
                ):
                    run_git(local, "config", "remote.origin.url", changed_origin)
                    injected["value"] = True
                return real_verify(current_source, branch, remote_ref)

            with mock.patch(
                "scripts.agent_skill_updater._verify_git_source_configuration",
                side_effect=change_origin_after_metadata_publish,
            ):
                with self.assertRaisesRegex(
                    updater.AgentSkillUpdaterError,
                    "HEAD, payload, and metadata were restored",
                ):
                    updater.update_git_worktree_skill(source)

            self.assertTrue(injected["value"])
            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_a)
            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "version-a\n")
            self.assertEqual(metadata_path.read_bytes(), original_metadata)
            self.assertEqual(run_git(local, "config", "--get", "remote.origin.url"), changed_origin)
            self.assertEqual(list(local.parent.glob(".demo.git-update-*")), [])

    def test_git_rollback_restores_tracked_residue_even_when_head_never_moved(self):
        from scripts.agent_skill_updater import (
            _copy_directory_contents,
            _rollback_git_fast_forward,
            _stage_git_revision_payload,
            directory_signature,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, seed, local, version_a = self.create_remote_and_clone(root)
            (seed / "new" / "nested").mkdir(parents=True)
            (seed / "new" / "nested" / "remote.txt").write_text("remote\n", encoding="utf-8")
            (seed / "SKILL.md").write_text("version-b\n", encoding="utf-8")
            run_git(seed, "add", "SKILL.md", "new/nested/remote.txt")
            run_git(seed, "commit", "-m", "version b with new path")
            run_git(seed, "push", "origin", "main")
            version_b = run_git(seed, "rev-parse", "HEAD")
            run_git(local, "fetch", "origin")

            transaction = root / ".demo.git-update-test"
            original_payload = transaction / "original"
            incoming_payload = transaction / "incoming"
            original_payload.mkdir(parents=True)
            _copy_directory_contents(local, original_payload)
            _stage_git_revision_payload(local, version_b, incoming_payload, "single-skill")

            (local / "SKILL.md").write_text("partial checkout residue\n", encoding="utf-8")
            (local / "new" / "nested").mkdir(parents=True)
            (local / "new" / "nested" / "remote.txt").write_text("partial\n", encoding="utf-8")
            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_a)

            recovery_path = _rollback_git_fast_forward(
                local,
                version_a,
                version_b,
                "main",
                original_payload,
                incoming_payload,
                transaction,
                directory_signature(original_payload),
                directory_signature(incoming_payload),
                "single-skill",
            )

            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_a)
            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "version-a\n")
            self.assertFalse((local / "new").exists())
            self.assertEqual(run_git(local, "status", "--porcelain=v1"), "")
            self.assertIsNotNone(recovery_path)

    def test_git_rollback_branch_switch_cannot_rewrite_the_new_branch(self):
        import scripts.agent_skill_updater as updater

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, seed, local, version_a = self.create_remote_and_clone(root)
            version_b = self.advance_remote(seed)
            run_git(local, "fetch", "origin")
            run_git(local, "branch", "topic", version_a)

            transaction = root / ".demo.git-update-branch-race"
            original_payload = transaction / "original"
            incoming_payload = transaction / "incoming"
            original_payload.mkdir(parents=True)
            updater._copy_directory_contents(local, original_payload)
            updater._stage_git_revision_payload(
                local,
                version_b,
                incoming_payload,
                "single-skill",
            )
            run_git(local, "merge", "--ff-only", version_b)

            real_validate = updater._validate_git_rollback_ref
            validation_calls = {"count": 0}

            def switch_after_ref_compare_and_swap(*args):
                real_validate(*args)
                validation_calls["count"] += 1
                if validation_calls["count"] == 2:
                    run_git(local, "checkout", "topic")

            with mock.patch(
                "scripts.agent_skill_updater._validate_git_rollback_ref",
                side_effect=switch_after_ref_compare_and_swap,
            ):
                with self.assertRaisesRegex(
                    updater.AgentSkillUpdaterError,
                    "requires branch 'main'",
                ):
                    updater._rollback_git_fast_forward(
                        local,
                        version_a,
                        version_b,
                        "main",
                        original_payload,
                        incoming_payload,
                        transaction,
                        updater.directory_signature(original_payload),
                        updater.directory_signature(incoming_payload),
                        "single-skill",
                    )

            self.assertEqual(run_git(local, "symbolic-ref", "--short", "HEAD"), "topic")
            self.assertEqual(run_git(local, "rev-parse", "refs/heads/main"), version_a)
            self.assertEqual(run_git(local, "rev-parse", "refs/heads/topic"), version_a)

    def test_git_control_entry_fails_closed_when_repository_is_invalid(self):
        from scripts.agent_skill_updater import (
            AgentSkillSource,
            AgentSkillUpdaterError,
            probe_git_worktree,
            resolve_skill_update,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            local = Path(temp_dir) / "broken"
            (local / ".git").mkdir(parents=True)
            (local / "SKILL.md").write_text("broken repo\n", encoding="utf-8")
            metadata_path = local / ".openskills.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        **snapshot_metadata(),
                        "source": "example/broken",
                        "repoUrl": "https://github.com/example/broken",
                    }
                ),
                encoding="utf-8",
            )
            source = AgentSkillSource(
                "broken",
                local,
                "example/broken",
                "git",
                "https://github.com/example/broken",
                ".",
                None,
                None,
                metadata_path,
                entry_type="single-skill",
            )

            with self.assertRaises(AgentSkillUpdaterError):
                probe_git_worktree(source)
            with mock.patch("scripts.agent_skill_updater.stage_remote_skill") as stage:
                with self.assertRaisesRegex(AgentSkillUpdaterError, "dedicated Git update path"):
                    resolve_skill_update(source, Path(temp_dir) / "stage")
            stage.assert_not_called()

    def test_linked_git_control_entry_is_rejected_before_any_git_command(self):
        from scripts.agent_skill_updater import (
            AgentSkillSource,
            AgentSkillUpdaterError,
            probe_git_worktree,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            local = Path(temp_dir) / "linked-control"
            (local / ".git").mkdir(parents=True)
            (local / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
            metadata_path = local / ".openskills.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "source": "example/demo",
                        "sourceType": "git",
                        "repoUrl": "https://github.com/example/demo",
                        "subpath": ".",
                        "installedBaseVersion": "a" * 40,
                    }
                ),
                encoding="utf-8",
            )
            source = AgentSkillSource(
                "demo",
                local,
                "example/demo",
                "git",
                "https://github.com/example/demo",
                ".",
                None,
                None,
                metadata_path,
                entry_type="single-skill",
            )

            def is_link(path):
                return Path(path).name == ".git"

            with mock.patch("scripts.agent_skill_updater._is_filesystem_link", side_effect=is_link):
                with mock.patch("scripts.agent_skill_updater.subprocess.run") as git_process:
                    with self.assertRaisesRegex(AgentSkillUpdaterError, "regular file or directory"):
                        probe_git_worktree(source)
            git_process.assert_not_called()

    def test_linked_worktree_gitfile_uses_git_update_mode(self):
        from scripts.agent_skill_updater import is_git_worktree_skill, probe_git_worktree

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, _, local, version_a = self.create_remote_and_clone(root)
            linked = root / "linked-worktree"
            run_git(local, "worktree", "add", "-b", "linked-test", str(linked))
            configure_git(linked)
            run_git(linked, "branch", "--set-upstream-to", "origin/main", "linked-test")
            metadata_path = self.write_metadata(linked, remote, version_a)

            self.assertTrue((linked / ".git").is_file())
            self.assertTrue(is_git_worktree_skill(linked))
            result = probe_git_worktree(self.source_for(linked, metadata_path))
            self.assertEqual(result.relation, "equal")
            self.assertFalse(result.working_tree_dirty)

    def test_registry_marks_origin_metadata_mismatch_as_error(self):
        import scripts.update_agent_skills as updater
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, _, local, version_a = self.create_remote_and_clone(root)
            metadata_path = self.write_metadata(local, remote, version_a)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["repoUrl"] = "https://github.com/example/wrong-repository"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

            registry = sync_registry(local.parent)
            entry = registry["entries"]["demo"]
            self.assertIn("metadataError", entry)
            probe = updater._probe_entry(entry)
            self.assertEqual(probe.status, "error")
            self.assertIn("differ", probe.error_message)

    def test_remote_target_is_explicit_and_broken_upstream_fails_closed(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError, probe_git_worktree

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, seed, local, version_a = self.create_remote_and_clone(root)
            version_b = self.advance_remote(seed)
            metadata_path = self.write_metadata(local, remote, version_a)
            source = self.source_for(local, metadata_path)
            run_git(local, "config", "--unset-all", "remote.origin.fetch")
            run_git(
                local,
                "config",
                "--add",
                "remote.origin.fetch",
                "+refs/heads/other:refs/remotes/origin/other",
            )

            result = probe_git_worktree(source)
            self.assertEqual(result.relation, "behind")
            self.assertEqual(result.remote_version, version_b)

            run_git(local, "config", "branch.main.merge", "refs/heads/deleted-upstream")
            with self.assertRaises(AgentSkillUpdaterError):
                probe_git_worktree(source)

            run_git(local, "config", "--unset", "branch.main.remote")
            run_git(local, "config", "--unset", "branch.main.merge")
            run_git(seed, "checkout", "-b", "release")
            run_git(seed, "push", "-u", "origin", "release")
            run_git(remote, "symbolic-ref", "HEAD", "refs/heads/release")
            with self.assertRaisesRegex(AgentSkillUpdaterError, "explicit upstream"):
                probe_git_worktree(source)

    def test_registry_never_uses_local_ahead_head_as_missing_base_or_persists_credentials(self):
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, local, _ = self.create_remote_and_clone(root)
            (local / "local-only.txt").write_text("ahead\n", encoding="utf-8")
            run_git(local, "add", "local-only.txt")
            run_git(local, "commit", "-m", "local ahead")
            run_git(
                local,
                "remote",
                "set-url",
                "origin",
                "https://SECRET_TOKEN@github.com/example/demo.git",
            )

            entry = sync_registry(local.parent)["entries"]["demo"]

            self.assertEqual(entry["installedBaseVersion"], "unknown")
            self.assertIn("installedBaseVersion", entry["metadataError"])
            self.assertIn("metadata fields", entry["metadataError"])
            self.assertIsNone(entry["repoUrl"])
            self.assertNotIn("SECRET_TOKEN", json.dumps(entry))
            self.assertFalse((local / ".openskills.json").exists())

    def test_remote_tracked_control_metadata_is_rejected_before_apply(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError, probe_git_worktree

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, seed, local, version_a = self.create_remote_and_clone(root)
            metadata_path = self.write_metadata(local, seed, version_a)
            original_metadata = metadata_path.read_bytes()
            (seed / ".openskills.json").write_text('{"tracked":true}\n', encoding="utf-8")
            run_git(seed, "add", "-f", ".openskills.json")
            run_git(seed, "commit", "-m", "track forbidden control metadata")
            run_git(seed, "push", "origin", "main")

            with self.assertRaisesRegex(AgentSkillUpdaterError, "tracks updater control entries"):
                probe_git_worktree(self.source_for(local, metadata_path))

            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_a)
            self.assertEqual(metadata_path.read_bytes(), original_metadata)

    def test_branch_change_during_snapshot_cancels_before_apply(self):
        from scripts.agent_skill_updater import (
            AgentSkillUpdaterError,
            _stage_git_revision_payload,
            update_git_worktree_skill,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, seed, local, version_a = self.create_remote_and_clone(root)
            self.advance_remote(seed)
            metadata_path = self.write_metadata(local, seed, version_a)
            source = self.source_for(local, metadata_path)

            def switch_branch_after_snapshot(repo_dir, revision, destination, entry_type):
                _stage_git_revision_payload(
                    repo_dir,
                    revision,
                    destination,
                    entry_type,
                )
                run_git(local, "checkout", "-b", "topic")

            with mock.patch(
                "scripts.agent_skill_updater._stage_git_revision_payload",
                side_effect=switch_branch_after_snapshot,
            ):
                with self.assertRaisesRegex(AgentSkillUpdaterError, "cancelled before apply"):
                    update_git_worktree_skill(source)

            self.assertEqual(run_git(local, "branch", "--show-current"), "topic")
            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_a)
            self.assertEqual(run_git(local, "rev-parse", "main"), version_a)
            self.assertEqual(list(local.parent.glob(".demo.git-update-*")), [])

    def test_interrupted_git_rollback_is_recovered_from_durable_journal(self):
        from scripts.agent_skill_updater import (
            AgentSkillUpdaterError,
            recover_incomplete_skill_transactions,
            update_git_worktree_skill,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, seed, local, version_a = self.create_remote_and_clone(root)
            version_b = self.advance_remote(seed)
            metadata_path = self.write_metadata(local, seed, version_a)
            original_metadata = metadata_path.read_bytes()
            source = self.source_for(local, metadata_path)

            with mock.patch(
                "scripts.agent_skill_updater._commit_transaction_metadata",
                side_effect=PermissionError("metadata failed"),
            ):
                with mock.patch(
                    "scripts.agent_skill_updater._rollback_git_fast_forward",
                    side_effect=KeyboardInterrupt(),
                ):
                    with self.assertRaisesRegex(AgentSkillUpdaterError, "Rollback also failed"):
                        update_git_worktree_skill(source)

            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_b)
            transactions = list(local.parent.glob(".demo.git-update-*"))
            self.assertEqual(len(transactions), 1)

            recover_incomplete_skill_transactions(local.parent)

            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_a)
            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "version-a\n")
            self.assertEqual(metadata_path.read_bytes(), original_metadata)
            self.assertEqual(list(local.parent.glob(".demo.git-update-*")), [])

    def test_committed_git_recovery_accepts_preserved_ignored_payload(self):
        from scripts.agent_skill_updater import (
            AgentSkillUpdaterError,
            recover_incomplete_skill_transactions,
            update_git_worktree_skill,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, seed, local, version_a = self.create_remote_and_clone(root)
            version_b = self.advance_remote(seed)
            metadata_path = self.write_metadata(local, seed, version_a)
            source = self.source_for(local, metadata_path)
            info_exclude = local / ".git" / "info" / "exclude"
            info_exclude.write_text("ignored.txt\n", encoding="utf-8")
            (local / "ignored.txt").write_text("keep me\n", encoding="utf-8")

            with mock.patch(
                "scripts.agent_skill_updater._remove_transaction_tree",
                side_effect=PermissionError("cleanup failed"),
            ):
                with self.assertRaisesRegex(AgentSkillUpdaterError, "committed"):
                    update_git_worktree_skill(source)

            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_b)
            self.assertEqual((local / "ignored.txt").read_text(encoding="utf-8"), "keep me\n")
            self.assertEqual(len(list(local.parent.glob(".demo.git-update-*"))), 1)

            recover_incomplete_skill_transactions(local.parent)

            self.assertEqual(run_git(local, "rev-parse", "HEAD"), version_b)
            self.assertEqual((local / "ignored.txt").read_text(encoding="utf-8"), "keep me\n")
            self.assertEqual(list(local.parent.glob(".demo.git-update-*")), [])


if __name__ == "__main__":
    unittest.main()
