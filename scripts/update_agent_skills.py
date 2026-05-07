#!/usr/bin/env python3
"""Apply updates to skills tracked in ~/.agents/skills/.skills-list.json."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Optional

script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))

from agent_skill_updater import (  # noqa: E402
    OPENSPEC_REPO,
    AgentSkillUpdaterError,
    AgentSkillSource,
    fetch_remote_commit_sha,
    fetch_remote_package_version,
    git_pull_repo,
    make_backup_root,
    refresh_skill_metadata_version,
    resolve_skill_update,
    update_skill_from_staged,
)
from i18n import get_i18n, t  # noqa: E402
from skills_registry import save_registry, sync_registry  # noqa: E402
from stdio_utils import configure_windows_utf8_stdio  # noqa: E402


configure_windows_utf8_stdio()


def main() -> None:
    parser = argparse.ArgumentParser(description="Update skills tracked in ~/.agents/skills/.skills-list.json")
    parser.add_argument("--skill", help="Update one skill or skill-pack")
    parser.add_argument("--check-only", action="store_true", help="Do not apply updates")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--lang", choices=["en", "zh"], help="Force output language")
    args = parser.parse_args()

    if args.lang:
        get_i18n(args.lang)

    registry = sync_registry()
    payload: list[dict[str, object]] = []
    updated_names: list[str] = []
    backup_root: Path | None = None

    with tempfile.TemporaryDirectory(prefix="skills-updater-apply-") as temp_dir:
        stage_root = Path(temp_dir)
        for name, entry in registry["entries"].items():
            if args.skill and name != args.skill:
                continue

            status, remote_version, error_message = _probe_entry(entry)
            item = {
                "name": name,
                "entry_type": entry.get("entryType"),
                "status": status,
                "local_version": entry.get("localVersion", "unknown"),
                "remote_version": remote_version,
                "error_message": error_message,
                "applied": False,
            }

            entry["remoteVersion"] = remote_version
            entry["lastStatus"] = status

            if not args.check_only and entry.get("autoUpdate") is False:
                item["error_message"] = "Auto-update disabled for this locally customized skill."
                payload.append(item)
                continue

            if args.check_only or (status not in {"update_available", "unknown_version"}) or entry["entryType"] == "skill-pack":
                if not args.check_only and status == "update_available" and entry["entryType"] == "skill-pack":
                    git_pull_repo(Path(entry["path"]))
                    item["applied"] = True
                    updated_names.append(name)
                payload.append(item)
                continue

            source = AgentSkillSource(
                name=entry["name"],
                local_dir=Path(entry["path"]),
                source=entry.get("source"),
                source_type=entry.get("sourceType"),
                repo_url=entry.get("repoUrl"),
                subpath=entry.get("subpath"),
                generator=entry.get("generator"),
                workflow_id=entry.get("workflowId"),
                metadata_path=(Path(entry["path"]) / ".openskills.json"),
            )
            resolved = resolve_skill_update(source, stage_root / name)
            if resolved.status == "update_available":
                if backup_root is None:
                    backup_root = make_backup_root()
                try:
                    update_skill_from_staged(resolved, backup_root)
                except AgentSkillUpdaterError as exc:
                    item["status"] = "error"
                    item["error_message"] = str(exc)
                    item["backup_root"] = str(backup_root)
                    payload.append(item)
                    continue
                item["applied"] = True
                item["backup_root"] = str(backup_root)
                updated_names.append(name)
            elif status == "unknown_version" and remote_version:
                refresh_skill_metadata_version(source, remote_version)
                item["status"] = resolved.status
                item["remote_version"] = remote_version
                item["applied"] = True
            elif status == "update_available" and resolved.status == "up_to_date" and remote_version:
                refresh_skill_metadata_version(source, remote_version)
                item["status"] = resolved.status
                item["remote_version"] = remote_version
                item["applied"] = True
            else:
                item["status"] = resolved.status
                item["error_message"] = resolved.error_message

            payload.append(item)

    refreshed_registry = sync_registry()
    save_registry(refreshed_registry)

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        raise SystemExit(0)

    if not payload:
        if args.skill:
            print(t("skill_not_found", skill=args.skill))
        else:
            print(t("no_installed_skills"))
        raise SystemExit(1)

    if args.check_only:
        print("Check completed. Use update_agent_skills.py without --check-only to apply updates.")
    else:
        if updated_names:
            print(f"Updated {len(updated_names)} item(s): {', '.join(updated_names)}")
            if backup_root is not None:
                print(f"Backup: {backup_root}")
        else:
            print("No skill updates were applied.")

    errors = [item for item in payload if item["status"] == "error"]
    if errors:
        print(f"{t('warning')}: {len(errors)} item(s) could not be processed.")
        raise SystemExit(1)

    raise SystemExit(0)


def _probe_entry(entry: dict) -> tuple[str, Optional[str], Optional[str]]:
    repo_url = entry.get("repoUrl")
    local_version = entry.get("localVersion") or "unknown"
    if not repo_url or not entry.get("managed"):
        return "unknown_version", None, None

    try:
        if entry.get("entryType") == "skill-pack":
            remote_version = fetch_remote_commit_sha(repo_url)
        elif entry.get("sourceType") == "git-generated" and repo_url == OPENSPEC_REPO:
            remote_version = fetch_remote_package_version(repo_url)
        else:
            remote_version = fetch_remote_commit_sha(repo_url)
    except Exception as exc:  # noqa: BLE001
        return "error", None, str(exc)

    if not remote_version or local_version == "unknown":
        return "unknown_version", remote_version, None
    if remote_version == local_version:
        return "up_to_date", remote_version, None
    return "update_available", remote_version, None


if __name__ == "__main__":
    main()
