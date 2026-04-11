#!/usr/bin/env python3
"""Install a new skill or skill-pack into ~/.agents/skills and refresh the registry."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))

from agent_skill_updater import (  # noqa: E402
    AgentSkillSource,
    fetch_remote_commit_sha,
    git_clone_repo,
    get_agent_skills_dir,
    stage_remote_skill,
)
from skills_registry import sync_registry  # noqa: E402
from stdio_utils import configure_windows_utf8_stdio  # noqa: E402


configure_windows_utf8_stdio()


def main() -> None:
    parser = argparse.ArgumentParser(description="Install a skill into ~/.agents/skills")
    parser.add_argument("--repo", required=True, help="GitHub repo in owner/repo format or full GitHub URL")
    parser.add_argument("--name", help="Installed directory name")
    parser.add_argument("--path", default=".", help="Relative skill path inside the repo")
    parser.add_argument("--type", choices=["single-skill", "skill-pack"], default="single-skill")
    parser.add_argument("--source-type", choices=["git", "git-generated"], default="git")
    parser.add_argument("--workflow-id", help="OpenSpec workflow id for generated skills")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    repo_url = _normalize_repo_url(args.repo)
    name = args.name or _derive_name(args.path, repo_url, args.type)
    destination = get_agent_skills_dir() / name

    if destination.exists():
        raise SystemExit(f"Destination already exists: {destination}")

    if args.type == "skill-pack":
        git_clone_repo(repo_url, destination)
    else:
        with tempfile.TemporaryDirectory(prefix="skills-updater-install-") as temp_dir:
            staged = stage_remote_skill(
                AgentSkillSource(
                    name=name,
                    local_dir=destination,
                    source=_repo_source_name(repo_url),
                    source_type=args.source_type,
                    repo_url=repo_url,
                    subpath=args.path,
                    generator="dist/core/shared/skill-generation.js" if args.source_type == "git-generated" else None,
                    workflow_id=args.workflow_id,
                    metadata_path=None,
                ),
                Path(temp_dir) / name,
            )
            shutil.copytree(staged, destination)
        _write_skill_metadata(destination, repo_url, args.path, args.source_type, args.workflow_id)

    registry = sync_registry()
    payload = {
        "name": name,
        "path": str(destination),
        "repoUrl": repo_url,
        "type": args.type,
        "registry": str(Path(registry["skillsRoot"]) / ".skills-list.json"),
    }

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Installed {name} to {destination}")
        print(f"Registry: {payload['registry']}")


def _normalize_repo_url(value: str) -> str:
    if value.startswith("https://github.com/"):
        return value.removesuffix(".git")
    return f"https://github.com/{value.strip().strip('/')}"


def _derive_name(path_value: str, repo_url: str, entry_type: str) -> str:
    if entry_type == "skill-pack":
        return repo_url.rstrip("/").split("/")[-1]
    cleaned = path_value.strip().strip("/").replace("\\", "/")
    if cleaned in {"", "."}:
        return repo_url.rstrip("/").split("/")[-1]
    return cleaned.split("/")[-1]


def _repo_source_name(repo_url: str) -> str:
    parts = [part for part in repo_url.split("/") if part]
    return "/".join(parts[-2:])


def _write_skill_metadata(
    destination: Path,
    repo_url: str,
    subpath: str,
    source_type: str,
    workflow_id: str | None,
) -> None:
    metadata = {
        "source": _repo_source_name(repo_url),
        "sourceType": source_type,
        "repoUrl": repo_url,
        "subpath": subpath,
        "installedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    if source_type == "git-generated":
        metadata["generator"] = "dist/core/shared/skill-generation.js"
        if workflow_id:
            metadata["workflowId"] = workflow_id
    else:
        remote_commit = fetch_remote_commit_sha(repo_url)
        if remote_commit:
            metadata["sourceCommitSha"] = remote_commit

    (destination / ".openskills.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
