import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class SkillsRegistryTests(unittest.TestCase):
    def test_sync_registry_detects_superpowers_as_skill_pack(self):
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir)
            pack_dir = skills_root / "superpowers"
            (pack_dir / "skills").mkdir(parents=True)
            (pack_dir / ".git").mkdir()

            with mock.patch("scripts.skills_registry.get_git_remote_url", return_value="https://github.com/obra/superpowers"):
                with mock.patch("scripts.skills_registry.get_git_head_commit", return_value="abc123def456"):
                    registry = sync_registry(skills_root)

        entry = registry["entries"]["superpowers"]
        self.assertEqual(entry["entryType"], "skill-pack")
        self.assertEqual(entry["repoUrl"], "https://github.com/obra/superpowers")
        self.assertEqual(entry["localVersion"], "abc123def456")

    def test_sync_registry_infers_known_single_skill_source(self):
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir)
            skill_dir = skills_root / "skill-creator"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: skill-creator\n---\n", encoding="utf-8")

            registry = sync_registry(skills_root)

        entry = registry["entries"]["skill-creator"]
        self.assertEqual(entry["entryType"], "single-skill")
        self.assertEqual(entry["repoUrl"], "https://github.com/anthropics/skills")
        self.assertEqual(entry["subpath"], "skills/skill-creator")

    def test_sync_registry_removes_deleted_entries(self):
        from scripts.skills_registry import REGISTRY_FILENAME, sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir)
            registry_path = skills_root / REGISTRY_FILENAME
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "entries": {
                            "ghost-skill": {
                                "name": "ghost-skill",
                                "entryType": "single-skill",
                                "path": str(skills_root / "ghost-skill"),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            registry = sync_registry(skills_root)

        self.assertNotIn("ghost-skill", registry["entries"])

    def test_sync_registry_disables_auto_update_for_local_skills_updater(self):
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir)
            skill_dir = skills_root / "skills-updater"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: skills-updater\n---\n", encoding="utf-8")

            registry = sync_registry(skills_root)

        entry = registry["entries"]["skills-updater"]
        self.assertEqual(entry["entryType"], "single-skill")
        self.assertFalse(entry["autoUpdate"])


if __name__ == "__main__":
    unittest.main()
