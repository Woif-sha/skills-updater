import json
import io
import stat
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class AgentSkillUpdaterTests(unittest.TestCase):
    def test_load_agent_skill_source_parses_git_generated_metadata(self):
        from scripts.agent_skill_updater import load_agent_skill_source

        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "openspec-explore"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: openspec-explore\n---\n", encoding="utf-8")
            (skill_dir / ".openskills.json").write_text(
                json.dumps(
                    {
                        "source": "Fission-AI/OpenSpec",
                        "sourceType": "git-generated",
                        "repoUrl": "https://github.com/Fission-AI/OpenSpec",
                        "generator": "dist/core/shared/skill-generation.js",
                        "workflowId": "explore",
                    }
                ),
                encoding="utf-8",
            )

            source = load_agent_skill_source(skill_dir)

            self.assertEqual(source.name, "openspec-explore")
            self.assertEqual(source.source_type, "git-generated")
            self.assertEqual(source.workflow_id, "explore")

    def test_directory_signature_ignores_openskills_metadata(self):
        from scripts.agent_skill_updater import directory_signature

        with tempfile.TemporaryDirectory() as temp_dir:
            left = Path(temp_dir) / "left"
            right = Path(temp_dir) / "right"
            left.mkdir()
            right.mkdir()

            (left / "SKILL.md").write_text("same content\n", encoding="utf-8")
            (right / "SKILL.md").write_text("same content\n", encoding="utf-8")
            (left / ".openskills.json").write_text('{"installedAt":"1"}', encoding="utf-8")
            (right / ".openskills.json").write_text('{"installedAt":"2"}', encoding="utf-8")

            self.assertEqual(directory_signature(left), directory_signature(right))

    def test_stage_remote_skill_uses_openspec_generator_for_git_generated_source(self):
        from scripts.agent_skill_updater import AgentSkillSource, stage_remote_skill

        with tempfile.TemporaryDirectory() as temp_dir:
            local_dir = Path(temp_dir) / "skills" / "openspec-explore"
            local_dir.parent.mkdir()
            source = AgentSkillSource(
                name="openspec-explore",
                local_dir=local_dir,
                source="Fission-AI/OpenSpec",
                source_type="git-generated",
                repo_url="https://github.com/Fission-AI/OpenSpec",
                subpath=".",
                generator="dist/core/shared/skill-generation.js",
                workflow_id="explore",
                entry_type="single-skill",
            )
            with mock.patch("scripts.agent_skill_updater._stage_openspec_generated_skill") as openspec_stage:
                with mock.patch("scripts.agent_skill_updater._stage_git_skill_at_ref") as git_stage:
                    stage_remote_skill(source, Path(temp_dir))

        openspec_stage.assert_called_once()
        git_stage.assert_not_called()

    def test_stage_remote_skill_requires_explicit_git_commit(self):
        from scripts.agent_skill_updater import (
            AgentSkillSource,
            AgentSkillUpdaterError,
            stage_remote_skill,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = AgentSkillSource(
                name="demo",
                local_dir=root / "demo",
                source="owner/demo",
                source_type="git",
                repo_url="https://github.com/owner/demo",
                subpath=".",
                generator=None,
                workflow_id=None,
                entry_type="single-skill",
            )
            with mock.patch("scripts.agent_skill_updater._stage_git_skill_at_ref") as stage_at_ref:
                with self.assertRaisesRegex(AgentSkillUpdaterError, "explicit remote commit"):
                    stage_remote_skill(source, root / "stage")

        stage_at_ref.assert_not_called()

    def test_download_repo_archive_creates_missing_temp_root(self):
        from scripts.agent_skill_updater import _download_repo_archive

        commit = "a" * 40
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr(f"demo-{commit}/SKILL.md", "---\nname: demo\n---\n")

        response = mock.MagicMock()
        response.read.return_value = zip_buffer.getvalue()
        response.__enter__.return_value = response
        response.__exit__.return_value = None

        with tempfile.TemporaryDirectory() as temp_dir:
            missing_root = Path(temp_dir) / "missing" / "stage-root"
            with mock.patch("scripts.agent_skill_updater.urllib.request.urlopen", return_value=response):
                repo_root = _download_repo_archive("owner", "demo", commit, missing_root)

        self.assertEqual(repo_root.name, f"demo-{commit}")

    def test_download_repo_archive_rejects_path_traversal(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError, _download_repo_archive

        commit = "a" * 40
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr(f"demo-{commit}/SKILL.md", "---\nname: demo\n---\n")
            archive.writestr(f"demo-{commit}/../outside.txt", "unsafe\n")

        response = mock.MagicMock()
        response.read.return_value = zip_buffer.getvalue()
        response.__enter__.return_value = response
        response.__exit__.return_value = None

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch("scripts.agent_skill_updater.urllib.request.urlopen", return_value=response):
                with self.assertRaisesRegex(AgentSkillUpdaterError, "unsafe path"):
                    _download_repo_archive("owner", "demo", commit, Path(temp_dir))

    def test_stage_remote_skill_ignores_symlink_outside_selected_payload(self):
        from scripts.agent_skill_updater import AgentSkillSource, stage_remote_skill

        commit = "a" * 40
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr(
                f"demo-{commit}/skills/selected/SKILL.md",
                "---\nname: selected\n---\n",
            )
            symlink = zipfile.ZipInfo(f"demo-{commit}/AGENTS.md")
            symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(symlink, "CLAUDE.md")

        response = mock.MagicMock()
        response.read.return_value = zip_buffer.getvalue()
        response.__enter__.return_value = response
        response.__exit__.return_value = None

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local_dir = root / "installed" / "selected"
            local_dir.parent.mkdir()
            source = AgentSkillSource(
                name="selected",
                local_dir=local_dir,
                source="owner/demo",
                source_type="git",
                repo_url="https://github.com/owner/demo",
                subpath="skills/selected",
                generator=None,
                workflow_id=None,
                entry_type="single-skill",
            )
            with mock.patch("scripts.agent_skill_updater.urllib.request.urlopen", return_value=response):
                staged = stage_remote_skill(source, root / "stage", remote_version=commit)

            self.assertTrue((staged / "SKILL.md").is_file())
            self.assertFalse((staged / "AGENTS.md").exists())

    def test_download_repo_archive_rejects_symlink_inside_selected_payload(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError, _download_repo_archive

        commit = "a" * 40
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr(
                f"demo-{commit}/skills/selected/SKILL.md",
                "---\nname: selected\n---\n",
            )
            symlink = zipfile.ZipInfo(f"demo-{commit}/skills/selected/rules.md")
            symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(symlink, "../../shared/rules.md")

        response = mock.MagicMock()
        response.read.return_value = zip_buffer.getvalue()
        response.__enter__.return_value = response
        response.__exit__.return_value = None

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch("scripts.agent_skill_updater.urllib.request.urlopen", return_value=response):
                with self.assertRaisesRegex(AgentSkillUpdaterError, "symbolic link"):
                    _download_repo_archive(
                        "owner",
                        "demo",
                        commit,
                        Path(temp_dir),
                        payload_subpath="skills/selected",
                    )

    def test_resolve_command_uses_exact_path_resolution_on_windows(self):
        import scripts.agent_skill_updater as updater

        with mock.patch.object(updater.sys, "platform", "win32"):
            with mock.patch(
                "scripts.agent_skill_updater.shutil.which",
                return_value=r"C:\Program Files\nodejs\npm.cmd",
            ) as which:
                resolved = updater._resolve_command(["npm", "ci", "--ignore-scripts"])

        which.assert_called_once_with("npm")
        self.assertEqual(resolved[0], r"C:\Program Files\nodejs\npm.cmd")

    def test_resolve_command_rejects_missing_windows_executable(self):
        import scripts.agent_skill_updater as updater

        with mock.patch.object(updater.sys, "platform", "win32"):
            with mock.patch("scripts.agent_skill_updater.shutil.which", return_value=None):
                with self.assertRaisesRegex(updater.AgentSkillUpdaterError, "not available on PATH"):
                    updater._resolve_command(["npm", "ci"])

    def test_update_skill_from_staged_merges_local_and_remote_text_changes(self):
        from scripts.agent_skill_updater import AgentSkillSource, AgentSkillUpdate, update_skill_from_staged

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "local" / "demo-skill"
            remote = root / "remote"
            base = root / "base"
            backup_root = root / "backup"
            local.mkdir(parents=True)
            remote.mkdir()
            base.mkdir()
            backup_root.mkdir()

            base_text = "# Demo\n\n## Remote Section\n\nOriginal remote line.\n\n## Local Section\n\nOriginal local line.\n"
            local_text = "# Demo\n\n## Remote Section\n\nOriginal remote line.\n\n## Local Section\n\nKeep this local rule.\n"
            remote_text = "# Demo\n\n## Remote Section\n\nRemote changed line.\n\n## Local Section\n\nOriginal local line.\n"

            (base / "SKILL.md").write_text(base_text, encoding="utf-8")
            (local / "SKILL.md").write_text(local_text, encoding="utf-8")
            (remote / "SKILL.md").write_text(remote_text, encoding="utf-8")
            (local / ".openskills.json").write_text(
                json.dumps(
                    {
                        "source": "example/demo-skill",
                        "sourceType": "git",
                        "repoUrl": "https://github.com/example/demo-skill",
                        "subpath": ".",
                        "installedBaseVersion": "a" * 40,
                    }
                ),
                encoding="utf-8",
            )

            source = AgentSkillSource(
                name="demo-skill",
                local_dir=local,
                source="example/demo-skill",
                source_type="git",
                repo_url="https://github.com/example/demo-skill",
                subpath=".",
                generator=None,
                workflow_id=None,
                metadata_path=local / ".openskills.json",
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

            with mock.patch("scripts.agent_skill_updater._stage_git_skill_at_ref", return_value=base):
                update_skill_from_staged(update, backup_root)

            merged = (local / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("Remote changed line.", merged)
            self.assertIn("Keep this local rule.", merged)
            self.assertTrue((backup_root / "demo-skill" / "SKILL.md").exists())

    def test_update_skill_from_staged_blocks_conflicting_local_and_remote_changes(self):
        from scripts.agent_skill_updater import (
            AgentSkillSource,
            AgentSkillUpdate,
            AgentSkillUpdaterError,
            update_skill_from_staged,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "local" / "demo-skill"
            remote = root / "remote"
            base = root / "base"
            backup_root = root / "backup"
            local.mkdir(parents=True)
            remote.mkdir()
            base.mkdir()
            backup_root.mkdir()

            (base / "SKILL.md").write_text("# Demo\n\nOriginal line.\n", encoding="utf-8")
            (local / "SKILL.md").write_text("# Demo\n\nLocal changed line.\n", encoding="utf-8")
            (remote / "SKILL.md").write_text("# Demo\n\nRemote changed line.\n", encoding="utf-8")
            (local / ".openskills.json").write_text(
                json.dumps(
                    {
                        "source": "example/demo-skill",
                        "sourceType": "git",
                        "repoUrl": "https://github.com/example/demo-skill",
                        "subpath": ".",
                        "installedBaseVersion": "a" * 40,
                    }
                ),
                encoding="utf-8",
            )
            source = AgentSkillSource(
                name="demo-skill",
                local_dir=local,
                source="example/demo-skill",
                source_type="git",
                repo_url="https://github.com/example/demo-skill",
                subpath=".",
                generator=None,
                workflow_id=None,
                metadata_path=local / ".openskills.json",
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

            with mock.patch("scripts.agent_skill_updater._stage_git_skill_at_ref", return_value=base):
                with self.assertRaises(AgentSkillUpdaterError):
                    update_skill_from_staged(update, backup_root)

            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "# Demo\n\nLocal changed line.\n")
            conflict_dir = backup_root / "demo-skill.merge-conflicts"
            self.assertTrue((conflict_dir / "SKILL.md.local").exists())
            self.assertTrue((conflict_dir / "SKILL.md.remote").exists())

    def test_update_agent_skills_updates_only_requested_registry_entry(self):
        import scripts.update_agent_skills as updater

        registry = {
            "version": 2,
            "generatedAt": "2026-04-11T00:00:00+00:00",
            "skillsRoot": r"C:\Users\sha\.agents\skills",
            "entries": {
                "demo-skill": {
                    "name": "demo-skill",
                    "entryType": "single-skill",
                    "path": r"C:\Users\sha\.agents\skills\demo-skill",
                    "repoUrl": "https://github.com/example/demo-skill",
                    "source": "example/demo-skill",
                    "sourceType": "git",
                    "subpath": ".",
                    "updateMode": "snapshot",
                    "installedBaseVersion": "old123456789",
                    "localVersion": "old123456789",
                    "managed": True,
                },
                "other-skill": {
                    "name": "other-skill",
                    "entryType": "single-skill",
                    "path": r"C:\Users\sha\.agents\skills\other-skill",
                    "repoUrl": "https://github.com/example/other-skill",
                    "source": "example/other-skill",
                    "sourceType": "git",
                    "subpath": ".",
                    "updateMode": "snapshot",
                    "installedBaseVersion": "same12345678",
                    "localVersion": "same12345678",
                    "managed": True,
                },
            },
        }

        stdout = io.StringIO()
        resolved = SimpleNamespace(
            status="update_available",
            error_message=None,
            installed_base_version="old123456789",
            local_version="old123456789",
            remote_version="new123456789",
        )
        with mock.patch.object(updater.sys, "argv", ["update_agent_skills.py", "--skill", "demo-skill", "--json"]):
            with mock.patch("scripts.update_agent_skills.sync_registry", side_effect=[registry, registry]):
                with mock.patch("scripts.update_agent_skills.update_registry_entries"):
                    with mock.patch(
                        "scripts.update_agent_skills._probe_entry",
                        return_value=updater.EntryProbe("update_available", "old123456789", "new123456789"),
                    ):
                        with mock.patch("scripts.update_agent_skills.resolve_skill_update", return_value=resolved):
                            with mock.patch("scripts.update_agent_skills.make_backup_root", return_value=Path(r"C:\backup-root")):
                                with mock.patch("scripts.update_agent_skills.update_skill_from_staged") as update_from_staged:
                                    with self.assertRaises(SystemExit) as exit_info:
                                        with redirect_stdout(stdout):
                                            updater.main()

        self.assertEqual(exit_info.exception.code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["name"], "demo-skill")
        self.assertTrue(payload[0]["applied"])
        update_from_staged.assert_called_once()

    def test_update_agent_skills_refreshes_metadata_when_staged_content_matches(self):
        import scripts.update_agent_skills as updater

        registry = {
            "version": 2,
            "generatedAt": "2026-04-11T00:00:00+00:00",
            "skillsRoot": r"C:\Users\sha\.agents\skills",
            "entries": {
                "demo-skill": {
                    "name": "demo-skill",
                    "entryType": "single-skill",
                    "path": r"C:\Users\sha\.agents\skills\demo-skill",
                    "repoUrl": "https://github.com/example/demo-skill",
                    "source": "example/demo-skill",
                    "sourceType": "git",
                    "subpath": ".",
                    "updateMode": "snapshot",
                    "installedBaseVersion": "old123456789",
                    "localVersion": "old123456789",
                    "managed": True,
                },
            },
        }

        source = SimpleNamespace(name="demo-skill")
        resolved = SimpleNamespace(
            status="up_to_date",
            error_message=None,
            source=source,
            installed_base_version="old123456789",
            local_version="old123456789",
            remote_version="new123456789",
        )
        stdout = io.StringIO()
        with mock.patch.object(updater.sys, "argv", ["update_agent_skills.py", "--skill", "demo-skill", "--json"]):
            with mock.patch("scripts.update_agent_skills.sync_registry", side_effect=[registry, registry]):
                with mock.patch("scripts.update_agent_skills.update_registry_entries"):
                    with mock.patch(
                        "scripts.update_agent_skills._probe_entry",
                        return_value=updater.EntryProbe("update_available", "old123456789", "new123456789"),
                    ):
                        with mock.patch("scripts.update_agent_skills.resolve_skill_update", return_value=resolved):
                            with mock.patch(
                                "scripts.update_agent_skills.refresh_skill_metadata_version",
                                return_value=True,
                            ) as refresh_metadata:
                                with self.assertRaises(SystemExit) as exit_info:
                                    with redirect_stdout(stdout):
                                        updater.main()

        self.assertEqual(exit_info.exception.code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["status"], "up_to_date")
        self.assertTrue(payload[0]["applied"])
        refresh_metadata.assert_called_once()

    def test_update_agent_skills_routes_skill_pack_through_transaction_engine(self):
        import scripts.update_agent_skills as updater

        version_a = "a" * 40
        version_b = "b" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir)
            pack_dir = skills_root / "demo-pack"
            (pack_dir / ".git").mkdir(parents=True)
            (pack_dir / "skills").mkdir()
            entry = {
                "name": "demo-pack",
                "entryType": "skill-pack",
                "path": str(pack_dir),
                "repoUrl": "https://github.com/example/demo-pack",
                "source": "example/demo-pack",
                "sourceType": "git-pack",
                "subpath": ".",
                "updateMode": "git-worktree",
                "installedBaseVersion": version_a,
                "localVersion": version_a,
                "managed": True,
            }
            registry = {
                "version": 2,
                "generatedAt": "2026-04-11T00:00:00+00:00",
                "skillsRoot": str(skills_root),
                "entries": {"demo-pack": entry},
            }
            result = SimpleNamespace(
                status="up_to_date",
                local_version=version_b,
                remote_version=version_b,
                relation="equal",
                working_tree_dirty=False,
                error_message=None,
                applied=True,
                action="fast_forwarded",
            )
            stdout = io.StringIO()
            with mock.patch.object(
                updater.sys,
                "argv",
                ["update_agent_skills.py", "--skill", "demo-pack", "--json"],
            ):
                with mock.patch("scripts.update_agent_skills.sync_registry", side_effect=[registry, registry]):
                    with mock.patch("scripts.update_agent_skills.update_registry_entries"):
                        with mock.patch(
                            "scripts.update_agent_skills._probe_entry",
                            return_value=updater.EntryProbe(
                                "update_available",
                                version_a,
                                version_b,
                                git_relation="behind",
                                working_tree_dirty=False,
                            ),
                        ):
                            with mock.patch(
                                "scripts.update_agent_skills.update_git_worktree_skill",
                                return_value=result,
                            ) as git_update:
                                with self.assertRaises(SystemExit) as exit_info:
                                    with redirect_stdout(stdout):
                                        updater.main()

        self.assertEqual(exit_info.exception.code, 0)
        git_update.assert_called_once()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["action"], "fast_forwarded")
        self.assertEqual(payload[0]["installed_base_version"], version_b)

    def test_install_agent_skill_uses_agent_skills_root_and_rewrites_registry(self):
        import scripts.install_agent_skill as installer
        from scripts.skills_registry import sync_registry as real_sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / ".agents" / "skills"
            skills_root.mkdir(parents=True)

            def fake_stage_remote_skill(source, stage_dir, remote_version=None):
                self.assertEqual(remote_version, "abc123def456")
                stage_dir.mkdir(parents=True, exist_ok=True)
                (stage_dir / "SKILL.md").write_text("---\nname: demo-skill\n---\n", encoding="utf-8")
                return stage_dir

            stdout = io.StringIO()
            with mock.patch.object(
                installer.sys,
                "argv",
                ["install_agent_skill.py", "--repo", "example/demo-skill", "--name", "demo-skill", "--json"],
            ):
                with mock.patch("scripts.install_agent_skill.get_agent_skills_dir", return_value=skills_root):
                    with mock.patch("scripts.install_agent_skill.stage_remote_skill", side_effect=fake_stage_remote_skill):
                        with mock.patch(
                            "scripts.install_agent_skill.fetch_remote_commit_sha",
                            return_value="abc123def456",
                        ) as fetch_commit:
                            with mock.patch(
                                "scripts.install_agent_skill.sync_registry",
                                side_effect=lambda: real_sync_registry(skills_root),
                            ):
                                with redirect_stdout(stdout):
                                    installer.main()

            fetch_commit.assert_called_once_with("https://github.com/example/demo-skill")

            destination = skills_root / "demo-skill"
            self.assertTrue(destination.exists())
            self.assertTrue((destination / "SKILL.md").exists())

            metadata = json.loads((destination / ".openskills.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["repoUrl"], "https://github.com/example/demo-skill")
            self.assertEqual(metadata["installedBaseVersion"], "abc123def456")
            self.assertNotIn("sourceCommitSha", metadata)

            registry = json.loads((skills_root / ".skills-list.json").read_text(encoding="utf-8"))
            self.assertIn("demo-skill", registry["entries"])
            self.assertEqual(registry["entries"]["demo-skill"]["path"], str(destination))

            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["path"], str(destination))
            self.assertEqual(payload["registry"], str(skills_root / ".skills-list.json"))

    def test_skill_pack_install_writes_complete_metadata_and_registers_as_managed(self):
        import scripts.install_agent_skill as installer
        from scripts.skills_registry import sync_registry

        version = "a" * 40
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skills_root.mkdir()
            destination = skills_root / "demo-pack"

            def fake_clone(_repo_url, payload):
                (payload / ".git").mkdir(parents=True)
                skill_dir = payload / "skills" / "demo"
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")

            with mock.patch("scripts.install_agent_skill.git_clone_repo", side_effect=fake_clone):
                with mock.patch("scripts.install_agent_skill._git_output", return_value=version):
                    with mock.patch("scripts.install_agent_skill._git_tracked_control_paths", return_value=[]):
                        installer._install_atomically(
                            destination=destination,
                            repo_url="https://github.com/example/demo-pack",
                            subpath=".",
                            entry_type="skill-pack",
                            source_type="git",
                            workflow_id=None,
                        )

            metadata = json.loads((destination / ".openskills.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["sourceType"], "git-pack")
            self.assertEqual(metadata["subpath"], ".")
            self.assertEqual(metadata["installedBaseVersion"], version)

            with mock.patch(
                "scripts.skills_registry.get_git_remote_url",
                return_value="https://github.com/example/demo-pack",
            ):
                with mock.patch("scripts.skills_registry.get_git_head_commit", return_value=version):
                    entry = sync_registry(skills_root)["entries"]["demo-pack"]
            self.assertTrue(entry["managed"])
            self.assertEqual(entry["installedBaseVersion"], version)

    def test_skill_pack_install_rejects_ignored_options_explicitly(self):
        import scripts.install_agent_skill as installer

        invalid_options = (
            {"source_type": "git", "subpath": "nested", "workflow_id": None},
            {"source_type": "git-generated", "subpath": ".", "workflow_id": None},
            {"source_type": "git", "subpath": ".", "workflow_id": "workflow"},
        )
        for options in invalid_options:
            with self.subTest(options=options):
                with self.assertRaises(installer.AgentSkillUpdaterError):
                    installer._validate_install_options(
                        entry_type="skill-pack",
                        repo_url="https://github.com/example/demo-pack",
                        **options,
                    )

    def test_install_rejects_escaping_name_and_subpath_before_network(self):
        import scripts.install_agent_skill as installer

        for arguments in (
            ["--repo", "example/demo", "--name", "../outside", "--json"],
            ["--repo", "example/demo", "--name", "demo", "--path", "../outside", "--json"],
            ["--repo", "example/demo", "--name", "demo", "--path", "C:\\outside", "--json"],
        ):
            with self.subTest(arguments=arguments):
                with tempfile.TemporaryDirectory() as temp_dir:
                    skills_root = Path(temp_dir) / "skills"
                    stdout = io.StringIO()
                    with mock.patch.object(installer.sys, "argv", ["install_agent_skill.py", *arguments]):
                        with mock.patch.object(
                            installer,
                            "get_agent_skills_dir",
                            return_value=skills_root,
                        ):
                            with mock.patch.object(
                                installer,
                                "fetch_remote_commit_sha",
                                side_effect=AssertionError("network access occurred"),
                            ):
                                with redirect_stdout(stdout):
                                    with self.assertRaises(SystemExit) as exit_context:
                                        installer._run_cli()
                    self.assertEqual(exit_context.exception.code, 1)
                    self.assertFalse((Path(temp_dir) / "outside").exists())
                    self.assertFalse((skills_root / "demo").exists())
                    self.assertFalse(json.loads(stdout.getvalue())[0]["installed"])

    def test_atomic_install_failure_leaves_no_destination_or_staging_directory(self):
        import scripts.install_agent_skill as installer

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skills_root.mkdir()
            destination = skills_root / "demo"

            def fake_stage(_source, stage_root, _remote_version):
                staged = stage_root / "demo"
                staged.mkdir(parents=True)
                (staged / "SKILL.md").write_text("demo\n", encoding="utf-8")
                return staged

            with mock.patch.object(installer, "fetch_remote_commit_sha", return_value="a" * 40):
                with mock.patch.object(installer, "stage_remote_skill", side_effect=fake_stage):
                    with mock.patch.object(
                        installer,
                        "_write_skill_metadata",
                        side_effect=PermissionError("metadata write failed"),
                    ):
                        with self.assertRaisesRegex(PermissionError, "metadata write failed"):
                            installer._install_atomically(
                                destination=destination,
                                repo_url="https://github.com/example/demo",
                                subpath=".",
                                entry_type="single-skill",
                                source_type="git",
                                workflow_id=None,
                            )

            self.assertFalse(destination.exists())
            self.assertEqual(list(skills_root.glob(".demo.install-*")), [])

    def test_install_interrupt_after_rename_reports_committed_destination(self):
        import inspect
        import sys

        import scripts.install_agent_skill as installer

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skills_root.mkdir()
            destination = skills_root / "demo"

            def fake_stage(_source, stage_root, _remote_version):
                staged = stage_root / "demo"
                staged.mkdir(parents=True)
                (staged / "SKILL.md").write_text("demo\n", encoding="utf-8")
                return staged

            source_lines, first_line = inspect.getsourcelines(installer._install_atomically)
            commit_line = first_line + next(
                index for index, line in enumerate(source_lines) if line.strip() == "committed = True"
            )

            def interrupt_before_commit_assignment(frame, event, _argument):
                if (
                    frame.f_code is installer._install_atomically.__code__
                    and event == "line"
                    and frame.f_lineno == commit_line
                ):
                    raise KeyboardInterrupt("interrupt after rename")
                return interrupt_before_commit_assignment

            previous_trace = sys.gettrace()
            with mock.patch.object(installer, "fetch_remote_commit_sha", return_value="a" * 40):
                with mock.patch.object(installer, "stage_remote_skill", side_effect=fake_stage):
                    sys.settrace(interrupt_before_commit_assignment)
                    try:
                        with self.assertRaises(installer.AgentSkillInstallError) as error:
                            installer._install_atomically(
                                destination=destination,
                                repo_url="https://github.com/example/demo",
                                subpath=".",
                                entry_type="single-skill",
                                source_type="git",
                                workflow_id=None,
                            )
                    finally:
                        sys.settrace(previous_trace)

            self.assertTrue(error.exception.installed)
            self.assertEqual(error.exception.path, destination)
            self.assertTrue((destination / "SKILL.md").is_file())
            self.assertEqual(list(skills_root.glob(".demo.install-*")), [])

    def test_install_json_reports_committed_directory_when_registry_refresh_fails(self):
        import scripts.install_agent_skill as installer

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"

            def fake_stage(_source, stage_root, _remote_version):
                staged = stage_root / "demo"
                staged.mkdir(parents=True)
                (staged / "SKILL.md").write_text("demo\n", encoding="utf-8")
                return staged

            stdout = io.StringIO()
            with mock.patch.object(
                installer.sys,
                "argv",
                [
                    "install_agent_skill.py",
                    "--repo",
                    "example/demo",
                    "--name",
                    "demo",
                    "--json",
                ],
            ):
                with mock.patch.object(installer, "get_agent_skills_dir", return_value=skills_root):
                    with mock.patch.object(installer, "fetch_remote_commit_sha", return_value="a" * 40):
                        with mock.patch.object(installer, "stage_remote_skill", side_effect=fake_stage):
                            with mock.patch.object(
                                installer,
                                "sync_registry",
                                side_effect=installer.AgentSkillUpdaterError("registry unavailable"),
                            ):
                                with redirect_stdout(stdout):
                                    with self.assertRaises(SystemExit) as exit_context:
                                        installer._run_cli()

            self.assertEqual(exit_context.exception.code, 1)
            payload = json.loads(stdout.getvalue())[0]
            self.assertTrue(payload["installed"])
            self.assertEqual(payload["path"], str(skills_root / "demo"))
            self.assertTrue((skills_root / "demo" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
