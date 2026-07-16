import json
import subprocess
import sys
import unittest
from pathlib import Path


class CliProtocolTests(unittest.TestCase):
    def test_json_argument_errors_are_always_structured(self):
        repo_root = Path(__file__).resolve().parents[1]
        cases = (
            ("check_updates.py", ["--json", "--definitely-invalid"], list),
            ("update_agent_skills.py", ["--json", "--definitely-invalid"], list),
            ("install_agent_skill.py", ["--json"], list),
            ("sync_skills_registry.py", ["--json", "--definitely-invalid"], dict),
        )

        for script_name, arguments, payload_type in cases:
            with self.subTest(script=script_name):
                result = subprocess.run(
                    [sys.executable, str(repo_root / "scripts" / script_name), *arguments],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="strict",
                    timeout=20,
                )

                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr, "")
                payload = json.loads(result.stdout)
                self.assertIsInstance(payload, payload_type)
                item = payload[0] if isinstance(payload, list) else payload
                self.assertEqual(item["status"], "error")
                self.assertTrue(item["error_message"])


if __name__ == "__main__":
    unittest.main()
