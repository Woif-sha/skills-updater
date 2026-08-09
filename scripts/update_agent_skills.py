#!/usr/bin/env python3
"""Apply updates to skills tracked in ~/.agents/skills/.skills-list.json."""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

if __package__:
    from .agent_skill_updater import (
        LOCAL_ONLY_UPDATE_POLICY,
        AgentSkillSource,
        AgentSkillRecoveryUncertainError,
        AgentSkillUpdateCommittedError,
        AgentSkillUpdaterError,
        RemoteObservation,
        TransactionOutcome,
        apply_observed_update,
        agent_skill_source_from_registry_entry,
        fetch_source_remote_observation,
        make_backup_root,
        probe_git_worktree,
        registry_entry_uses_git_worktree,
        resolve_skill_update,
        update_git_worktree_skill,
        update_skill_from_staged,
        versions_match,
    )
    from .i18n import get_i18n, t
    from .skills_registry import sync_registry, update_registry_entries
    from .stdio_utils import JsonArgumentParser, configure_windows_utf8_stdio
else:
    sys.path.insert(0, str(Path(__file__).parent))
    from agent_skill_updater import (  # noqa: E402
        LOCAL_ONLY_UPDATE_POLICY,
        AgentSkillSource,
        AgentSkillRecoveryUncertainError,
        AgentSkillUpdateCommittedError,
        AgentSkillUpdaterError,
        RemoteObservation,
        TransactionOutcome,
        apply_observed_update,
        agent_skill_source_from_registry_entry,
        fetch_source_remote_observation,
        make_backup_root,
        probe_git_worktree,
        registry_entry_uses_git_worktree,
        resolve_skill_update,
        update_git_worktree_skill,
        update_skill_from_staged,
        versions_match,
    )
    from i18n import get_i18n, t  # noqa: E402
    from skills_registry import sync_registry, update_registry_entries  # noqa: E402
    from stdio_utils import JsonArgumentParser, configure_windows_utf8_stdio  # noqa: E402


configure_windows_utf8_stdio()


@dataclass
class EntryProbe:
    status: str
    local_version: str
    remote_version: Optional[str]
    error_message: Optional[str] = None
    git_relation: Optional[str] = None
    working_tree_dirty: Optional[bool] = None
    remote_observation: Optional[RemoteObservation] = None


