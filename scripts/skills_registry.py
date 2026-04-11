#!/usr/bin/env python3
"""Registry management for skills stored in ~/.agents/skills."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REGISTRY_FILENAME = ".skills-list.json"
SUPERPOWERS_REPO = "https://github.com/obra/superpowers"

KNOWN_SINGLE_SKILL_SOURCES: dict[str, dict[str, str]] = {
    "docx": {
        "source": "anthropics/skills",
        "sourceType": "git",
        "repoUrl": "https://github.com/anthropics/skills",
        "subpath": "skills/docx",
    },
    "pdf": {
        "source": "anthropics/skills",
        "sourceType": "git",
        "repoUrl": "https://github.com/anthropics/skills",
        "subpath": "skills/pdf",
    },
    "pptx": {
        "source": "anthropics/skills",
        "sourceType": "git",
        "repoUrl": "https://github.com/anthropics/skills",
        "subpath": "skills/pptx",
    },
    "skill-creator": {
        "source": "anthropics/skills",
        "sourceType": "git",
        "repoUrl": "https://github.com/anthropics/skills",
        "subpath": "skills/skill-creator",
    },
    "xlsx": {
        "source": "anthropics/skills",
        "sourceType": "git",
        "repoUrl": "https://github.com/anthropics/skills",
        "subpath": "skills/xlsx",
    },
    "humanizer": {
        "source": "blader/humanizer",
        "sourceType": "git",
        "repoUrl": "https://github.com/blader/humanizer",
        "subpath": ".",
    },
    "humanizer-zh": {
        "source": "op7418/Humanizer-zh",
        "sourceType": "git",
        "repoUrl": "https://github.com/op7418/Humanizer-zh",
        "subpath": ".",
    },
    "skills-updater": {
        "source": "yizhiyanhua-ai/skills-updater",
        "sourceType": "git",
        "repoUrl": "https://github.com/yizhiyanhua-ai/skills-updater",
        "subpath": ".",
    },
}


def get_agent_skills_dir() -> Path:
    return Path.home() / ".agents" / "skills"


def get_registry_path(skills_root: Optional[Path] = None) -> Path:
    return (skills_root or get_agent_skills_dir()) / REGISTRY_FILENAME


def load_registry(skills_root: Optional[Path] = None) -> dict:
    registry_path = get_registry_path(skills_root)
    if not registry_path.exists():
        return {
            "version": 1,
            "generatedAt": None,
            "skillsRoot": str(skills_root or get_agent_skills_dir()),
            "entries": {},
        }
    return json.loads(registry_path.read_text(encoding="utf-8"))


def save_registry(registry: dict, skills_root: Optional[Path] = None) -> Path:
    registry_path = get_registry_path(skills_root)
    registry_path.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return registry_path


def sync_registry(skills_root: Optional[Path] = None) -> dict:
    root = skills_root or get_agent_skills_dir()
    root.mkdir(parents=True, exist_ok=True)
    previous = load_registry(root).get("entries", {})
    entries: dict[str, dict] = {}

    for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if child.name.startswith(".") or not child.is_dir():
            continue

        detected = detect_registry_entry(child, previous.get(child.name))
        if detected is not None:
            entries[child.name] = detected

    registry = {
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "skillsRoot": str(root),
        "entries": entries,
    }
    save_registry(registry, root)
    return registry


def detect_registry_entry(skill_path: Path, previous_entry: Optional[dict] = None) -> Optional[dict]:
    if _is_skill_pack(skill_path):
        return _build_skill_pack_entry(skill_path, previous_entry)
    if (skill_path / "SKILL.md").exists():
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
    return value[:12] if value else None


def normalize_github_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    value = url.strip()
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value[len("git@github.com:") :]
    if value.endswith(".git"):
        value = value[:-4]
    return value


def _is_skill_pack(skill_path: Path) -> bool:
    return (skill_path / ".git").exists() and (skill_path / "skills").is_dir()


def _build_skill_pack_entry(skill_path: Path, previous_entry: Optional[dict]) -> dict:
    repo_url = get_git_remote_url(skill_path)
    if not repo_url and previous_entry:
        repo_url = previous_entry.get("repoUrl")
    if not repo_url and skill_path.name == "superpowers":
        repo_url = SUPERPOWERS_REPO

    source = _repo_source_name(repo_url) or skill_path.name
    return {
        "name": skill_path.name,
        "entryType": "skill-pack",
        "path": str(skill_path),
        "repoUrl": repo_url,
        "source": source,
        "sourceType": "git-pack",
        "localVersion": get_git_head_commit(skill_path) or "unknown",
        "remoteVersion": previous_entry.get("remoteVersion") if previous_entry else None,
        "managed": bool(repo_url),
        "autoUpdate": True,
    }


def _build_single_skill_entry(skill_path: Path, previous_entry: Optional[dict]) -> dict:
    metadata = _load_skill_metadata(skill_path)
    known = KNOWN_SINGLE_SKILL_SOURCES.get(skill_path.name, {})

    repo_url = metadata.get("repoUrl") or (previous_entry or {}).get("repoUrl") or known.get("repoUrl")
    source_type = metadata.get("sourceType") or (previous_entry or {}).get("sourceType") or known.get("sourceType")
    subpath = metadata.get("subpath") or (previous_entry or {}).get("subpath") or known.get("subpath")
    source = metadata.get("source") or (previous_entry or {}).get("source") or known.get("source") or _repo_source_name(repo_url) or "unknown"
    workflow_id = metadata.get("workflowId") or (previous_entry or {}).get("workflowId")
    generator = metadata.get("generator") or (previous_entry or {}).get("generator")

    local_version = "unknown"
    if source_type == "git-generated":
        local_version = _generated_version(skill_path / "SKILL.md") or metadata.get("generatedByVersion") or "unknown"
    else:
        local_version = metadata.get("sourceCommitSha") or (previous_entry or {}).get("localVersion") or "unknown"

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
        "localVersion": local_version,
        "remoteVersion": (previous_entry or {}).get("remoteVersion"),
        "managed": bool(repo_url),
        "autoUpdate": skill_path.name != "skills-updater",
    }
    _ensure_skill_metadata(skill_path, entry, metadata)
    return entry


def _load_skill_metadata(skill_path: Path) -> dict:
    metadata_path = skill_path / ".openskills.json"
    if not metadata_path.exists():
        return {}
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def _ensure_skill_metadata(skill_path: Path, entry: dict, current_metadata: dict) -> None:
    metadata = dict(current_metadata)
    changed = False

    for key in ("source", "sourceType", "repoUrl", "subpath", "generator", "workflowId"):
        value = entry.get(key)
        if value is None:
            continue
        if metadata.get(key) != value:
            metadata[key] = value
            changed = True

    if entry["sourceType"] == "git-generated":
        if entry["localVersion"] != "unknown" and metadata.get("generatedByVersion") != entry["localVersion"]:
            metadata["generatedByVersion"] = entry["localVersion"]
            changed = True
    else:
        if entry["localVersion"] != "unknown" and metadata.get("sourceCommitSha") != entry["localVersion"]:
            metadata["sourceCommitSha"] = entry["localVersion"]
            changed = True

    if changed:
        metadata.setdefault("installedAt", datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        metadata_path = skill_path / ".openskills.json"
        metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _generated_version(skill_file: Path) -> Optional[str]:
    if not skill_file.exists():
        return None
    for line in skill_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("generatedBy:"):
            _, _, value = line.partition(":")
            return value.strip().strip('"')
    return None


def _repo_source_name(repo_url: Optional[str]) -> Optional[str]:
    if not repo_url:
        return None
    normalized = normalize_github_url(repo_url)
    if normalized is None:
        return None
    parts = [part for part in normalized.split("/") if part]
    if len(parts) < 2:
        return normalized
    return "/".join(parts[-2:])
