#!/usr/bin/env python3
"""Registry management for skills stored in ~/.agents/skills."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

if __package__:
    from .agent_skill_updater import (
        AgentSkillUpdaterError,
        LOCAL_ONLY_UPDATE_POLICY,
        canonical_repo_identity,
        detect_skill_entry_type,
        get_agent_skills_dir,
        is_git_worktree_skill,
        normalize_git_commit,
        recover_incomplete_skill_transactions,
        same_git_commit,
        sanitize_repo_url,
        skill_update_lock,
        _is_filesystem_link,
        _validate_skill_root,
    )
else:
    from agent_skill_updater import (
        AgentSkillUpdaterError,
        LOCAL_ONLY_UPDATE_POLICY,
        canonical_repo_identity,
        detect_skill_entry_type,
        get_agent_skills_dir,
        is_git_worktree_skill,
        normalize_git_commit,
        recover_incomplete_skill_transactions,
        same_git_commit,
        sanitize_repo_url,
        skill_update_lock,
        _is_filesystem_link,
        _validate_skill_root,
    )

REGISTRY_FILENAME = ".skills-list.json"
REGISTRY_VERSION = 2


def get_registry_path(skills_root: Optional[Path] = None) -> Path:
    return (skills_root or get_agent_skills_dir()) / REGISTRY_FILENAME


def load_registry(skills_root: Optional[Path] = None) -> dict:
    registry_path = get_registry_path(skills_root)
    if not registry_path.exists():
        return {
            "version": REGISTRY_VERSION,
            "generatedAt": None,
            "skillsRoot": str(skills_root or get_agent_skills_dir()),
            "entries": {},
        }
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(registry, dict):
        raise AgentSkillUpdaterError(f"Registry must contain a JSON object: {registry_path}")
    if registry.get("version") != REGISTRY_VERSION:
        raise AgentSkillUpdaterError(
            f"Unsupported registry version in {registry_path}: {registry.get('version')}"
        )
    if not isinstance(registry.get("entries"), dict):
        raise AgentSkillUpdaterError(f"Registry entries must be a JSON object: {registry_path}")
    return registry


def sync_registry(skills_root: Optional[Path] = None) -> dict:
    root = skills_root or get_agent_skills_dir()
    root.mkdir(parents=True, exist_ok=True)
    with registry_update_lock(root):
        return _sync_registry_unlocked(root)


def _sync_registry_unlocked(root: Path) -> dict:
    recover_incomplete_skill_transactions(root)
    previous = load_registry(root)["entries"]
    entries: dict[str, dict] = {}

    for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if child.name.startswith(".") or not child.is_dir():
            continue

        with skill_update_lock(child):
            detected = detect_registry_entry(child, previous.get(child.name))
        if detected is not None:
            entries[child.name] = detected

    registry = {
        "version": REGISTRY_VERSION,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "skillsRoot": str(root),
        "entries": entries,
    }
    _write_json_atomic(get_registry_path(root), registry)
    return registry


def update_registry_entries(
    updates: dict[str, dict[str, object]],
    skills_root: Optional[Path] = None,
) -> dict:
    root = skills_root or get_agent_skills_dir()
    root.mkdir(parents=True, exist_ok=True)
    with registry_update_lock(root):
        registry = load_registry(root)
        entries = registry["entries"]
        for name, fields in updates.items():
            entry = entries.get(name)
            if not isinstance(entry, dict):
                continue
            entry.update(fields)
        _write_json_atomic(get_registry_path(root), registry)
        return registry


@contextmanager
def registry_update_lock(skills_root: Optional[Path] = None) -> Iterator[None]:
    root = skills_root or get_agent_skills_dir()
    root.mkdir(parents=True, exist_ok=True)
    with skill_update_lock(get_registry_path(root)):
        yield


def detect_registry_entry(skill_path: Path, previous_entry: Optional[dict] = None) -> Optional[dict]:
    _validate_skill_root(skill_path)
    entry_type = detect_skill_entry_type(skill_path)
    if entry_type == "skill-pack":
        return _build_skill_pack_entry(skill_path, previous_entry)
    if entry_type == "single-skill":
        return _build_single_skill_entry(skill_path, previous_entry)
    return None


def get_git_remote_url(repo_dir: Path) -> Optional[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return normalize_github_url(result.stdout.strip())


def get_git_head_commit(repo_dir: Path) -> Optional[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def normalize_github_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return sanitize_repo_url(url)


def _build_skill_pack_entry(skill_path: Path, previous_entry: Optional[dict]) -> dict:
    metadata = _load_skill_metadata(skill_path)
    local_only = metadata.get("updatePolicy") == LOCAL_ONLY_UPDATE_POLICY
    git_head = get_git_head_commit(skill_path)
    if local_only:
        installed_base = normalize_git_commit(metadata.get("installedBaseVersion"))
        metadata_error = None
        raw_repo_url = metadata.get("repoUrl")
        repo_url = sanitize_repo_url(str(raw_repo_url)) if raw_repo_url else None
        source = metadata.get("source") or skill_path.name
        source_type = metadata.get("sourceType") or "local"
        subpath = metadata.get("subpath")
    else:
        installed_base, metadata_error = _installed_base_from_metadata(metadata)
        metadata_error = metadata_error or _required_metadata_error(
            metadata,
            ("source", "sourceType", "repoUrl", "subpath", "installedBaseVersion"),
            "remotely managed skill pack",
        )
        origin_url = get_git_remote_url(skill_path)
        metadata_repo_url = metadata.get("repoUrl")
        repo_url = (
            sanitize_repo_url(str(metadata_repo_url))
            if metadata_repo_url
            else None
        )
        source = metadata.get("source")
        source_type = metadata.get("sourceType")
        subpath = metadata.get("subpath")
        if origin_url is None:
            metadata_error = metadata_error or "Git worktree origin is required for remote updates."
        elif repo_url and not _repo_urls_match(origin_url, repo_url):
            metadata_error = metadata_error or (
                "Git origin and .openskills.json repoUrl differ: "
                f"{sanitize_repo_url(origin_url)} != {repo_url}."
            )
        if source_type and source_type != "git-pack":
            metadata_error = metadata_error or (
                "sourceType must be 'git-pack' for a remotely managed skill pack."
            )
        if subpath and subpath != ".":
            metadata_error = metadata_error or (
                "subpath must be '.' for a root Git worktree skill pack."
            )
        if installed_base is None:
            metadata_error = metadata_error or (
                "installedBaseVersion is required in .openskills.json for remotely managed "
                "skill packs."
            )
        if git_head is None:
            metadata_error = metadata_error or "Git HEAD is required for a skill pack."
        elif installed_base and same_git_commit(git_head, installed_base):
            installed_base = git_head
    entry = {
        "name": skill_path.name,
        "entryType": "skill-pack",
        "path": str(skill_path),
        "repoUrl": repo_url,
        "source": source,
        "sourceType": source_type,
        "subpath": subpath,
        "updatePolicy": LOCAL_ONLY_UPDATE_POLICY if local_only else None,
        "updateMode": "local-only" if local_only else "git-worktree",
        "installedBaseVersion": installed_base or "unknown",
        "localVersion": git_head or "unknown",
        "remoteVersion": None if local_only else _cached_remote_version(repo_url, previous_entry),
        "managed": bool(repo_url) and not local_only and metadata_error is None,
    }
    if metadata_error:
        entry["metadataError"] = metadata_error
    return entry


def _build_single_skill_entry(skill_path: Path, previous_entry: Optional[dict]) -> dict:
    metadata = _load_skill_metadata(skill_path)
    local_only = metadata.get("updatePolicy") == LOCAL_ONLY_UPDATE_POLICY
    git_worktree = is_git_worktree_skill(skill_path)
    git_head = get_git_head_commit(skill_path) if git_worktree else None
    raw_repo_url = metadata.get("repoUrl")
    repo_url = sanitize_repo_url(str(raw_repo_url)) if raw_repo_url else None
    workflow_id = metadata.get("workflowId")
    generator = metadata.get("generator")

    if local_only:
        installed_base = normalize_git_commit(metadata.get("installedBaseVersion")) or "unknown"
        metadata_error = None
        source_type = metadata.get("sourceType") or "local"
        subpath = metadata.get("subpath")
        source = metadata.get("source") or skill_path.name
    elif git_worktree:
        installed_base, metadata_error = _installed_base_from_metadata(metadata)
        metadata_error = metadata_error or _required_metadata_error(
            metadata,
            ("source", "sourceType", "repoUrl", "subpath", "installedBaseVersion"),
            "remotely managed Git worktree skill",
        )
        git_repo_url = get_git_remote_url(skill_path)
        source_type = metadata.get("sourceType")
        subpath = metadata.get("subpath")
        source = metadata.get("source")
        if git_repo_url is None:
            metadata_error = metadata_error or "Git worktree origin is required for remote updates."
        elif repo_url and not _repo_urls_match(git_repo_url, repo_url):
            metadata_error = metadata_error or (
                "Git origin and .openskills.json repoUrl differ: "
                f"{sanitize_repo_url(git_repo_url)} != {repo_url}."
            )
        if source_type and source_type != "git":
            metadata_error = metadata_error or (
                "sourceType must be 'git' for a remotely managed single-skill Git worktree."
            )
        if subpath and subpath != ".":
            metadata_error = metadata_error or (
                "subpath must be '.' for a root Git worktree skill."
            )
        if installed_base is None:
            metadata_error = metadata_error or (
                "installedBaseVersion is required in .openskills.json for remotely managed skills."
            )
        if git_head is None:
            metadata_error = metadata_error or "Git HEAD is required for a Git worktree skill."
        elif installed_base and same_git_commit(git_head, installed_base):
            installed_base = git_head
        installed_base = installed_base or "unknown"
    else:
        source_type = metadata.get("sourceType")
        subpath = metadata.get("subpath")
        source = metadata.get("source")
        if repo_url:
            installed_base, metadata_error = _installed_base_from_metadata(metadata)
            metadata_error = metadata_error or _required_metadata_error(
                metadata,
                ("source", "sourceType", "repoUrl", "subpath"),
                "remotely managed snapshot skill",
            )
            if source_type not in {"git", "git-generated"}:
                metadata_error = metadata_error or (
                    "sourceType must be 'git' or 'git-generated' for a remotely managed snapshot skill."
                )
            if source_type != "git-generated" and installed_base is None:
                metadata_error = metadata_error or (
                    "installedBaseVersion is required in .openskills.json for remotely managed skills."
                )
            installed_base = installed_base or "unknown"
        else:
            metadata_error = None
            installed_base = "unknown"
            if source_type not in {None, "local"}:
                metadata_error = "repoUrl is required for remotely managed skills."

    if local_only:
        local_version = git_head or "local"
        update_mode = "local-only"
    elif source_type == "git-generated":
        local_version = _generated_version(skill_path / "SKILL.md") or metadata.get("generatedByVersion") or "unknown"
        update_mode = "generated"
    elif git_worktree:
        local_version = git_head or "unknown"
        update_mode = "git-worktree"
    elif not repo_url:
        local_version = "unknown"
        update_mode = "unmanaged"
    else:
        local_version = installed_base
        update_mode = "snapshot"

    entry = {
        "name": skill_path.name,
        "entryType": "single-skill",
        "path": str(skill_path),
        "repoUrl": repo_url,
        "source": source,
        "sourceType": source_type,
        "subpath": subpath,
        "generator": generator,
        "workflowId": workflow_id,
        "updatePolicy": LOCAL_ONLY_UPDATE_POLICY if local_only else None,
        "updateMode": update_mode,
        "installedBaseVersion": installed_base,
        "localVersion": local_version,
        "remoteVersion": None if local_only else _cached_remote_version(repo_url, previous_entry),
        "managed": bool(repo_url) and not local_only and metadata_error is None,
    }
    if metadata_error:
        entry["metadataError"] = metadata_error
    return entry


def _required_metadata_error(
    metadata: dict,
    fields: tuple[str, ...],
    label: str,
) -> Optional[str]:
    missing = [field for field in fields if not metadata.get(field)]
    if not missing:
        return None
    return f"Missing {label} metadata fields: {', '.join(missing)}."


def _installed_base_from_metadata(metadata: dict) -> tuple[Optional[str], Optional[str]]:
    if "sourceCommitSha" in metadata:
        return None, (
            "Legacy sourceCommitSha is unsupported; rename it to installedBaseVersion "
            "in .openskills.json."
        )
    raw_installed_base = metadata.get("installedBaseVersion")
    installed_base = normalize_git_commit(str(raw_installed_base)) if raw_installed_base else None
    if raw_installed_base and installed_base is None:
        return None, "installedBaseVersion must be a 12-40 character hexadecimal Git commit SHA."
    return (installed_base, None) if installed_base else (None, None)


def _load_skill_metadata(skill_path: Path) -> dict:
    metadata_path = skill_path / ".openskills.json"
    if not os.path.lexists(metadata_path):
        return {}
    if _is_filesystem_link(metadata_path) or not metadata_path.is_file():
        raise AgentSkillUpdaterError(f"Skill metadata must be a regular file: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise AgentSkillUpdaterError(f"Skill metadata must be a JSON object: {metadata_path}")
    string_fields = {
        "source",
        "sourceType",
        "repoUrl",
        "subpath",
        "generator",
        "workflowId",
        "installedBaseVersion",
        "generatedByVersion",
        "updatePolicy",
    }
    invalid = [key for key in string_fields if key in metadata and not isinstance(metadata[key], str)]
    if invalid:
        raise AgentSkillUpdaterError(
            f"Skill metadata fields must be strings in {metadata_path}: {', '.join(sorted(invalid))}"
        )
    update_policy = metadata.get("updatePolicy")
    if update_policy is not None and update_policy != LOCAL_ONLY_UPDATE_POLICY:
        raise AgentSkillUpdaterError(
            f"Unsupported updatePolicy in {metadata_path}: {update_policy}"
        )
    return metadata


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f"{path.name}.tmp-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _generated_version(skill_file: Path) -> Optional[str]:
    if not skill_file.exists():
        return None
    for line in skill_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("generatedBy:"):
            _, _, value = line.partition(":")
            return value.strip().strip('"')
    return None


def _repo_urls_match(left: str, right: str) -> bool:
    return bool(left and right) and canonical_repo_identity(left) == canonical_repo_identity(right)


def _cached_remote_version(repo_url: Optional[str], previous_entry: Optional[dict]) -> Optional[str]:
    if not repo_url or not previous_entry:
        return None
    previous_repo = previous_entry.get("repoUrl")
    remote_version = previous_entry.get("remoteVersion")
    if not isinstance(previous_repo, str) or not isinstance(remote_version, str):
        return None
    return remote_version if _repo_urls_match(repo_url, previous_repo) else None
