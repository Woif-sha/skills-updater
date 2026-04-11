#!/usr/bin/env python3
"""Rescan ~/.agents/skills and rewrite .skills-list.json."""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path

script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))

from skills_registry import get_registry_path, sync_registry  # noqa: E402
from stdio_utils import configure_windows_utf8_stdio  # noqa: E402


configure_windows_utf8_stdio()


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh ~/.agents/skills/.skills-list.json")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    registry = sync_registry()
    if args.json:
        print(json.dumps(registry, indent=2, ensure_ascii=False))
    else:
        print(f"Registry synced: {get_registry_path()}")
        print(f"Entries: {len(registry['entries'])}")


if __name__ == "__main__":
    main()
