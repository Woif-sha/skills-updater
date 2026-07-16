import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class SkillsRegistryTests(unittest.TestCase):
    def test_load_registry_rejects_unsupported_or_incomplete_schema(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError
        from scripts.skills_registry import REGISTRY_FILENAME, load_registry

        invalid_payloads = (
            {"version": 1, "entries": {}},
            {"version": 2},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    (root / REGISTRY_FILENAME).write_text(
                        json.dumps(payload),
                        encoding="utf-8",
                    )
                    with self.assertRaises(AgentSkillUpdaterError):
                        load_registry(root)

    def test_sync_registry_detects_superpowers_as_skill_pack(self):
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir)
            pack_dir = skills_root / "superpowers"
            (pack_dir / "skills").mkdir(parents=True)
            (pack_dir / ".git").mkdir()
            (pack_dir / ".openskills.json").write_text(
                json.dumps(
                    {
                        "source": "obra/superpowers",
                        "sourceType": "git-pack",
                        "repoUrl": "https://github.com/obra/superpowers",
                        "subpath": ".",
                        "installedBaseVersion": "abc123def456",
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch("scripts.skills_registry.get_git_remote_url", return_value="https://github.com/obra/superpowers"):
                with mock.patch("scripts.skills_registry.get_git_head_commit", return_value="abc123def456"):
                    registry = sync_registry(skills_root)

        entry = registry["entries"]["superpowers"]
        self.assertEqual(entry["entryType"], "skill-pack")
        self.assertEqual(entry["repoUrl"], "https://github.com/obra/superpowers")
        self.assertEqual(entry["localVersion"], "abc123def456")
        self.assertTrue(entry["managed"])

    def test_skill_pack_origin_mismatch_fails_closed(self):
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pack_dir = root / "pack"
            (pack_dir / "skills").mkdir(parents=True)
            (pack_dir / ".git").mkdir()
            (pack_dir / ".openskills.json").write_text(
                json.dumps(
                    {
                        "source": "example/expected",
                        "sourceType": "git-pack",
                        "repoUrl": "https://github.com/example/expected",
                        "subpath": ".",
                        "installedBaseVersion": "a" * 40,
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch(
                "scripts.skills_registry.get_git_remote_url",
                return_value="https://github.com/example/changed",
            ):
                with mock.patch(
                    "scripts.skills_registry.get_git_head_commit",
                    return_value="a" * 40,
                ):
                    entry = sync_registry(root)["entries"]["pack"]

        self.assertIn("origin and .openskills.json repoUrl differ", entry["metadataError"])
        self.assertFalse(entry["managed"])
        self.assertNotIn("autoUpdate", entry)

    def test_skill_pack_missing_provenance_fails_closed_without_inference(self):
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pack_dir = root / "pack"
            (pack_dir / "skills").mkdir(parents=True)
            (pack_dir / ".git").mkdir()
            (pack_dir / ".openskills.json").write_text(
                json.dumps({"installedBaseVersion": "a" * 40}),
                encoding="utf-8",
            )
            with mock.patch(
                "scripts.skills_registry.get_git_remote_url",
                return_value="https://github.com/example/pack",
            ):
                with mock.patch(
                    "scripts.skills_registry.get_git_head_commit",
                    return_value="a" * 40,
                ):
                    entry = sync_registry(root)["entries"]["pack"]

        self.assertFalse(entry["managed"])
        self.assertIsNone(entry["repoUrl"])
        self.assertIsNone(entry["source"])
        self.assertIsNone(entry["sourceType"])
        self.assertIn("source", entry["metadataError"])
        self.assertIn("repoUrl", entry["metadataError"])
        self.assertIn("subpath", entry["metadataError"])

    def test_sync_registry_does_not_infer_source_from_skill_name(self):
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir)
            skill_dir = skills_root / "skill-creator"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: skill-creator\n---\n", encoding="utf-8")

            registry = sync_registry(skills_root)

        entry = registry["entries"]["skill-creator"]
        self.assertEqual(entry["entryType"], "single-skill")
        self.assertIsNone(entry["repoUrl"])
        self.assertIsNone(entry["sourceType"])
        self.assertIsNone(entry["subpath"])
        self.assertEqual(entry["updateMode"], "unmanaged")
        self.assertFalse(entry["managed"])

    def test_sync_registry_removes_deleted_entries(self):
        from scripts.skills_registry import REGISTRY_FILENAME, sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir)
            registry_path = skills_root / REGISTRY_FILENAME
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 2,
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

    def test_sync_registry_marks_metadata_free_skill_unmanaged(self):
        from scripts.skills_registry import sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir)
            skill_dir = skills_root / "skills-updater"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: skills-updater\n---\n", encoding="utf-8")

            registry = sync_registry(skills_root)

        entry = registry["entries"]["skills-updater"]
        self.assertEqual(entry["entryType"], "single-skill")
        self.assertIsNone(entry["repoUrl"])
        self.assertIsNone(entry["sourceType"])
        self.assertEqual(entry["updateMode"], "unmanaged")
        self.assertFalse(entry["managed"])
        self.assertNotIn("autoUpdate", entry)

    def test_sync_registry_does_not_restore_deleted_provenance_from_cache(self):
        from scripts.skills_registry import REGISTRY_FILENAME, sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "demo"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("demo\n", encoding="utf-8")
            (skill_dir / ".openskills.json").write_text(
                json.dumps(
                    {
                        "sourceType": "git",
                        "installedBaseVersion": "a" * 40,
                    }
                ),
                encoding="utf-8",
            )
            (root / REGISTRY_FILENAME).write_text(
                json.dumps(
                    {
                        "version": 2,
                        "entries": {
                            "demo": {
                                "name": "demo",
                                "repoUrl": "https://github.com/example/old",
                                "source": "example/old",
                                "sourceType": "git",
                                "subpath": "skills/demo",
                                "remoteVersion": "b" * 40,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            entry = sync_registry(root)["entries"]["demo"]

        self.assertIsNone(entry["repoUrl"])
        self.assertIsNone(entry["remoteVersion"])
        self.assertFalse(entry["managed"])
        self.assertIn("repoUrl is required", entry["metadataError"])

    def test_registry_lock_rejects_a_second_writer(self):
        from scripts.agent_skill_updater import AgentSkillUpdaterError
        from scripts.skills_registry import registry_update_lock, sync_registry

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with registry_update_lock(root):
                with self.assertRaisesRegex(AgentSkillUpdaterError, "already being updated"):
                    sync_registry(root)

    def test_registry_field_merge_preserves_concurrent_and_unrelated_data(self):
        from scripts.skills_registry import REGISTRY_FILENAME, update_registry_entries

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry_path = root / REGISTRY_FILENAME
            registry_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "concurrentTopLevel": "keep",
                        "entries": {
                            "demo": {
                                "name": "demo",
                                "localVersion": "b" * 40,
                                "concurrentField": "keep",
                            },
                            "other": {"name": "other", "value": "keep"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            registry = update_registry_entries(
                {"demo": {"remoteVersion": "c" * 40, "lastStatus": "up_to_date"}},
                root,
            )

        self.assertEqual(registry["concurrentTopLevel"], "keep")
        self.assertEqual(registry["entries"]["demo"]["localVersion"], "b" * 40)
        self.assertEqual(registry["entries"]["demo"]["concurrentField"], "keep")
        self.assertEqual(registry["entries"]["other"]["value"], "keep")


if __name__ == "__main__":
    unittest.main()
