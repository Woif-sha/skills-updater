import json
import io
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
