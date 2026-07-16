#!/usr/bin/env python3
"""Check registry-backed updates for skills stored in ~/.agents/skills."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

if __package__:
    from .agent_skill_updater import (
        LOCAL_ONLY_UPDATE_POLICY,
        AgentSkillUpdaterError,
        agent_skill_source_from_registry_entry,
        fetch_source_remote_version,
        probe_git_worktree,
        registry_entry_uses_git_worktree,
        versions_match,
    )
    from .i18n import get_i18n, t
    from .skills_registry import sync_registry, update_registry_entries
    from .stdio_utils import JsonArgumentParser, configure_windows_utf8_stdio
else:
    sys.path.insert(0, str(Path(__file__).parent))
    from agent_skill_updater import (  # noqa: E402
        LOCAL_ONLY_UPDATE_POLICY,
        AgentSkillUpdaterError,
        agent_skill_source_from_registry_entry,
        fetch_source_remote_version,
        probe_git_worktree,
        registry_entry_uses_git_worktree,
        versions_match,
    )
    from i18n import get_i18n, t  # noqa: E402
    from skills_registry import sync_registry, update_registry_entries  # noqa: E402
    from stdio_utils import JsonArgumentParser, configure_windows_utf8_stdio  # noqa: E402


configure_windows_utf8_stdio()


class UpdateStatus(Enum):
    UP_TO_DATE = "up_to_date"
    UPDATE_AVAILABLE = "update_available"
    LOCAL_ONLY = "local_only"
    UNKNOWN_VERSION = "unknown_version"
    ERROR = "error"


@dataclass
class SkillInfo:
    name: str
    entry_type: str
    source: str
    update_mode: str
    installed_base_version: str
    local_version: str
    remote_version: Optional[str]
    status: UpdateStatus
    install_path: str
    managed: bool
    git_relation: Optional[str] = None
    working_tree_dirty: Optional[bool] = None
    error_message: Optional[str] = None


def probe_updates(filter_skill: Optional[str] = None) -> tuple[dict, list[SkillInfo]]:
    registry = sync_registry()
    results: list[SkillInfo] = []
    registry_updates: dict[str, dict[str, object]] = {}

    for name, entry in registry["entries"].items():
        if filter_skill and name != filter_skill:
            continue

        info = _entry_to_skill_info(entry)
        results.append(info)
        fields: dict[str, object] = {
            "remoteVersion": info.remote_version,
            "lastStatus": info.status.value,
            "lastCheckedAt": registry["generatedAt"],
        }
        if info.git_relation is not None:
            fields["gitRelation"] = info.git_relation
        if info.working_tree_dirty is not None:
            fields["workingTreeDirty"] = info.working_tree_dirty
        registry_updates[name] = fields

    registry = update_registry_entries(registry_updates, Path(registry["skillsRoot"]))
    return registry, results


def print_results(results: list[SkillInfo], as_json: bool = False) -> None:
    if as_json:
        payload = [
            {
                "name": item.name,
                "entry_type": item.entry_type,
                "source": item.source,
                "update_mode": item.update_mode,
                "installed_base_version": item.installed_base_version,
                "local_version": item.local_version,
                "remote_version": item.remote_version,
                "status": item.status.value,
                "install_path": item.install_path,
                "managed": item.managed,
                "git_relation": item.git_relation,
                "working_tree_dirty": item.working_tree_dirty,
                "error_message": item.error_message,
            }
            for item in results
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    up_to_date = [item for item in results if item.status == UpdateStatus.UP_TO_DATE]
    updates_available = [item for item in results if item.status == UpdateStatus.UPDATE_AVAILABLE]
    local_only = [item for item in results if item.status == UpdateStatus.LOCAL_ONLY]
    unknown = [item for item in results if item.status == UpdateStatus.UNKNOWN_VERSION]
    errors = [item for item in results if item.status == UpdateStatus.ERROR]

    print(f"📦 {t('installed_skills_status')}")
    print("━" * 26)
    print()

    if up_to_date:
        print(f"✅ {t('up_to_date')} ({len(up_to_date)}):")
        for item in up_to_date:
            suffix = f" [{item.entry_type}]"
            print(f"   • {item.name}{suffix} ({_display_version(item.local_version)})")
        print()

    if updates_available:
        print(f"⚠️  {t('updates_available')} ({len(updates_available)}):")
        for item in updates_available:
            print(f"   • {item.name} [{item.entry_type}]")
            print(
                f"     {t('local')}: {_display_version(item.local_version)} -> "
                f"{t('remote')}: {_display_version(item.remote_version) if item.remote_version else 'newer'}"
            )
        print()

    if local_only:
        print(f"📌 {t('local_only')} ({len(local_only)}):")
        for item in local_only:
            print(f"   • {item.name} [{item.entry_type}]")
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
    entry_type = entry["entryType"]
    repo_url = entry.get("repoUrl")
    local_version = entry.get("localVersion") or "unknown"
    installed_base_version = entry.get("installedBaseVersion") or "unknown"
    update_mode = entry["updateMode"]
    source = entry.get("source") or repo_url or "unknown"
    managed = bool(entry.get("managed"))

    metadata_error = entry.get("metadataError")
    if metadata_error:
        return SkillInfo(
            name=name,
            entry_type=entry_type,
            source=source,
            update_mode=update_mode,
            installed_base_version=installed_base_version,
            local_version=local_version,
            remote_version=None,
            status=UpdateStatus.ERROR,
            install_path=entry["path"],
            managed=managed,
            error_message=str(metadata_error),
        )

    if entry.get("updatePolicy") == LOCAL_ONLY_UPDATE_POLICY:
        return SkillInfo(
            name=name,
            entry_type=entry_type,
            source=source,
            update_mode=update_mode,
            installed_base_version=installed_base_version,
            local_version=local_version,
            remote_version=None,
            status=UpdateStatus.LOCAL_ONLY,
            install_path=entry["path"],
            managed=managed,
        )

    if not repo_url or not managed:
        return SkillInfo(
            name=name,
            entry_type=entry_type,
            source=source,
            update_mode=update_mode,
            installed_base_version=installed_base_version,
            local_version=local_version,
            remote_version=None,
            status=UpdateStatus.UNKNOWN_VERSION,
            install_path=entry["path"],
            managed=managed,
        )

    try:
        git_worktree_mode = registry_entry_uses_git_worktree(entry)
        registry_source = agent_skill_source_from_registry_entry(entry)
    except (AgentSkillUpdaterError, OSError, ValueError) as exc:
        return SkillInfo(
            name=name,
            entry_type=entry_type,
            source=source,
            update_mode=update_mode,
            installed_base_version=installed_base_version,
            local_version=local_version,
            remote_version=None,
            status=UpdateStatus.ERROR,
            install_path=entry["path"],
            managed=managed,
            error_message=str(exc),
        )

    if git_worktree_mode:
        try:
            result = probe_git_worktree(registry_source)
        except (AgentSkillUpdaterError, OSError, ValueError) as exc:
            return SkillInfo(
                name=name,
                entry_type=entry_type,
                source=source,
                update_mode=update_mode,
                installed_base_version=installed_base_version,
                local_version=local_version,
                remote_version=None,
                status=UpdateStatus.ERROR,
                install_path=entry["path"],
                managed=managed,
                error_message=str(exc),
            )
        return SkillInfo(
            name=name,
            entry_type=entry_type,
            source=source,
            update_mode=update_mode,
            installed_base_version=installed_base_version,
            local_version=result.local_version,
            remote_version=result.remote_version,
            status=UpdateStatus(result.status),
            install_path=entry["path"],
            managed=managed,
            git_relation=result.relation,
            working_tree_dirty=result.working_tree_dirty,
            error_message=result.error_message,
        )

    try:
        remote_version = fetch_source_remote_version(registry_source)
    except (AgentSkillUpdaterError, OSError, ValueError) as exc:
        return SkillInfo(
            name=name,
            entry_type=entry_type,
            source=source,
            update_mode=update_mode,
            installed_base_version=installed_base_version,
            local_version=local_version,
            remote_version=None,
            status=UpdateStatus.ERROR,
            install_path=entry["path"],
            managed=managed,
            error_message=str(exc),
        )

    if not remote_version or local_version == "unknown":
        status = UpdateStatus.UNKNOWN_VERSION
    elif versions_match(remote_version, local_version):
        status = UpdateStatus.UP_TO_DATE
    else:
        status = UpdateStatus.UPDATE_AVAILABLE

    return SkillInfo(
        name=name,
        entry_type=entry_type,
        source=source,
        update_mode=update_mode,
        installed_base_version=installed_base_version,
        local_version=local_version,
        remote_version=remote_version,
        status=status,
        install_path=entry["path"],
        managed=managed,
    )


def _display_version(value: str) -> str:
    if len(value) == 40 and all(character in "0123456789abcdefABCDEF" for character in value):
        return value[:12]
    return value


def main() -> None:
    parser = JsonArgumentParser(
        description="Check updates using ~/.agents/skills/.skills-list.json",
        json_error_factory=lambda message: [
            {
                "name": "__arguments__",
                "entry_type": "arguments",
                "status": UpdateStatus.ERROR.value,
                "error_message": message,
            }
        ],
    )
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
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "name": args.skill or "__selection__",
                            "entry_type": "selection",
                            "status": UpdateStatus.ERROR.value,
                            "error_message": (
                                f"Skill '{args.skill}' was not found."
                                if args.skill
                                else "No installed skills were found."
                            ),
                        }
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
            raise SystemExit(1)
        if args.skill:
            print(t("skill_not_found", skill=args.skill))
        else:
            print(t("no_installed_skills"))
        raise SystemExit(1)

    print_results(results, as_json=args.json)
    failing_statuses = {UpdateStatus.UPDATE_AVAILABLE, UpdateStatus.ERROR}
    raise SystemExit(1 if any(item.status in failing_statuses for item in results) else 0)


def _run_cli() -> None:
    try:
        main()
    except (AgentSkillUpdaterError, OSError, ValueError) as exc:
        if "--json" in sys.argv[1:]:
            print(
                json.dumps(
                    [
                        {
                            "name": "__runtime__",
                            "entry_type": "runtime",
                            "status": UpdateStatus.ERROR.value,
                            "error_message": str(exc),
                        }
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    _run_cli()
