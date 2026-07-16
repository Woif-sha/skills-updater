#!/usr/bin/env python3
"""Install a new skill or skill-pack into ~/.agents/skills and refresh the registry."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
import zipfile
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .agent_skill_updater import (
        AgentSkillSource,
        AgentSkillUpdaterError,
        OPENSPEC_REPO,
        _git_output,
        _git_tracked_control_paths,
        _validate_skill_payload,
        _write_json_atomic,
        fetch_remote_commit_sha,
        get_agent_skills_dir,
        git_clone_repo,
        is_git_worktree_skill,
        normalize_skill_subpath,
        sanitize_repo_url,
        skill_update_lock,
        stage_remote_skill,
    )
    from .skills_registry import sync_registry
    from .stdio_utils import JsonArgumentParser, configure_windows_utf8_stdio
else:
    sys.path.insert(0, str(Path(__file__).parent))
    from agent_skill_updater import (  # noqa: E402
        AgentSkillSource,
        AgentSkillUpdaterError,
        OPENSPEC_REPO,
        _git_output,
        _git_tracked_control_paths,
        _validate_skill_payload,
        _write_json_atomic,
        fetch_remote_commit_sha,
        get_agent_skills_dir,
        git_clone_repo,
        is_git_worktree_skill,
        normalize_skill_subpath,
        sanitize_repo_url,
        skill_update_lock,
        stage_remote_skill,
    )
    from skills_registry import sync_registry  # noqa: E402
    from stdio_utils import JsonArgumentParser, configure_windows_utf8_stdio  # noqa: E402


configure_windows_utf8_stdio()


class AgentSkillInstallError(AgentSkillUpdaterError):
    def __init__(self, message: str, *, installed: bool, path: Path | None = None):
        super().__init__(message)
        self.installed = installed
        self.path = path


def main() -> None:
    parser = JsonArgumentParser(
        description="Install a skill into ~/.agents/skills",
        json_error_factory=lambda message: [
            {
                "name": "__arguments__",
                "status": "error",
                "error_message": message,
                "installed": False,
            }
        ],
    )
    parser.add_argument("--repo", required=True, help="GitHub repo in owner/repo format or full GitHub URL")
    parser.add_argument("--name", help="Installed directory name")
    parser.add_argument("--path", default=".", help="Relative skill path inside the repo")
    parser.add_argument("--type", choices=["single-skill", "skill-pack"], default="single-skill")
    parser.add_argument("--source-type", choices=["git", "git-generated"], default="git")
    parser.add_argument("--workflow-id", help="OpenSpec workflow id for generated skills")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    repo_url = _normalize_repo_url(args.repo)
    subpath = normalize_skill_subpath(args.path)
    _validate_install_options(
        entry_type=args.type,
        source_type=args.source_type,
        repo_url=repo_url,
        subpath=subpath,
        workflow_id=args.workflow_id,
    )
    default_name = (
        f"openspec-{args.workflow_id}"
        if args.source_type == "git-generated"
        else _derive_name(args.path, repo_url, args.type)
    )
    name = _validate_install_name(args.name or default_name)
    skills_root = get_agent_skills_dir()
    skills_root.mkdir(parents=True, exist_ok=True)
    destination = skills_root / name

    with skill_update_lock(destination):
        if os.path.lexists(destination):
            raise AgentSkillUpdaterError(f"Destination already exists: {destination}")
        _install_atomically(
            destination=destination,
            repo_url=repo_url,
            subpath=subpath,
            entry_type=args.type,
            source_type=args.source_type,
            workflow_id=args.workflow_id,
        )

    try:
        registry = sync_registry()
    except (AgentSkillUpdaterError, OSError, ValueError) as exc:
        raise AgentSkillInstallError(
            f"Skill '{name}' was installed at {destination}, but the registry refresh failed: {exc}",
            installed=True,
            path=destination,
        ) from exc
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


def _install_atomically(
    *,
    destination: Path,
    repo_url: str,
    subpath: str,
    entry_type: str,
    source_type: str,
    workflow_id: str | None,
) -> None:
    committed = False
    rename_started = False
    install_payload: Path | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix=f".{destination.name}.install-",
            dir=destination.parent,
        ) as temp_dir:
            transaction_root = Path(temp_dir)
            install_payload = transaction_root / "payload"
            if entry_type == "skill-pack":
                git_clone_repo(repo_url, install_payload)
                if not is_git_worktree_skill(install_payload):
                    raise AgentSkillUpdaterError(
                        f"Cloned skill pack is not a root Git worktree: {repo_url}"
                    )
                _validate_skill_payload(install_payload, entry_type="skill-pack")
                installed_version = _git_output(
                    install_payload,
                    ["rev-parse", "--verify", "HEAD^{commit}"],
                )
                tracked_control_paths = _git_tracked_control_paths(
                    install_payload,
                    installed_version,
                )
                if tracked_control_paths:
                    raise AgentSkillUpdaterError(
                        "Skill pack tracks updater control entries: "
                        f"{', '.join(tracked_control_paths)}."
                    )
                _write_skill_metadata(
                    install_payload,
                    repo_url,
                    ".",
                    "git-pack",
                    None,
                    installed_version=installed_version,
                )
            else:
                remote_commit = (
                    fetch_remote_commit_sha(repo_url)
                    if source_type == "git"
                    else None
                )
                staged = stage_remote_skill(
                    AgentSkillSource(
                        name=destination.name,
                        local_dir=destination,
                        source=_repo_source_name(repo_url),
                        source_type=source_type,
                        repo_url=repo_url,
                        subpath=subpath,
                        generator=(
                            "dist/core/shared/skill-generation.js"
                            if source_type == "git-generated"
                            else None
                        ),
                        workflow_id=workflow_id,
                        metadata_path=None,
                        entry_type="single-skill",
                    ),
                    transaction_root / "download",
                    remote_commit,
                )
                shutil.copytree(staged, install_payload)
                _write_skill_metadata(
                    install_payload,
                    repo_url,
                    subpath,
                    source_type,
                    workflow_id,
                    installed_version=remote_commit,
                )

            if os.path.lexists(destination):
                raise AgentSkillUpdaterError(f"Destination appeared during install: {destination}")
            try:
                rename_started = True
                os.rename(install_payload, destination)
                committed = True
            except BaseException:
                committed = _install_move_committed(install_payload, destination)
                raise
    except BaseException as exc:
        if not committed and rename_started and install_payload is not None:
            committed = _install_move_committed(install_payload, destination)
        if committed:
            raise AgentSkillInstallError(
                f"Skill was installed at {destination}, but temporary install cleanup failed: {exc}",
                installed=True,
                path=destination,
            ) from exc
        raise


def _install_move_committed(install_payload: Path, destination: Path) -> bool:
    return not os.path.lexists(install_payload) and os.path.lexists(destination)


def _normalize_repo_url(value: str) -> str:
    candidate = value if "://" in value else f"https://github.com/{value.strip().strip('/')}"
    sanitized = sanitize_repo_url(candidate)
    parsed = urllib.parse.urlsplit(sanitized)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "github.com"
        or len(parts) != 2
        or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)
    ):
        raise AgentSkillUpdaterError("Install source must be a GitHub repository in owner/repo form.")
    return f"https://github.com/{parts[0]}/{parts[1].removesuffix('.git')}"


def _validate_install_name(value: str) -> str:
    name = value.strip()
    path = Path(name)
    if (
        not name
        or name in {".", ".."}
        or name.startswith(".")
        or path.is_absolute()
        or bool(path.anchor)
        or path.name != name
        or "/" in name
        or "\\" in name
        or "\0" in name
    ):
        raise AgentSkillUpdaterError(
            f"Installed Skill name must be one visible directory name: {value!r}"
        )
    return name


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


def _validate_install_options(
    *,
    entry_type: str,
    source_type: str,
    repo_url: str,
    subpath: str,
    workflow_id: str | None,
) -> None:
    if workflow_id and source_type != "git-generated":
        raise AgentSkillUpdaterError("--workflow-id requires --source-type git-generated.")
    if source_type == "git-generated":
        if entry_type != "single-skill":
            raise AgentSkillUpdaterError("Generated installation requires --type single-skill.")
        if repo_url != OPENSPEC_REPO:
            raise AgentSkillUpdaterError(
                f"Generated installation supports only {OPENSPEC_REPO}."
            )
        if subpath != ".":
            raise AgentSkillUpdaterError("Generated installation requires --path '.'.")
        if not workflow_id:
            raise AgentSkillUpdaterError("Generated installation requires --workflow-id.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", workflow_id):
            raise AgentSkillUpdaterError("--workflow-id must be one safe identifier.")
        return
    if entry_type != "skill-pack":
        return
    if subpath != ".":
        raise AgentSkillUpdaterError("Skill-pack installation requires --path '.'.")
    if source_type != "git":
        raise AgentSkillUpdaterError("Skill-pack installation requires --source-type git.")
    if workflow_id:
        raise AgentSkillUpdaterError("Skill-pack installation does not accept --workflow-id.")


def _write_skill_metadata(
    destination: Path,
    repo_url: str,
    subpath: str,
    source_type: str,
    workflow_id: str | None,
    *,
    installed_version: str | None,
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
        if installed_version:
            metadata["installedBaseVersion"] = installed_version

    _write_json_atomic(destination / ".openskills.json", metadata)


def _run_cli() -> None:
    try:
        main()
    except AgentSkillInstallError as exc:
        if "--json" in sys.argv[1:]:
            print(
                json.dumps(
                    [
                        {
                            "name": "__install__",
                            "status": "error",
                            "error_message": str(exc),
                            "installed": exc.installed,
                            "path": str(exc.path) if exc.path else None,
                        }
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except (AgentSkillUpdaterError, OSError, ValueError, zipfile.BadZipFile, shutil.Error) as exc:
        if "--json" in sys.argv[1:]:
            print(
                json.dumps(
                    [
                        {
                            "name": "__install__",
                            "status": "error",
                            "error_message": str(exc),
                            "installed": False,
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
