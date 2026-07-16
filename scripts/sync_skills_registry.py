#!/usr/bin/env python3
"""Rescan ~/.agents/skills and rewrite .skills-list.json."""

from __future__ import annotations

import json
import sys

from pathlib import Path

if __package__:
    from .agent_skill_updater import AgentSkillUpdaterError
    from .skills_registry import get_registry_path, sync_registry
    from .stdio_utils import JsonArgumentParser, configure_windows_utf8_stdio
else:
    sys.path.insert(0, str(Path(__file__).parent))
    from agent_skill_updater import AgentSkillUpdaterError  # noqa: E402
    from skills_registry import get_registry_path, sync_registry  # noqa: E402
    from stdio_utils import JsonArgumentParser, configure_windows_utf8_stdio  # noqa: E402


configure_windows_utf8_stdio()


def main() -> None:
    parser = JsonArgumentParser(
        description="Refresh ~/.agents/skills/.skills-list.json",
        json_error_factory=lambda message: {
            "status": "error",
            "error_message": message,
        },
    )
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    registry = sync_registry()
    if args.json:
        print(json.dumps(registry, indent=2, ensure_ascii=False))
    else:
        print(f"Registry synced: {get_registry_path()}")
        print(f"Entries: {len(registry['entries'])}")


def _run_cli() -> None:
    try:
        main()
    except (AgentSkillUpdaterError, OSError, ValueError) as exc:
        if "--json" in sys.argv[1:]:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error_message": str(exc),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    _run_cli()