def main() -> None:
    parser = JsonArgumentParser(
        description="Update skills tracked in ~/.agents/skills/.skills-list.json",
        json_error_factory=lambda message: [
            {
                "name": "__arguments__",
                "entry_type": "arguments",
                "status": "error",
                "error_message": message,
                "applied": False,
                "action": "none",
                "installed_state": "unchanged",
            }
        ],
    )
    parser.add_argument("--skill", help="Update one skill or skill-pack")
    parser.add_argument("--check-only", action="store_true", help="Do not apply updates")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--lang", choices=["en", "zh"], help="Force output language")
    args = parser.parse_args()

    if args.lang:
        get_i18n(args.lang)

    try:
        registry = sync_registry()
    except AgentSkillRecoveryUncertainError as exc:
        outcome = exc.outcome
        item = _recovery_outcome_item(outcome)
        if args.json:
            print(json.dumps([item], indent=2, ensure_ascii=False))
        else:
            print(f"Error: {outcome.error_message}", file=sys.stderr)
        raise SystemExit(1)
    payload: list[dict[str, object]] = []
    updated_names: list[str] = []
    backup_root: Path | None = None

    with tempfile.TemporaryDirectory(prefix="skills-updater-apply-") as temp_dir:
        stage_root = Path(temp_dir)
        for name, entry in registry["entries"].items():
            if args.skill and name != args.skill:
                continue

            probe = _probe_entry(entry)
            item = {
                "name": name,
                "entry_type": entry.get("entryType"),
                "update_mode": entry.get("updateMode"),
                "status": probe.status,
                "installed_base_version": entry.get("installedBaseVersion", "unknown"),
                "local_version": probe.local_version,
                "remote_version": probe.remote_version,
                "git_relation": probe.git_relation,
                "working_tree_dirty": probe.working_tree_dirty,
                "action": "none",
                "error_message": probe.error_message,
                "applied": False,
                "installed_state": "unchanged",
            }

            entry["localVersion"] = probe.local_version
            entry["remoteVersion"] = probe.remote_version
            entry["lastStatus"] = probe.status
            if probe.git_relation is not None:
                entry["gitRelation"] = probe.git_relation
            if probe.working_tree_dirty is not None:
                entry["workingTreeDirty"] = probe.working_tree_dirty

            try:
                if entry.get("updatePolicy") == LOCAL_ONLY_UPDATE_POLICY:
                    item["action"] = "skipped_local"
                    payload.append(item)
                    continue

                if args.check_only or probe.status == "error":
                    payload.append(item)
                    continue

                if probe.status == "unknown_version":
                    payload.append(item)
                    continue

                source = agent_skill_source_from_registry_entry(entry)
                if registry_entry_uses_git_worktree(entry):
                    result = update_git_worktree_skill(source)
                    item["status"] = result.status
                    item["local_version"] = result.local_version
                    item["remote_version"] = result.remote_version
                    item["git_relation"] = result.relation
                    item["working_tree_dirty"] = result.working_tree_dirty
                    item["error_message"] = result.error_message
                    item["applied"] = result.applied
                    item["action"] = result.action
                    item["installed_state"] = result.installed_state
                    if result.diagnostic_journal is not None:
                        item["diagnostic_journal"] = str(result.diagnostic_journal)
                    if result.cleanup_residue is not None:
                        item["cleanup_residue"] = str(result.cleanup_residue)
                    if result.action in {"metadata_refreshed", "fast_forwarded"}:
                        item["installed_base_version"] = result.remote_version
                    if result.applied:
                        updated_names.append(name)
                    payload.append(item)
                    continue

                if (
                    probe.status == "up_to_date"
                    and probe.remote_version
                    and probe.local_version != probe.remote_version
                    and versions_match(probe.local_version, probe.remote_version)
                ):
                    outcome = _apply_metadata_observation(
                        item,
                        source,
                        str(entry["installedBaseVersion"]),
                        _require_probe_observation(name, probe),
                    )
                    if outcome.applied:
                        updated_names.append(name)
                    payload.append(item)
                    continue

                if probe.status != "update_available":
                    payload.append(item)
                    continue

                resolved = resolve_skill_update(
                    source,
                    stage_root / name,
                    _require_probe_observation(name, probe),
                )
                item["installed_base_version"] = resolved.installed_base_version
                item["local_version"] = resolved.local_version
                resolved_remote_version = resolved.remote_version or probe.remote_version
                if resolved.status == "update_available":
                    if backup_root is None:
                        backup_root = make_backup_root(Path(registry["skillsRoot"]))
                    update_skill_from_staged(resolved, backup_root)
                    item["status"] = "up_to_date"
                    item["remote_version"] = resolved.remote_version
                    item["installed_base_version"] = resolved.remote_version
                    item["local_version"] = resolved.remote_version
                    item["applied"] = True
                    item["action"] = "payload_merged"
                    item["installed_state"] = "committed"
                    item["backup_root"] = str(backup_root)
                    updated_names.append(name)
                elif resolved.status == "up_to_date" and resolved_remote_version:
                    if resolved.remote_observation is None:
                        raise AgentSkillUpdaterError(
                            f"Resolved update for '{name}' is missing its Remote Observation."
                        )
                    outcome = _apply_metadata_observation(
                        item,
                        source,
                        resolved.installed_base_version,
                        resolved.remote_observation,
                    )
                    item["remote_version"] = resolved_remote_version
                    if outcome.applied:
                        updated_names.append(name)
                else:
                    item["status"] = resolved.status
                    item["error_message"] = resolved.error_message
            except AgentSkillRecoveryUncertainError as exc:
                _apply_transaction_outcome(item, exc.outcome)
            except AgentSkillUpdateCommittedError as exc:
                item["status"] = "error"
                item["error_message"] = str(exc)
                item["applied"] = True
                item["action"] = exc.action
                item["installed_state"] = "committed"
                if exc.version is not None:
                    item["installed_base_version"] = exc.version
                    item["local_version"] = exc.version
                    item["remote_version"] = exc.version
                if name not in updated_names:
                    updated_names.append(name)
                if backup_root is not None:
                    item["backup_root"] = str(backup_root)
            except (AgentSkillUpdaterError, OSError, ValueError) as exc:
                item["status"] = "error"
                item["error_message"] = str(exc)
                item["applied"] = False
                item["installed_state"] = "unchanged"
                if backup_root is not None:
                    try:
                        removed = _remove_empty_backup_root(backup_root)
                    except OSError as cleanup_exc:
                        item["error_message"] = (
                            f"{exc}. Empty backup-root cleanup failed at "
                            f"{backup_root}: {cleanup_exc}"
                        )
                        item["backup_root"] = str(backup_root)
                    else:
                        if removed:
                            backup_root = None
                        else:
                            item["backup_root"] = str(backup_root)

            payload.append(item)

    try:
        refreshed_registry = sync_registry()
        registry_updates: dict[str, dict[str, object]] = {}
        for item in payload:
            refreshed_entry = refreshed_registry["entries"].get(item["name"])
            if refreshed_entry is None:
                continue
            fields: dict[str, object] = {
                "remoteVersion": item.get("remote_version"),
                "lastStatus": item["status"],
                "lastCheckedAt": refreshed_registry["generatedAt"],
            }
            if item.get("git_relation") is not None:
                fields["gitRelation"] = item["git_relation"]
            if item.get("working_tree_dirty") is not None:
                fields["workingTreeDirty"] = item["working_tree_dirty"]
            registry_updates[str(item["name"])] = fields
        update_registry_entries(
            registry_updates,
            Path(refreshed_registry["skillsRoot"]),
        )
    except AgentSkillRecoveryUncertainError as exc:
        payload.append(_recovery_outcome_item(exc.outcome))
    except (AgentSkillUpdaterError, OSError, ValueError) as exc:
        payload.append(
            {
                "name": "__registry__",
                "entry_type": "registry",
                "status": "error",
                "error_message": str(exc),
                "applied": False,
                "action": "none",
                "installed_state": "unchanged",
            }
        )

    if args.json:
        if not payload:
            payload.append(
                {
                    "name": args.skill or "__selection__",
                    "entry_type": "selection",
                    "status": "error",
                    "error_message": (
                        f"Skill '{args.skill}' was not found."
                        if args.skill
                        else "No installed skills were found."
                    ),
                    "applied": False,
                    "action": "none",
                    "installed_state": "unchanged",
                }
            )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        has_errors = any(item["status"] == "error" for item in payload)
        raise SystemExit(1 if has_errors or not payload else 0)

    if not payload:
        if args.skill:
            print(t("skill_not_found", skill=args.skill))
        else:
            print(t("no_installed_skills"))
        raise SystemExit(1)

    if args.check_only:
        print("Check completed. Use update_agent_skills.py without --check-only to apply updates.")
    else:
        local_only_names = [
            str(item["name"])
            for item in payload
            if item.get("action") == "skipped_local"
        ]
        if updated_names:
            print(f"Updated {len(updated_names)} item(s): {', '.join(updated_names)}")
            if backup_root is not None:
                print(f"Backup: {backup_root}")
        else:
            print("No skill updates were applied.")
        if local_only_names:
            print(f"Skipped local-only item(s): {', '.join(local_only_names)}")

    errors = [item for item in payload if item["status"] == "error"]
    if errors:
        print(f"{t('warning')}: {len(errors)} item(s) could not be processed.")
        raise SystemExit(1)

    raise SystemExit(0)


