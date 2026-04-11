#!/usr/bin/env python3
"""Utilities for checking and updating skills stored in ~/.agents/skills."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


DEFAULT_USER_AGENT = "skills-updater/1.0"
OPENSPEC_REPO = "https://github.com/Fission-AI/OpenSpec"
IGNORED_SIGNATURE_FILES = {".openskills.json"}


class AgentSkillUpdaterError(Exception):
    """Raised when agent skill update operations fail."""


@dataclass
class AgentSkillSource:
    name: str
    local_dir: Path
    source: Optional[str]
    source_type: Optional[str]
    repo_url: Optional[str]
    subpath: Optional[str]
    generator: Optional[str]
    workflow_id: Optional[str]
    metadata_path: Optional[Path] = None


@dataclass
class AgentSkillUpdate:
    source: AgentSkillSource
    staged_dir: Optional[Path]
    status: str
    local_version: str
    remote_version: Optional[str]
    error_message: Optional[str] = None


def get_agent_skills_dir() -> Path:
    return Path.home() / ".agents" / "skills"


def iter_agent_skill_dirs(skills_root: Optional[Path] = None) -> list[Path]:
    root = skills_root or get_agent_skills_dir()
    if not root.exists():
        return []

    skill_dirs: list[Path] = []
    for child in root.iterdir():
        if child.name.startswith("."):
            continue
        if child.is_dir() and (child / "SKILL.md").exists():
            skill_dirs.append(child)
    return sorted(skill_dirs, key=lambda item: item.name.lower())


def load_agent_skill_source(skill_dir: Path) -> AgentSkillSource:
    metadata_path = skill_dir / ".openskills.json"
    metadata: dict[str, object] = {}
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    return AgentSkillSource(
        name=skill_dir.name,
        local_dir=skill_dir,
        source=_as_optional_str(metadata.get("source")),
        source_type=_as_optional_str(metadata.get("sourceType")),
        repo_url=_as_optional_str(metadata.get("repoUrl")),
        subpath=_as_optional_str(metadata.get("subpath")),
        generator=_as_optional_str(metadata.get("generator")),
        workflow_id=_as_optional_str(metadata.get("workflowId")),
        metadata_path=metadata_path if metadata_path.exists() else None,
    )


def directory_signature(path: Path) -> str:
    records: list[str] = []
    for file_path in sorted(p for p in path.rglob("*") if p.is_file()):
        if file_path.name in IGNORED_SIGNATURE_FILES:
            continue
        file_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        records.append(f"{file_hash} {file_path.relative_to(path).as_posix()}")
    return "\n".join(records)


def stage_remote_skill(source: AgentSkillSource, stage_root: Path) -> Path:
    if source.source_type == "git-generated" and _is_openspec_source(source):
        return _stage_openspec_generated_skill(source, stage_root)
    if source.repo_url:
        return _stage_git_skill(source, stage_root)
    raise AgentSkillUpdaterError(f"Skill '{source.name}' is missing repo metadata.")


def resolve_skill_update(source: AgentSkillSource, stage_root: Path) -> AgentSkillUpdate:
    try:
        staged_dir = stage_remote_skill(source, stage_root)
    except Exception as exc:  # noqa: BLE001
        return AgentSkillUpdate(
            source=source,
            staged_dir=None,
            status="error",
            local_version=_read_local_version(source),
            remote_version=None,
            error_message=str(exc),
        )

    remote_version = _read_remote_version(source, staged_dir)
    if directory_signature(source.local_dir) == directory_signature(staged_dir):
        status = "up_to_date"
    else:
        status = "update_available"

    return AgentSkillUpdate(
        source=source,
        staged_dir=staged_dir,
        status=status,
        local_version=_read_local_version(source),
        remote_version=remote_version,
    )


def update_skill_from_staged(
    update: AgentSkillUpdate,
    backup_root: Path,
) -> None:
    if update.status != "update_available" or update.staged_dir is None:
        return

    backup_dir = backup_root / update.source.name
    backup_dir.parent.mkdir(parents=True, exist_ok=True)
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    shutil.copytree(update.source.local_dir, backup_dir)

    _clear_skill_directory(update.source.local_dir)
    for child in update.staged_dir.iterdir():
        destination = update.source.local_dir / child.name
        if child.is_dir():
            shutil.copytree(child, destination)
        else:
            shutil.copy2(child, destination)

    _refresh_metadata(update)


def refresh_skill_metadata_version(source: AgentSkillSource, remote_version: Optional[str]) -> None:
    if not remote_version:
        return
    update = AgentSkillUpdate(
        source=source,
        staged_dir=None,
        status="up_to_date",
        local_version=_read_local_version(source),
        remote_version=remote_version,
    )
    _refresh_metadata(update)


def make_backup_root(skills_root: Optional[Path] = None) -> Path:
    root = skills_root or get_agent_skills_dir()
    backup_root = root / f".backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup_root.mkdir(parents=True, exist_ok=False)
    return backup_root


def read_skill_sources(skills_root: Optional[Path] = None) -> list[AgentSkillSource]:
    return [load_agent_skill_source(skill_dir) for skill_dir in iter_agent_skill_dirs(skills_root)]


def fetch_remote_commit_sha(repo_url: str) -> Optional[str]:
    return _fetch_remote_commit_sha(repo_url)


def fetch_remote_package_version(repo_url: str, package_path: str = "package.json") -> Optional[str]:
    owner, repo = _parse_github_repo(repo_url)
    normalized_path = package_path.strip("/").replace("\\", "/")
    for ref in ("main", "master"):
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{normalized_path}"
        request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
                version = payload.get("version")
                if isinstance(version, str) and version:
                    return version
        except Exception:  # noqa: BLE001
            continue
    return None


def git_clone_repo(repo_url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", repo_url, str(destination)], cwd=destination.parent)


def git_pull_repo(repo_dir: Path) -> None:
    branch = git_default_branch(repo_dir)
    _run(["git", "-C", str(repo_dir), "pull", "--ff-only", "origin", branch], cwd=repo_dir)


def git_default_branch(repo_dir: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode == 0:
        value = result.stdout.strip()
        if value:
            return value.split("/")[-1]

    for branch in ("main", "master"):
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--verify", f"origin/{branch}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            return branch
    return "main"


def _stage_git_skill(source: AgentSkillSource, stage_root: Path) -> Path:
    repo_root, _ = _download_repo_root(source.repo_url, stage_root)
    skill_path = repo_root
    if source.subpath and source.subpath not in {".", ""}:
        skill_path = repo_root / Path(source.subpath.replace("\\", "/"))
    if not skill_path.exists():
        raise AgentSkillUpdaterError(f"Subpath '{source.subpath}' not found for '{source.name}'.")
    if not (skill_path / "SKILL.md").exists():
        raise AgentSkillUpdaterError(f"Remote skill '{source.name}' does not contain SKILL.md.")

    destination = stage_root / source.name
    shutil.copytree(skill_path, destination)
    return destination


def _stage_openspec_generated_skill(source: AgentSkillSource, stage_root: Path) -> Path:
    repo_root, _ = _download_repo_root(source.repo_url or OPENSPEC_REPO, stage_root)
    _run(["npm", "ci", "--ignore-scripts"], cwd=repo_root)
    _run(["node", "build.js"], cwd=repo_root)

    destination = stage_root / source.name
    destination.mkdir(parents=True, exist_ok=True)
    node_script = """
