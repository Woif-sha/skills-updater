import json
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class SkillStructureTests(unittest.TestCase):
    def test_routing_manifest_is_valid_and_all_reads_exist(self):
        manifest = json.loads((REPO_ROOT / "routing.yaml").read_text(encoding="utf-8"))

        self.assertTrue(
            {
                "repair-updater takes precedence when the request changes or reviews this repository's implementation",
                "An explicit operation route takes precedence over default-invocation",
                "default-invocation matches only a bare invocation with no requested operation",
                "Multiple independent explicit operations are processed in the user's stated order",
                "mark-local-only already includes its same-target sync and check; do not select duplicate sync-registry or check-updates routes unless repetition is explicit",
            }.issubset(manifest["routing_rules"])
        )
        route_ids = [route["id"] for route in manifest["tasks"]]
        self.assertEqual(len(route_ids), len(set(route_ids)))
        self.assertIn("mark-local-only", route_ids)
        default_route = next(
            route for route in manifest["tasks"] if route["id"] == "default-invocation"
        )
        self.assertEqual(default_route["trigger_examples"], ["skills-updater"])
        self.assertTrue(
            {"检查", "更新", "安装", "同步", "修复"}.issubset(
                default_route["negative_signals"]
            )
        )
        self.assertEqual(
            default_route["positive_signals"],
            ["The request contains no check, update, install, sync, policy, or repair operation"],
        )
        repair_route = next(route for route in manifest["tasks"] if route["id"] == "repair-updater")
        self.assertTrue(
            {"workflows/task-closure.md", "workflows/update-rules.md"}.issubset(
                repair_route["required_reads"]
            )
        )
        repair_workflow = (REPO_ROOT / repair_route["workflow"]).read_text(encoding="utf-8")
        closure_workflow = (REPO_ROOT / "workflows/task-closure.md").read_text(encoding="utf-8")
        self.assertIn("[task-closure.md](task-closure.md)", repair_workflow)
        self.assertIn("[update-rules.md](update-rules.md)", closure_workflow)

        paths = list(manifest["always_read"])
        for route in manifest["tasks"]:
            self.assertTrue(route["route"])
            paths.extend(route["required_reads"])
            if route["workflow"] is not None:
                paths.append(route["workflow"])
        missing = sorted(path for path in set(paths) if not (REPO_ROOT / path).is_file())
        self.assertEqual(missing, [])

        routed = set(paths)
        documentation = {
            path.relative_to(REPO_ROOT).as_posix()
            for directory in ("rules", "workflows", "references")
            for path in (REPO_ROOT / directory).glob("*.md")
        }
        self.assertEqual(sorted(documentation - routed), [])

    def test_skill_entry_is_compact_and_routes_through_manifest(self):
        skill_text = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertLessEqual(len(skill_text.splitlines()), 90)
        self.assertIn("Read `routing.yaml`", skill_text)
        self.assertNotIn("sourceCommitSha", skill_text)
        self.assertNotIn("git pull", skill_text)

    def test_retired_non_core_subsystems_are_absent(self):
        retired = (
            "scripts/recommend_skills.py",
            "scripts/recommendations.json",
            "scripts/update_marketplace.py",
            "workflows/recommend-skills.md",
            "workflows/update-marketplace.md",
            "references/marketplaces.md",
        )

        self.assertEqual([path for path in retired if (REPO_ROOT / path).exists()], [])

    def test_internal_markdown_links_resolve(self):
        broken: list[str] = []
        for document in REPO_ROOT.rglob("*.md"):
            if ".git" in document.parts:
                continue
            text = document.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^]]*\]\(([^)]+)\)", text):
                path_text = target.split("#", 1)[0]
                if not path_text or "://" in path_text:
                    continue
                if not (document.parent / path_text).resolve().exists():
                    broken.append(f"{document.relative_to(REPO_ROOT)} -> {target}")
        self.assertEqual(sorted(broken), [])

    def test_i18n_has_no_missing_key_or_language_fallback(self):
        from scripts import i18n

        i18n.get_i18n("en")
        with self.assertRaises(KeyError):
            i18n.t("not-registered")
        with self.assertRaises(KeyError):
            i18n.t("skill_not_found")
        with self.assertRaises(ValueError):
            i18n.get_i18n("fr")


if __name__ == "__main__":
    unittest.main()