def _remove_empty_backup_root(backup_root: Path) -> bool:
    if any(backup_root.iterdir()):
        return False
    backup_root.rmdir()
    return True


def _apply_transaction_outcome(
    item: dict[str, object],
    outcome: TransactionOutcome,
) -> None:
    item["status"] = outcome.status
    item["installed_state"] = outcome.installed_state
    item["applied"] = outcome.applied
    item["action"] = outcome.action
    item["error_message"] = outcome.error_message
    if outcome.diagnostic_journal is not None:
        item["diagnostic_journal"] = str(outcome.diagnostic_journal)
    if outcome.cleanup_residue is not None:
        item["cleanup_residue"] = str(outcome.cleanup_residue)


def _require_probe_observation(
    name: str,
    probe: EntryProbe,
) -> RemoteObservation:
    if probe.remote_observation is None:
        raise AgentSkillUpdaterError(
            f"Probe for '{name}' is missing its Remote Observation."
        )
    return probe.remote_observation


def _recovery_outcome_item(outcome: TransactionOutcome) -> dict[str, object]:
    item: dict[str, object] = {
        "name": outcome.name,
        "entry_type": "recovery",
    }
    _apply_transaction_outcome(item, outcome)
    return item


def _apply_metadata_observation(
    item: dict[str, object],
    source: AgentSkillSource,
    installed_base_version: str,
    observation: RemoteObservation,
) -> TransactionOutcome:
    outcome = apply_observed_update(
        source,
        observation,
        installed_base_version=installed_base_version,
    )
    _apply_transaction_outcome(item, outcome)
    if outcome.version is not None and outcome.installed_state == "committed":
        item["installed_base_version"] = outcome.version
        item["local_version"] = outcome.version
    return outcome