import fs from 'node:fs';
import path from 'node:path';
import { getSkillTemplates, generateSkillContent } from './dist/core/shared/skill-generation.js';

const outputDir = process.argv[1];
const dirName = process.argv[2];
const pkg = JSON.parse(fs.readFileSync('./package.json', 'utf8'));
const entry = getSkillTemplates().find((item) => item.dirName === dirName);
if (!entry) {
  throw new Error(`Unable to find OpenSpec skill template for ${dirName}`);
}
fs.mkdirSync(outputDir, { recursive: true });
fs.writeFileSync(
  path.join(outputDir, 'SKILL.md'),
  generateSkillContent(entry.template, pkg.version),
  'utf8'
);
"""
    _run(
        ["node", "--input-type=module", "-e", node_script, str(destination), source.name],
        cwd=repo_root,
    )
    return destination


def _download_repo_root(repo_url: str, temp_root: Path) -> tuple[Path, str]:
    owner, repo = _parse_github_repo(repo_url)
    for ref in ("main", "master"):
        try:
            return _download_repo_archive(owner, repo, ref, temp_root), ref
        except AgentSkillUpdaterError:
            continue
    raise AgentSkillUpdaterError(f"Unable to download GitHub archive for {repo_url}.")


def _download_repo_archive(owner: str, repo: str, ref: str, temp_root: Path) -> Path:
    archive_url = f"https://codeload.github.com/{owner}/{repo}/zip/{ref}"
    request = urllib.request.Request(archive_url, headers={"User-Agent": DEFAULT_USER_AGENT})
    temp_root.mkdir(parents=True, exist_ok=True)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise AgentSkillUpdaterError(f"Download failed for {owner}/{repo}@{ref}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise AgentSkillUpdaterError(f"Download failed for {owner}/{repo}@{ref}: {exc.reason}") from exc

    zip_path = temp_root / f"{repo}-{ref}.zip"
    zip_path.write_bytes(payload)
    extract_dir = temp_root / f"{repo}-{ref}"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extract_dir)

    extracted_roots = [item for item in extract_dir.iterdir() if item.is_dir()]
    if len(extracted_roots) != 1:
        raise AgentSkillUpdaterError(f"Unexpected archive layout for {owner}/{repo}@{ref}.")
    return extracted_roots[0]


def _parse_github_repo(repo_url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(repo_url)
    if parsed.netloc != "github.com":
        raise AgentSkillUpdaterError(f"Only GitHub URLs are supported: {repo_url}")
    parts = [part for part in parsed.path.replace(".git", "").split("/") if part]
    if len(parts) < 2:
        raise AgentSkillUpdaterError(f"Invalid GitHub repo URL: {repo_url}")
    return parts[0], parts[1]


def _clear_skill_directory(skill_dir: Path) -> None:
    for child in skill_dir.iterdir():
        if child.name == ".openskills.json":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _refresh_metadata(update: AgentSkillUpdate) -> None:
    metadata_path = update.source.local_dir / ".openskills.json"
    if not metadata_path.exists():
        return
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["installedAt"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if update.remote_version:
        if _is_openspec_source(update.source):
            metadata["generatedByVersion"] = update.remote_version
        else:
            metadata["sourceCommitSha"] = update.remote_version
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_local_version(source: AgentSkillSource) -> str:
    if _is_openspec_source(source):
        version = _read_generated_by_version(source.local_dir / "SKILL.md")
        return version or "unknown"

    if source.metadata_path and source.metadata_path.exists():
        metadata = json.loads(source.metadata_path.read_text(encoding="utf-8"))
        source_commit = _as_optional_str(metadata.get("sourceCommitSha"))
        if source_commit:
            return source_commit[:12]
    return "unknown"


def _read_remote_version(source: AgentSkillSource, staged_dir: Path) -> Optional[str]:
    if _is_openspec_source(source):
        return _read_generated_by_version(staged_dir / "SKILL.md")
    if source.repo_url:
        return _fetch_remote_commit_sha(source.repo_url)
    return None


def _read_generated_by_version(skill_file: Path) -> Optional[str]:
    if not skill_file.exists():
        return None
    for line in skill_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("generatedBy:"):
            _, _, value = line.partition(":")
            return value.strip().strip('"')
    return None


def _fetch_remote_commit_sha(repo_url: str) -> Optional[str]:
    owner, repo = _parse_github_repo(repo_url)
    for ref in ("main", "master"):
        api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}"
        request = urllib.request.Request(
            api_url,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "application/vnd.github.v3+json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
                sha = payload.get("sha")
                if isinstance(sha, str) and sha:
                    return sha[:12]
        except Exception:  # noqa: BLE001
            continue
    try:
        result = subprocess.run(
            _resolve_command(["git", "ls-remote", repo_url, "HEAD"]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            line = result.stdout.strip().splitlines()[0]
            sha = line.split()[0]
            if sha:
                return sha[:12]
    except Exception:  # noqa: BLE001
        pass
    return None


def _run(command: list[str], cwd: Path) -> None:
    result = subprocess.run(
        _resolve_command(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Command failed"
        raise AgentSkillUpdaterError(message)


def _resolve_command(command: list[str]) -> list[str]:
    if not command:
        return command

    executable = command[0]
    if sys.platform != "win32" or Path(executable).suffix:
        return command

    resolved = shutil.which(executable)
    if resolved:
        return [resolved, *command[1:]]

    for suffix in (".cmd", ".exe", ".bat"):
        candidate = f"{executable}{suffix}"
        resolved = shutil.which(candidate)
        if resolved:
            return [resolved, *command[1:]]
    return command


def _is_openspec_source(source: AgentSkillSource) -> bool:
    return (source.repo_url or "").rstrip("/") == OPENSPEC_REPO and source.source_type == "git-generated"


def _as_optional_str(value: object) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    return None
