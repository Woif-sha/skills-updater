#!/usr/bin/env python3
"""Rescan ~/.agents/skills and rewrite .skills-list.json."""

from __future__ import annotations

import argparse
import io
import json
import sys

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from pathlib import Path

script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))

from skills_registry import get_registry_path, sync_registry  # noqa: E402


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