def _probe_entry(entry: dict) -> EntryProbe:
    repo_url = entry.get("repoUrl")
    local_version = entry.get("localVersion") or "unknown"
    metadata_error = entry.get("metadataError")
    if metadata_error:
        return EntryProbe("error", local_version, None, str(metadata_error))
    if entry.get("updatePolicy") == LOCAL_ONLY_UPDATE_POLICY:
        return EntryProbe("local_only", local_version, None)
    if not repo_url or not entry.get("managed"):
        return EntryProbe("unknown_version", local_version, None)
    try:
        git_worktree_mode = registry_entry_uses_git_worktree(entry)
        source = agent_skill_source_from_registry_entry(entry)
    except (AgentSkillUpdaterError, OSError, ValueError) as exc:
        return EntryProbe("error", local_version, None, str(exc))

    if git_worktree_mode:
        try:
            result = probe_git_worktree(source)
        except (AgentSkillUpdaterError, OSError, ValueError) as exc:
            return EntryProbe("error", local_version, None, str(exc))
        return EntryProbe(
            status=result.status,
            local_version=result.local_version,
            remote_version=result.remote_version,
            error_message=result.error_message,
            git_relation=result.relation,
            working_tree_dirty=result.working_tree_dirty,
        )

    try:
        observation = fetch_source_remote_observation(source)
    except (AgentSkillUpdaterError, OSError, ValueError) as exc:
        return EntryProbe("error", local_version, None, str(exc))

    remote_version = observation.version
    if not remote_version or local_version == "unknown":
        return EntryProbe(
            "unknown_version",
            local_version,
            remote_version,
            remote_observation=observation,
        )
    if versions_match(remote_version, local_version):
        return EntryProbe(
            "up_to_date",
            local_version,
            remote_version,
            remote_observation=observation,
        )
    return EntryProbe(
        "update_available",
        local_version,
        remote_version,
        remote_observation=observation,
    )


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
                            "status": "error",
                            "error_message": str(exc),
                            "applied": False,
                            "action": "none",
                            "installed_state": "unchanged",
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
