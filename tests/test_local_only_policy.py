import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


class LocalOnlyPolicyTests(unittest.TestCase):
    def test_single_skill_policy_skips_git_origin_and_clears_remote_state(self):
        from scripts.skills_registry import REGISTRY_FILENAME, sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir)
            skill_dir = skills_root / "mine"
            skill_dir.mkdir()
            (skill_dir / ".git").mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: mine\n---\n", encoding="utf-8")
            (skill_dir / ".openskills.json").write_text(
                json.dumps(
                    {
                        "updatePolicy": "local-only",
                        "source": "me/mine",
                        "sourceType": "git",
                        "repoUrl": "https://github.com/me/mine",
                    }
                ),
                encoding="utf-8",
            )
            (skills_root / REGISTRY_FILENAME).write_text(
                json.dumps(
                    {
                        "version": 2,
                        "entries": {
                            "mine": {
                                "name": "mine",
                                "source": "me/mine",
                                "sourceType": "git",
                                "repoUrl": "https://github.com/me/mine",
                                "remoteVersion": "a" * 40,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch(
                "scripts.skills_registry.get_git_remote_url",
                side_effect=AssertionError("local-only must not inspect Git origin"),
            ):
                with mock.patch(
                    "scripts.skills_registry.get_git_head_commit",
                    return_value="b" * 40,
                ):
                    registry = sync_registry(skills_root)

        entry = registry["entries"]["mine"]
        self.assertFalse(entry["managed"])
        self.assertNotIn("autoUpdate", entry)
        self.assertEqual(entry["updateMode"], "local-only")
        self.assertEqual(entry["updatePolicy"], "local-only")
        self.assertEqual(entry["sourceType"], "git")
        self.assertIsNone(entry["remoteVersion"])

    def test_skill_pack_policy_takes_precedence_over_remote_discovery(self):
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir)
            pack_dir = skills_root / "my-pack"
            (pack_dir / ".git").mkdir(parents=True)
            (pack_dir / "skills").mkdir()
            (pack_dir / ".openskills.json").write_text(
                json.dumps(
                    {
                        "updatePolicy": "local-only",
                        "source": "me/my-pack",
                        "sourceType": "git-pack",
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch(
                "scripts.skills_registry.get_git_remote_url",
                side_effect=AssertionError("local-only pack must not inspect Git origin"),
            ):
                with mock.patch(
                    "scripts.skills_registry.get_git_head_commit",
                    return_value="c" * 40,
                ):
                    registry = sync_registry(skills_root)

        entry = registry["entries"]["my-pack"]
        self.assertEqual(entry["entryType"], "skill-pack")
        self.assertEqual(entry["updateMode"], "local-only")
        self.assertFalse(entry["managed"])
        self.assertNotIn("autoUpdate", entry)
        self.assertIsNone(entry["remoteVersion"])

    def test_check_and_update_probes_never_call_remote_for_local_only(self):
        from scripts.check_updates import UpdateStatus, _entry_to_skill_info
        from scripts.update_agent_skills import _probe_entry

        entry = self.local_only_entry()
        with mock.patch(
            "scripts.check_updates.fetch_source_remote_version",
            side_effect=AssertionError("remote commit probe called"),
        ):
            with mock.patch(
                "scripts.check_updates.probe_git_worktree",
                side_effect=AssertionError("Git branch probe called"),
            ):
                info = _entry_to_skill_info(entry)
        with mock.patch(
            "scripts.update_agent_skills.fetch_source_remote_observation",
            side_effect=AssertionError("remote commit probe called"),
        ):
            with mock.patch(
                "scripts.update_agent_skills.probe_git_worktree",
                side_effect=AssertionError("Git branch probe called"),
            ):
                probe = _probe_entry(entry)

        self.assertEqual(info.status, UpdateStatus.LOCAL_ONLY)
        self.assertEqual(probe.status, "local_only")
        self.assertIsNone(info.remote_version)
        self.assertIsNone(probe.remote_version)

    def test_low_level_update_apis_enforce_local_only_policy(self):
        from scripts.agent_skill_updater import (
            AgentSkillUpdate,
            AgentSkillUpdaterError,
            load_agent_skill_source,
            probe_git_worktree,
            refresh_skill_metadata_version,
            resolve_skill_update,
            stage_remote_skill,
            update_git_worktree_skill,
            update_skill_from_staged,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "mine"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("mine\n", encoding="utf-8")
            metadata_path = skill_dir / ".openskills.json"
            metadata_path.write_text(
                json.dumps({"updatePolicy": "local-only"}),
                encoding="utf-8",
            )
            source = load_agent_skill_source(skill_dir)

            with mock.patch(
                "scripts.agent_skill_updater._read_installed_base_version",
                side_effect=AssertionError("install baseline read"),
            ):
                with mock.patch(
                    "scripts.agent_skill_updater.fetch_source_remote_version",
                    side_effect=AssertionError("remote commit probe called"),
                ):
                    result = resolve_skill_update(source, root / "stage")

            self.assertEqual(result.status, "local_only")
            self.assertIsNone(result.staged_dir)
            self.assertEqual(result.installed_base_version, "local")
            self.assertEqual(result.local_version, "local")
            for operation in (
                lambda: stage_remote_skill(source, root / "stage-2"),
                lambda: probe_git_worktree(source),
                lambda: update_git_worktree_skill(source),
                lambda: refresh_skill_metadata_version(source, "a" * 40, "b" * 40),
                lambda: update_skill_from_staged(
                    AgentSkillUpdate(
                        source=source,
                        staged_dir=root / "incoming",
                        status="update_available",
                        installed_base_version="a" * 40,
                        local_version="a" * 40,
                        remote_version="b" * 40,
                    ),
                    root / "backup",
                ),
            ):
                with self.assertRaisesRegex(AgentSkillUpdaterError, "local-only"):
                    operation()

    def test_json_clis_report_local_only_without_remote_access(self):
        import scripts.check_updates as check_updates
        import scripts.update_agent_skills as update_agent_skills

        entry = self.local_only_entry()
        registry = {
            "version": 2,
            "generatedAt": "2026-01-01T00:00:00+00:00",
            "skillsRoot": str(Path(entry["path"]).parent),
            "entries": {"mine": entry},
        }

        with mock.patch.object(check_updates, "sync_registry", return_value=registry):
            with mock.patch.object(check_updates, "update_registry_entries", return_value=registry):
                with mock.patch.object(
                    check_updates,
                    "fetch_source_remote_version",
                    side_effect=AssertionError("check CLI accessed remote"),
                ):
                    output = io.StringIO()
                    with mock.patch.object(sys, "argv", ["check_updates.py", "--skill", "mine", "--json"]):
                        with redirect_stdout(output):
                            with self.assertRaises(SystemExit) as exit_context:
                                check_updates.main()
        self.assertEqual(exit_context.exception.code, 0)
        self.assertEqual(json.loads(output.getvalue())[0]["status"], "local_only")

        with mock.patch.object(update_agent_skills, "sync_registry", return_value=registry):
            with mock.patch.object(update_agent_skills, "update_registry_entries", return_value=registry):
                with mock.patch.object(
                    update_agent_skills,
                    "fetch_source_remote_observation",
                    side_effect=AssertionError("update CLI accessed remote"),
                ):
                    output = io.StringIO()
                    with mock.patch.object(sys, "argv", ["update_agent_skills.py", "--skill", "mine", "--json"]):
                        with redirect_stdout(output):
                            with self.assertRaises(SystemExit) as exit_context:
                                update_agent_skills.main()
        self.assertEqual(exit_context.exception.code, 0)
        item = json.loads(output.getvalue())[0]
        self.assertEqual(item["status"], "local_only")
        self.assertEqual(item["action"], "skipped_local")
        self.assertFalse(item["applied"])

    def test_invalid_update_policy_is_a_controlled_metadata_error(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError, load_agent_skill_source

        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "mine"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("mine\n", encoding="utf-8")
            (skill_dir / ".openskills.json").write_text(
                json.dumps({"updatePolicy": "sometimes"}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(AgentSkillUpdaterError, "Unsupported updatePolicy"):
                load_agent_skill_source(skill_dir)

    def test_registry_json_cli_structures_invalid_policy_error(self):
        import scripts.sync_skills_registry as sync_skills_registry

        output = io.StringIO()
        with mock.patch.object(
            sync_skills_registry,
            "sync_registry",
            side_effect=sync_skills_registry.AgentSkillUpdaterError(
                "Unsupported updatePolicy: typo"
            ),
        ):
            with mock.patch.object(sys, "argv", ["sync_skills_registry.py", "--json"]):
                with redirect_stdout(output):
                    with self.assertRaises(SystemExit) as exit_context:
                        sync_skills_registry._run_cli()

        self.assertEqual(exit_context.exception.code, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertIn("Unsupported updatePolicy", payload["error_message"])

    @staticmethod
    def local_only_entry() -> dict:
        return {
            "name": "mine",
            "entryType": "single-skill",
            "path": str(Path(tempfile.gettempdir()) / "mine"),
            "repoUrl": "https://github.com/me/mine",
            "source": "me/mine",
            "sourceType": "git",
            "subpath": ".",
            "updatePolicy": "local-only",
            "updateMode": "local-only",
            "installedBaseVersion": "a" * 40,
            "localVersion": "b" * 40,
            "remoteVersion": None,
            "managed": False,
        }


if __name__ == "__main__":
    unittest.main()
