#!/usr/bin/env python3
"""Check registry-backed updates for skills stored in ~/.agents/skills."""

from __future__ import annotations

import argparse
import io
import json
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))

from agent_skill_updater import OPENSPEC_REPO, fetch_remote_commit_sha, fetch_remote_package_version  # noqa: E402
from i18n import get_i18n, t  # noqa: E402
from skills_registry import save_registry, sync_registry  # noqa: E402


class UpdateStatus(Enum):
    UP_TO_DATE = "up_to_date"
    UPDATE_AVAILABLE = "update_available"
    UNKNOWN_VERSION = "unknown_version"
    ERROR = "error"


@dataclass
class SkillInfo:
    name: str
    entry_type: str
    source: str
    local_version: str
    remote_version: Optional[str]
    status: UpdateStatus
    install_path: str
    managed: bool
    error_message: Optional[str] = None


def probe_updates(filter_skill: Optional[str] = None) -> tuple[dict, list[SkillInfo]]:
    registry = sync_registry()
    results: list[SkillInfo] = []

    for name, entry in registry["entries"].items():
        if filter_skill and name != filter_skill:
            continue

        info = _entry_to_skill_info(entry)
        results.append(info)
        entry["localVersion"] = info.local_version
        entry["remoteVersion"] = info.remote_version
        entry["lastStatus"] = info.status.value
        entry["lastCheckedAt"] = registry["generatedAt"]

    save_registry(registry)
    return registry, results


def print_results(results: list[SkillInfo], as_json: bool = False) -> None:
    if as_json:
        payload = [
            {
                "name": item.name,
                "entry_type": item.entry_type,
                "source": item.source,
                "local_version": item.local_version,
                "remote_version": item.remote_version,
                "status": item.status.value,
                "install_path": item.install_path,
                "managed": item.managed,
                "error_message": item.error_message,
            }
            for item in results
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    up_to_date = [item for item in results if item.status == UpdateStatus.UP_TO_DATE]
    updates_available = [item for item in results if item.status == UpdateStatus.UPDATE_AVAILABLE]
    unknown = [item for item in results if item.status == UpdateStatus.UNKNOWN_VERSION]
    errors = [item for item in results if item.status == UpdateStatus.ERROR]

    print(f"📦 {t('installed_skills_status')}")
    print("━" * 26)
    print()

    if up_to_date:
        print(f"✅ {t('up_to_date')} ({len(up_to_date)}):")
        for item in up_to_date:
            suffix = f" [{item.entry_type}]"
            print(f"   • {item.name}{suffix} ({item.local_version})")
        print()

    if updates_available:
        print(f"⚠️  {t('updates_available')} ({len(updates_available)}):")
        for item in updates_available:
            print(f"   • {item.name} [{item.entry_type}]")
            print(f"     {t('local')}: {item.local_version} -> {t('remote')}: {item.remote_version or 'newer'}")
        print()

    if unknown:
        print(f"❓ {t('unknown_version')} ({len(unknown)}):")
        for item in unknown:
            print(f"   • {item.name} ({item.source})")
        print()

    if errors:
        print(f"❌ {t('errors')} ({len(errors)}):")
        for item in errors:
            print(f"   • {item.name}: {item.error_message}")
        print()

    print("━" * 26)
    print(f"{t('total')}: {len(results)} {t('skills')} | {len(updates_available)} {t('updates_available_count')}")


def _entry_to_skill_info(entry: dict) -> SkillInfo:
    name = entry["name"]
    entry_type = entry.get("entryType", "single-skill")
    repo_url = entry.get("repoUrl")
    local_version = entry.get("localVersion") or "unknown"
    source = entry.get("source") or repo_url or "unknown"
    managed = bool(entry.get("managed"))

    if not repo_url or not managed:
        return SkillInfo(
            name=name,
            entry_type=entry_type,
            source=source,
            local_version=local_version,
            remote_version=None,
            status=UpdateStatus.UNKNOWN_VERSION,
            install_path=entry["path"],
            managed=managed,
        )

    try:
        remote_version = _fetch_entry_remote_version(entry)
    except Exception as exc:  # noqa: BLE001
        return SkillInfo(
            name=name,
            entry_type=entry_type,
            source=source,
            local_version=local_version,
            remote_version=None,
            status=UpdateStatus.ERROR,
            install_path=entry["path"],
            managed=managed,
            error_message=str(exc),
        )

    if not remote_version or local_version == "unknown":
        status = UpdateStatus.UNKNOWN_VERSION
    elif remote_version == local_version:
        status = UpdateStatus.UP_TO_DATE
    else:
        status = UpdateStatus.UPDATE_AVAILABLE

    return SkillInfo(
        name=name,
        entry_type=entry_type,
        source=source,
        local_version=local_version,
        remote_version=remote_version,
        status=status,
        install_path=entry["path"],
        managed=managed,
    )


def _fetch_entry_remote_version(entry: dict) -> Optional[str]:
    if entry.get("entryType") == "skill-pack":
        return fetch_remote_commit_sha(entry["repoUrl"])
    if entry.get("sourceType") == "git-generated" and entry.get("repoUrl") == OPENSPEC_REPO:
        return fetch_remote_package_version(entry["repoUrl"])
    return fetch_remote_commit_sha(entry["repoUrl"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Check updates using ~/.agents/skills/.skills-list.json")
    parser.add_argument("--skill", help="Check a single skill or skill-pack")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--lang", choices=["en", "zh"], help="Force output language")
    args = parser.parse_args()

    if args.lang:
        get_i18n(args.lang)

    if not args.json:
        print(f"🔳 {t('checking_updates')}\n")

    _, results = probe_updates(filter_skill=args.skill)
    if not results:
        if args.skill:
            print(t("skill_not_found", skill=args.skill))
        else:
            print(t("no_installed_skills"))
        raise SystemExit(1)

    print_results(results, as_json=args.json)
    raise SystemExit(1 if any(item.status == UpdateStatus.UPDATE_AVAILABLE for item in results) else 0)


if __name__ == "__main__":
    main()
