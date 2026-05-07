import json
import io
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

        source = AgentSkillSource(
            name="openspec-explore",
            local_dir=Path("C:/fake/openspec-explore"),
            source="Fission-AI/OpenSpec",
            source_type="git-generated",
            repo_url="https://github.com/Fission-AI/OpenSpec",
            subpath=None,
            generator="dist/core/shared/skill-generation.js",
            workflow_id="explore",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch("scripts.agent_skill_updater._stage_openspec_generated_skill") as openspec_stage:
                with mock.patch("scripts.agent_skill_updater._stage_git_skill") as git_stage:
                    stage_remote_skill(source, Path(temp_dir))

        openspec_stage.assert_called_once()
        git_stage.assert_not_called()

    def test_download_repo_archive_creates_missing_temp_root(self):
        from scripts.agent_skill_updater import _download_repo_archive

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr("demo-main/SKILL.md", "---\nname: demo\n---\n")

        response = mock.MagicMock()
        response.read.return_value = zip_buffer.getvalue()
        response.__enter__.return_value = response
        response.__exit__.return_value = None

        with tempfile.TemporaryDirectory() as temp_dir:
            missing_root = Path(temp_dir) / "missing" / "stage-root"
            with mock.patch("scripts.agent_skill_updater.urllib.request.urlopen", return_value=response):
                repo_root = _download_repo_archive("owner", "demo", "main", missing_root)

        self.assertTrue(repo_root.name.startswith("demo-main"))

    def test_resolve_command_uses_cmd_suffix_on_windows(self):
        import scripts.agent_skill_updater as updater

        with mock.patch.object(updater.sys, "platform", "win32"):
            with mock.patch("scripts.agent_skill_updater.shutil.which") as which:
                which.side_effect = lambda value: {
                    "npm": None,
                    "npm.cmd": r"C:\Program Files\nodejs\npm.cmd",
                }.get(value)

                resolved = updater._resolve_command(["npm", "ci", "--ignore-scripts"])

        self.assertEqual(resolved[0], r"C:\Program Files\nodejs\npm.cmd")

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
                        "sourceCommitSha": "old123456789",
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
            )
            update = AgentSkillUpdate(
                source=source,
                staged_dir=remote,
                status="update_available",
                local_version="old123456789",
                remote_version="new123456789",
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
                        "sourceCommitSha": "old123456789",
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
            )
            update = AgentSkillUpdate(
                source=source,
                staged_dir=remote,
                status="update_available",
                local_version="old123456789",
                remote_version="new123456789",
            )

            with mock.patch("scripts.agent_skill_updater._stage_git_skill_at_ref", return_value=base):
                with self.assertRaises(AgentSkillUpdaterError):
                    update_skill_from_staged(update, backup_root)

            self.assertEqual((local / "SKILL.md").read_text(encoding="utf-8"), "# Demo\n\nLocal changed line.\n")
            conflict_dir = backup_root / "demo-skill.merge-conflicts"
            self.assertTrue((conflict_dir / "SKILL.md.local").exists())
            self.assertTrue((conflict_dir / "SKILL.md.remote").exists())

    def test_update_agent_skills_skips_local_customized_self_update(self):
        import scripts.update_agent_skills as updater

        registry = {
            "version": 1,
            "generatedAt": "2026-04-11T00:00:00+00:00",
            "skillsRoot": r"C:\Users\sha\.agents\skills",
            "entries": {
                "skills-updater": {
                    "name": "skills-updater",
                    "entryType": "single-skill",
                    "path": r"C:\Users\sha\.agents\skills\skills-updater",
                    "repoUrl": "https://github.com/yizhiyanhua-ai/skills-updater",
                    "source": "yizhiyanhua-ai/skills-updater",
                    "sourceType": "git",
                    "subpath": ".",
                    "localVersion": "abc123def456",
                    "managed": True,
                    "autoUpdate": False,
                }
            },
        }

        stdout = io.StringIO()
        with mock.patch.object(updater.sys, "argv", ["update_agent_skills.py", "--skill", "skills-updater", "--json"]):
            with mock.patch("scripts.update_agent_skills.sync_registry", side_effect=[registry, registry]):
                with mock.patch("scripts.update_agent_skills.save_registry"):
                    with mock.patch("scripts.update_agent_skills._probe_entry", return_value=("up_to_date", "abc123def456", None)):
                        with self.assertRaises(SystemExit) as exit_info:
                            with redirect_stdout(stdout):
                                updater.main()

        self.assertEqual(exit_info.exception.code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["name"], "skills-updater")
        self.assertFalse(payload[0]["applied"])
        self.assertEqual(
            payload[0]["error_message"],
            "Auto-update disabled for this locally customized skill.",
        )

    def test_update_agent_skills_updates_only_requested_registry_entry(self):
        import scripts.update_agent_skills as updater

        registry = {
            "version": 1,
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
                    "localVersion": "old123456789",
                    "managed": True,
                    "autoUpdate": True,
                },
                "other-skill": {
                    "name": "other-skill",
                    "entryType": "single-skill",
                    "path": r"C:\Users\sha\.agents\skills\other-skill",
                    "repoUrl": "https://github.com/example/other-skill",
                    "source": "example/other-skill",
                    "sourceType": "git",
                    "subpath": ".",
                    "localVersion": "same12345678",
                    "managed": True,
                    "autoUpdate": True,
                },
            },
        }

        stdout = io.StringIO()
        resolved = SimpleNamespace(status="update_available", error_message=None)
        with mock.patch.object(updater.sys, "argv", ["update_agent_skills.py", "--skill", "demo-skill", "--json"]):
            with mock.patch("scripts.update_agent_skills.sync_registry", side_effect=[registry, registry]):
                with mock.patch("scripts.update_agent_skills.save_registry"):
                    with mock.patch("scripts.update_agent_skills._probe_entry", return_value=("update_available", "new123456789", None)):
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
            "version": 1,
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
                    "localVersion": "old123456789",
                    "managed": True,
                    "autoUpdate": True,
                },
            },
        }

        source = SimpleNamespace(name="demo-skill")
        resolved = SimpleNamespace(status="up_to_date", error_message=None, source=source)
        stdout = io.StringIO()
        with mock.patch.object(updater.sys, "argv", ["update_agent_skills.py", "--skill", "demo-skill", "--json"]):
            with mock.patch("scripts.update_agent_skills.sync_registry", side_effect=[registry, registry]):
                with mock.patch("scripts.update_agent_skills.save_registry"):
                    with mock.patch("scripts.update_agent_skills._probe_entry", return_value=("update_available", "new123456789", None)):
                        with mock.patch("scripts.update_agent_skills.resolve_skill_update", return_value=resolved):
                            with mock.patch("scripts.update_agent_skills.refresh_skill_metadata_version") as refresh_metadata:
                                with self.assertRaises(SystemExit) as exit_info:
                                    with redirect_stdout(stdout):
                                        updater.main()

        self.assertEqual(exit_info.exception.code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload[0]["status"], "up_to_date")
        self.assertTrue(payload[0]["applied"])
        refresh_metadata.assert_called_once()

    def test_install_agent_skill_uses_agent_skills_root_and_rewrites_registry(self):
        import scripts.install_agent_skill as installer
        from scripts.skills_registry import sync_registry as real_sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / ".agents" / "skills"
            skills_root.mkdir(parents=True)

            def fake_stage_remote_skill(source, stage_dir):
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
                        with mock.patch("scripts.install_agent_skill.fetch_remote_commit_sha", return_value="abc123def456"):
                            with mock.patch(
                                "scripts.install_agent_skill.sync_registry",
                                side_effect=lambda: real_sync_registry(skills_root),
                            ):
                                with redirect_stdout(stdout):
                                    installer.main()

            destination = skills_root / "demo-skill"
            self.assertTrue(destination.exists())
            self.assertTrue((destination / "SKILL.md").exists())

            metadata = json.loads((destination / ".openskills.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["repoUrl"], "https://github.com/example/demo-skill")
            self.assertEqual(metadata["sourceCommitSha"], "abc123def456")

            registry = json.loads((skills_root / ".skills-list.json").read_text(encoding="utf-8"))
            self.assertIn("demo-skill", registry["entries"])
            self.assertEqual(registry["entries"]["demo-skill"]["path"], str(destination))

            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["path"], str(destination))
            self.assertEqual(payload["registry"], str(skills_root / ".skills-list.json"))


if __name__ == "__main__":
    unittest.main()
