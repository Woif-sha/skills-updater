#!/usr/bin/env python3
"""Utilities for checking and updating skills stored in ~/.agents/skills."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Iterator, Optional


DEFAULT_USER_AGENT = "skills-updater/1.0"
OPENSPEC_REPO = "https://github.com/Fission-AI/OpenSpec"
LOCAL_ONLY_UPDATE_POLICY = "local-only"
SOURCE_PROVENANCE_FIELDS = (
    "source",
    "sourceType",
    "repoUrl",
    "subpath",
    "generator",
    "workflowId",
)
PAYLOAD_CONTRACTS = {
    "single-skill": ("SKILL.md", "blob"),
    "skill-pack": ("skills", "tree"),
}
REGISTRY_UPDATE_MODES = frozenset({"generated", "git-worktree", "local-only", "snapshot"})
SKILL_CONTROL_ENTRIES = frozenset({".git", ".openskills.json"})
SKILL_CONTROL_ENTRY_NAMES = frozenset(entry.casefold() for entry in SKILL_CONTROL_ENTRIES)
METADATA_TEMP_PREFIX = ".openskills.json.tmp-"
MINIMUM_GIT_SHA_LENGTH = 12
GIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{12,40}$")
TRANSACTION_STATE_FILENAME = "state.json"
TRANSACTION_MARKER_FILENAME = ".skills-updater-transaction"
TRANSACTION_STATE_VERSION = 4
SNAPSHOT_TRANSACTION_TYPE = "snapshot"
TRANSACTION_PHASE_PREPARED = "prepared"
TRANSACTION_PHASE_PREPARING = "preparing"
TRANSACTION_PHASE_MOVING_ORIGINAL = "moving_original"
TRANSACTION_PHASE_INSTALLING = "installing"
TRANSACTION_PHASE_COMMITTING_METADATA = "committing_metadata"
TRANSACTION_PHASE_COMMITTED = "committed"
TRANSACTION_PHASE_ROLLED_BACK = "rolled_back"
TRANSACTION_PHASES = frozenset(
    {
        TRANSACTION_PHASE_PREPARED,
        TRANSACTION_PHASE_PREPARING,
        TRANSACTION_PHASE_MOVING_ORIGINAL,
        TRANSACTION_PHASE_INSTALLING,
        TRANSACTION_PHASE_COMMITTING_METADATA,
        TRANSACTION_PHASE_COMMITTED,
        TRANSACTION_PHASE_ROLLED_BACK,
    }
)
GIT_TRANSACTION_STATE_VERSION = 3
GIT_TRANSACTION_TYPE = "git-worktree"
GIT_TRANSACTION_PHASE_PREPARED = "prepared"
GIT_TRANSACTION_PHASE_APPLYING = "applying"
GIT_TRANSACTION_PHASE_COMMITTED = "committed"
GIT_TRANSACTION_PHASE_ROLLED_BACK = "rolled_back"
GIT_TRANSACTION_PHASES = frozenset(
    {
        GIT_TRANSACTION_PHASE_PREPARED,
        GIT_TRANSACTION_PHASE_APPLYING,
        GIT_TRANSACTION_PHASE_COMMITTED,
        GIT_TRANSACTION_PHASE_ROLLED_BACK,
    }
)
METADATA_TRANSACTION_STATE_VERSION = 1
METADATA_TRANSACTION_TYPE = "metadata"
METADATA_TRANSACTION_PHASES = frozenset(
    {
        TRANSACTION_PHASE_PREPARED,
        GIT_TRANSACTION_PHASE_APPLYING,
        TRANSACTION_PHASE_COMMITTED,
        TRANSACTION_PHASE_ROLLED_BACK,
    }
)
COORDINATOR_TRANSACTION_STATE_VERSION = 1
COORDINATOR_TRANSACTION_TYPE = "coordinator"
METADATA_ONLY_TRANSACTION_KIND = "metadata-only"
COORDINATOR_PHASE_PREPARED = "prepared"
COORDINATOR_PHASE_CAPTURING_METADATA = "capturing_metadata"
COORDINATOR_PHASE_METADATA_CAPTURED = "metadata_captured"
COORDINATOR_PHASE_PUBLISHING_METADATA = "publishing_metadata"
COORDINATOR_PHASE_METADATA_PUBLISH_FAILED = "metadata_publish_failed"
COORDINATOR_PHASE_METADATA_PUBLISHED = "metadata_published"
COORDINATOR_PHASE_COMMITTED = "committed"
COORDINATOR_PHASE_ROLLED_BACK = "rolled_back"
COORDINATOR_PHASES = frozenset(
    {
        COORDINATOR_PHASE_PREPARED,
        COORDINATOR_PHASE_CAPTURING_METADATA,
        COORDINATOR_PHASE_METADATA_CAPTURED,
        COORDINATOR_PHASE_PUBLISHING_METADATA,
        COORDINATOR_PHASE_METADATA_PUBLISH_FAILED,
        COORDINATOR_PHASE_METADATA_PUBLISHED,
        COORDINATOR_PHASE_COMMITTED,
        COORDINATOR_PHASE_ROLLED_BACK,
    }
)
METADATA_PHASE_PREPARED = "prepared"
METADATA_PHASE_CAPTURING = "capturing"
METADATA_PHASE_CAPTURED = "captured"
METADATA_PHASE_PUBLISHING = "publishing"
METADATA_PHASE_PUBLISH_FAILED = "publish_failed"
METADATA_PHASE_PUBLISHED = "published"
METADATA_PHASES = frozenset(
    {
        METADATA_PHASE_PREPARED,
        METADATA_PHASE_CAPTURING,
        METADATA_PHASE_CAPTURED,
        METADATA_PHASE_PUBLISHING,
        METADATA_PHASE_PUBLISH_FAILED,
        METADATA_PHASE_PUBLISHED,
    }
)


class AgentSkillUpdaterError(Exception):
    """Raised when agent skill update operations fail."""


class AgentSkillUpdateCommittedError(AgentSkillUpdaterError):
    """Raised when an update committed but durable transaction cleanup failed."""

    def __init__(
        self,
        message: str,
        *,
        action: str,
        version: Optional[str],
    ) -> None:
        super().__init__(message)
        self.action = action
        self.version = version


class AgentSkillRecoveryUncertainError(AgentSkillUpdaterError):
    """Raised at compatibility seams when recovery needs structured operator action."""

    def __init__(self, outcome: "TransactionOutcome") -> None:
        super().__init__(outcome.error_message or "Transaction recovery is uncertain.")
        self.outcome = outcome


class AgentSkillMergeConflictError(AgentSkillUpdaterError):
    """Raised when local and remote payload edits require explicit resolution."""


def _payload_contract(entry_type: str) -> tuple[str, str]:
    try:
        return PAYLOAD_CONTRACTS[entry_type]
    except KeyError as exc:
        raise AgentSkillUpdaterError(f"Unsupported entryType: {entry_type}") from exc


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
    update_policy: Optional[str] = None
    entry_type: str = field(kw_only=True)

    def __post_init__(self) -> None:
        _payload_contract(self.entry_type)
        if self.update_policy not in {None, LOCAL_ONLY_UPDATE_POLICY}:
            raise AgentSkillUpdaterError(
                f"Unsupported updatePolicy for '{self.name}': {self.update_policy}"
            )


@dataclass
class AgentSkillUpdate:
    source: AgentSkillSource
    staged_dir: Optional[Path]
    status: str
    installed_base_version: str
    local_version: str
    remote_version: Optional[str]
    error_message: Optional[str] = None
    remote_observation: Optional["RemoteObservation"] = None


@dataclass
class GitWorktreeResult:
    status: str
    local_version: str
    remote_version: str
    relation: str
    working_tree_dirty: bool
    branch: str
    remote_ref: str
    ignored_conflicts: tuple[str, ...] = ()
    applied: bool = False
    action: str = "none"
    error_message: Optional[str] = None
    installed_state: str = "unchanged"
    diagnostic_journal: Optional[Path] = None
    cleanup_residue: Optional[Path] = None


@dataclass(frozen=True)
class SourceContract:
    source: Optional[str]
    source_type: Optional[str]
    repo_url: Optional[str]
    subpath: Optional[str]
    generator: Optional[str]
    workflow_id: Optional[str]


@dataclass(frozen=True)
class GitIdentityEvidence:
    local_revision: str
    branch: str
    remote_ref: str


@dataclass(frozen=True)
class RemoteObservation:
    revision: str
    version: str
    source_contract: SourceContract
    git_identity: Optional[GitIdentityEvidence] = None

    @classmethod
    def from_source(
        cls,
        source: AgentSkillSource,
        *,
        revision: str,
        version: str,
        git_identity: Optional[GitIdentityEvidence] = None,
    ) -> "RemoteObservation":
        return cls(
            revision=revision,
            version=version,
            source_contract=_source_contract(source),
            git_identity=git_identity,
        )


@dataclass(frozen=True)
class TransactionOutcome:
    name: str
    status: str
    installed_state: str
    applied: bool
    action: str
    version: Optional[str] = None
    error_message: Optional[str] = None
    diagnostic_journal: Optional[Path] = None
    cleanup_residue: Optional[Path] = None


@dataclass(frozen=True)
class _ControlMetadataEvidence:
    before_present: bool
    before_sha256: Optional[str]
    expected_sha256: str


@dataclass
class _MetadataJournal:
    skill_name: str
    skill_dir: Path
    phase: str
    target_version: Optional[str]
    evidence: _ControlMetadataEvidence
    writable_state: Optional[dict]


@dataclass(frozen=True)
class _MetadataPhaseProtocol:
    set_phase: Callable[[Path, dict, str], None]
    capturing: str
    captured: str
    publishing: str
    publish_failed: str
    published: str


@dataclass(frozen=True)
class _PendingGitMetadataUpdate:
    result: GitWorktreeResult
    installed_base: str


@dataclass(frozen=True)
class _PayloadNode:
    kind: str
    content: Optional[bytes] = None
    children: tuple[tuple[str, "_PayloadNode"], ...] = ()


ABSENT_PAYLOAD_NODE = _PayloadNode("absent")


def get_agent_skills_dir() -> Path:
    return Path.home() / ".agents" / "skills"


def load_agent_skill_source(skill_dir: Path) -> AgentSkillSource:
    _validate_skill_root(skill_dir)
    entry_type = detect_skill_entry_type(skill_dir)
    if entry_type is None:
        raise AgentSkillUpdaterError(f"Directory is not a skill or skill pack: {skill_dir}")
    metadata_path = skill_dir / ".openskills.json"
    metadata: dict[str, object] = {}
    if metadata_path.exists():
        metadata = _read_json_object(metadata_path, "Skill metadata")

    repo_url = _as_optional_str(metadata.get("repoUrl"))
    update_policy = _read_update_policy(metadata, metadata_path)
    return AgentSkillSource(
        name=skill_dir.name,
        local_dir=skill_dir,
        source=_as_optional_str(metadata.get("source")),
        source_type=_as_optional_str(metadata.get("sourceType")),
        repo_url=sanitize_repo_url(repo_url) if repo_url else None,
        subpath=_as_optional_str(metadata.get("subpath")),
        generator=_as_optional_str(metadata.get("generator")),
        workflow_id=_as_optional_str(metadata.get("workflowId")),
        metadata_path=metadata_path if metadata_path.exists() else None,
        update_policy=update_policy,
        entry_type=entry_type,
    )


def agent_skill_source_from_registry_entry(entry: dict) -> AgentSkillSource:
    if "entryType" not in entry:
        raise AgentSkillUpdaterError("Registry entry is missing entryType.")
    local_dir = Path(entry["path"])
    return AgentSkillSource(
        name=entry["name"],
        local_dir=local_dir,
        source=entry.get("source"),
        source_type=entry.get("sourceType"),
        repo_url=entry.get("repoUrl"),
        subpath=entry.get("subpath"),
        generator=entry.get("generator"),
        workflow_id=entry.get("workflowId"),
        metadata_path=local_dir / ".openskills.json",
        update_policy=entry.get("updatePolicy"),
        entry_type=entry["entryType"],
    )


def registry_entry_uses_git_worktree(entry: dict) -> bool:
    source = agent_skill_source_from_registry_entry(entry)
    update_mode = entry.get("updateMode")
    if not isinstance(update_mode, str) or update_mode not in REGISTRY_UPDATE_MODES:
        raise AgentSkillUpdaterError(
            f"Registry entry for '{source.name}' has unsupported updateMode: {update_mode}."
        )
    local_only = source.update_policy == LOCAL_ONLY_UPDATE_POLICY
    if (update_mode == "local-only") != local_only:
        raise AgentSkillUpdaterError(
            f"Registry updateMode and updatePolicy disagree for '{source.name}'."
        )
    if source.entry_type == "skill-pack" and update_mode not in {
        "git-worktree",
        "local-only",
    }:
        raise AgentSkillUpdaterError(
            f"Skill pack '{source.name}' cannot use updateMode={update_mode}."
        )
    if local_only:
        return False
    configured_for_git = update_mode == "git-worktree"
    is_git_worktree = is_git_worktree_skill(source.local_dir)
    if configured_for_git != is_git_worktree:
        raise AgentSkillUpdaterError(
            f"Registry updateMode for '{source.name}' does not match its filesystem: "
            f"updateMode={update_mode}, rootGitWorktree={str(is_git_worktree).lower()}."
        )
    return configured_for_git


def _read_json_object(path: Path, label: str) -> dict:
    if not os.path.lexists(path) or _is_filesystem_link(path) or not path.is_file():
        raise AgentSkillUpdaterError(f"{label} must be a regular file: {path}")
    return _decode_json_object(path.read_bytes(), path, label)


def _decode_json_object(content: bytes, path: Path, label: str) -> dict:
    payload = json.loads(content.decode("utf-8"))
    if not isinstance(payload, dict):
        raise AgentSkillUpdaterError(f"{label} must be a JSON object: {path}")
    return payload


def is_skill_payload_path(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return not any(_is_skill_control_part(part) for part in relative.parts)


def _is_skill_control_part(part: str) -> bool:
    normalized = part.casefold()
    return normalized in SKILL_CONTROL_ENTRY_NAMES or normalized.startswith(METADATA_TEMP_PREFIX)


def _validate_skill_root(root: Path) -> None:
    if _is_filesystem_link(root):
        raise AgentSkillUpdaterError(
            f"Skill root must not be a symlink or junction: {root}"
        )
    if not root.is_dir():
        raise AgentSkillUpdaterError(f"Skill root is not a directory: {root}")


def _raise_walk_error(error: OSError) -> None:
    raise error


def iter_skill_payload_files(root: Path) -> list[Path]:
    if not os.path.lexists(root):
        return []
    _validate_skill_root(root)

    files: list[Path] = []
    for current_root, directory_names, file_names in os.walk(
        root,
        topdown=True,
        onerror=_raise_walk_error,
        followlinks=False,
    ):
        current_path = Path(current_root)
        directory_names[:] = _payload_directory_names(current_path, directory_names, root)
        for name in file_names:
            file_path = current_path / name
            if is_skill_payload_path(file_path, root):
                if _is_filesystem_link(file_path):
                    raise AgentSkillUpdaterError(
                        f"Skill payload contains an unsupported file link: {file_path}"
                    )
                files.append(file_path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def iter_skill_payload_directories(root: Path) -> list[Path]:
    if not os.path.lexists(root):
        return []
    _validate_skill_root(root)

    directories: list[Path] = []
    for current_root, directory_names, _ in os.walk(
        root,
        topdown=True,
        onerror=_raise_walk_error,
        followlinks=False,
    ):
        current_path = Path(current_root)
        directory_names[:] = _payload_directory_names(current_path, directory_names, root)
        directories.extend(current_path / name for name in directory_names)
    return sorted(directories, key=lambda item: item.relative_to(root).as_posix())


def _payload_directory_names(current_path: Path, names: list[str], root: Path) -> list[str]:
    payload_directories: list[str] = []
    for name in names:
        directory_path = current_path / name
        if not is_skill_payload_path(directory_path, root):
            continue
        if _is_filesystem_link(directory_path):
            raise AgentSkillUpdaterError(
                f"Skill payload contains an unsupported directory link: {directory_path}"
            )
        payload_directories.append(name)
    return payload_directories


def _is_filesystem_link(path: Path) -> bool:
    return path.is_symlink() or bool(
        hasattr(os.path, "isjunction") and os.path.isjunction(path)
    )


def directory_signature(path: Path) -> str:
    digest = hashlib.sha256()
    for directory in iter_skill_payload_directories(path):
        _update_signature_record(
            digest,
            b"directory",
            directory.relative_to(path).as_posix(),
        )
    for file_path in iter_skill_payload_files(path):
        _update_signature_record(
            digest,
            b"file",
            file_path.relative_to(path).as_posix(),
            hashlib.sha256(file_path.read_bytes()).digest(),
        )
    return digest.hexdigest()


def _update_signature_record(
    digest: "hashlib._Hash",
    kind: bytes,
    relative_path: str,
    content_digest: bytes = b"",
) -> None:
    path_bytes = relative_path.encode("utf-8", "surrogatepass")
    digest.update(len(kind).to_bytes(2, "big"))
    digest.update(kind)
    digest.update(len(path_bytes).to_bytes(8, "big"))
    digest.update(path_bytes)
    digest.update(len(content_digest).to_bytes(2, "big"))
    digest.update(content_digest)


def is_git_worktree_skill(skill_dir: Path) -> bool:
    return os.path.lexists(skill_dir / ".git")


def _validate_git_control_entry(skill_dir: Path) -> Path:
    git_control = skill_dir / ".git"
    if not os.path.lexists(git_control):
        raise AgentSkillUpdaterError(f"Git control entry is missing: {git_control}")
    if _is_filesystem_link(git_control) or not (
        git_control.is_file() or git_control.is_dir()
    ):
        raise AgentSkillUpdaterError(
            f"Git control entry must be a regular file or directory: {git_control}"
        )
    return git_control


def detect_skill_entry_type(skill_dir: Path) -> Optional[str]:
    skills_dir = skill_dir / "skills"
    git_worktree = is_git_worktree_skill(skill_dir)
    if git_worktree:
        _validate_git_control_entry(skill_dir)
    if git_worktree and os.path.lexists(skills_dir):
        if _is_filesystem_link(skills_dir) or not skills_dir.is_dir():
            raise AgentSkillUpdaterError(
                f"Skill-pack content root must be a regular directory: {skills_dir}"
            )
        return "skill-pack"
    skill_file = skill_dir / "SKILL.md"
    if os.path.lexists(skill_file):
        if _is_filesystem_link(skill_file) or not skill_file.is_file():
            raise AgentSkillUpdaterError(
                f"Single-skill content root must be a regular file: {skill_file}"
            )
        return "single-skill"
    return None


def same_git_commit(left: Optional[str], right: Optional[str]) -> bool:
    normalized_left = normalize_git_commit(left)
    normalized_right = normalize_git_commit(right)
    if normalized_left is None or normalized_right is None:
        return False
    shorter, longer = sorted((normalized_left, normalized_right), key=len)
    return len(shorter) >= MINIMUM_GIT_SHA_LENGTH and longer.startswith(shorter)


def normalize_git_commit(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = value.strip().lower()
    return normalized if GIT_SHA_PATTERN.fullmatch(normalized) else None


def _require_git_commit(value: str, label: str) -> str:
    normalized = normalize_git_commit(value)
    if normalized is None:
        raise AgentSkillUpdaterError(
            f"{label} requires an exact 12-40 character Git commit."
        )
    return normalized


def versions_match(left: Optional[str], right: Optional[str]) -> bool:
    if not left or not right:
        return False
    return left == right or same_git_commit(left, right)


def normalize_skill_subpath(value: str) -> str:
    raw = value.strip().replace("\\", "/")
    if not raw:
        raise AgentSkillUpdaterError("Skill subpath must be explicit.")
    if "\0" in raw:
        raise AgentSkillUpdaterError("Skill subpath contains a null byte.")
    relative = PurePosixPath(raw)
    windows_path = PureWindowsPath(raw)
    if (
        relative.is_absolute()
        or bool(windows_path.drive)
        or bool(windows_path.root)
        or ".." in relative.parts
    ):
        raise AgentSkillUpdaterError(
            f"Skill subpath must stay inside the source repository: {value}"
        )
    normalized = relative.as_posix()
    return "." if normalized == "." else normalized


def stage_remote_skill(
    source: AgentSkillSource,
    stage_root: Path,
    remote_version: Optional[str] = None,
) -> Path:
    _require_remote_probe_ready(source)
    if source.source_type == "git-generated" and _is_openspec_source(source):
        return _stage_openspec_generated_skill(source, stage_root, remote_version)
    if not source.repo_url:
        raise AgentSkillUpdaterError(f"Skill '{source.name}' is missing repo metadata.")
    if not remote_version:
        raise AgentSkillUpdaterError(
            f"Skill '{source.name}' requires an explicit remote commit for staging."
        )
    return _stage_git_skill_at_ref(source, stage_root, remote_version)


def resolve_skill_update(
    source: AgentSkillSource,
    stage_root: Path,
    observation: Optional[RemoteObservation] = None,
) -> AgentSkillUpdate:
    if _is_local_only_source(source):
        local_version = _read_local_only_version(source)
        return AgentSkillUpdate(
            source=source,
            staged_dir=None,
            status="local_only",
            installed_base_version=local_version,
            local_version=local_version,
            remote_version=None,
        )
    if is_git_worktree_skill(source.local_dir):
        raise AgentSkillUpdaterError(
            f"Skill '{source.name}' is a Git worktree and must use the dedicated Git update path."
        )
    if observation is None:
        raise AgentSkillUpdaterError(
            f"Skill '{source.name}' requires an explicit Remote Observation."
        )
    _validate_remote_observation(source, observation)
    _require_remote_updates_enabled(source)
    installed_base_version = _read_installed_base_version(source)
    _require_remote_updates_enabled(source, installed_base_version)
    local_version = _read_local_version(source)
    try:
        if source.source_type == "git-generated" and _is_openspec_source(source):
            _require_remote_updates_enabled(source, installed_base_version)
            staged_dir = stage_remote_skill(source, stage_root, observation.revision)
            remote_version = _read_generated_by_version(staged_dir / "SKILL.md")
            if not remote_version:
                raise AgentSkillUpdaterError(
                    f"Generated skill '{source.name}' has no generatedBy version."
                )
            if remote_version != observation.version:
                raise AgentSkillUpdaterError(
                    f"Generated skill '{source.name}' version does not match its observed "
                    f"source revision: {remote_version} != {observation.version}."
                )
        elif source.repo_url:
            remote_version = observation.version
            _require_remote_updates_enabled(source, installed_base_version)
            if not remote_version:
                raise AgentSkillUpdaterError(f"Unable to resolve the remote commit for '{source.name}'.")
            staged_dir = _stage_git_skill_at_ref(
                source,
                stage_root,
                observation.revision,
            )
        else:
            raise AgentSkillUpdaterError(f"Skill '{source.name}' is missing repo metadata.")

        _require_remote_updates_enabled(source, installed_base_version)
        local_signature = directory_signature(source.local_dir)
        remote_signature = directory_signature(staged_dir)
    except (AgentSkillUpdaterError, OSError, ValueError) as exc:
        return AgentSkillUpdate(
            source=source,
            staged_dir=None,
            status="error",
            installed_base_version=installed_base_version,
            local_version=local_version,
            remote_version=None,
            error_message=str(exc),
        )

    if local_signature == remote_signature:
        status = "up_to_date"
    else:
        status = "update_available"

    return AgentSkillUpdate(
        source=source,
        staged_dir=staged_dir,
        status=status,
        installed_base_version=installed_base_version,
        local_version=local_version,
        remote_version=remote_version,
        remote_observation=observation,
    )


def update_skill_from_staged(
    update: AgentSkillUpdate,
    backup_root: Path,
) -> None:
    _require_remote_updates_enabled(update.source, update.installed_base_version)
    if update.status != "update_available" or update.staged_dir is None:
        raise AgentSkillUpdaterError(
            f"Skill '{update.source.name}' has no staged update to apply."
        )
    if not update.remote_version:
        raise AgentSkillUpdaterError(
            f"Skill '{update.source.name}' has no exact remote version to apply."
        )
    if is_git_worktree_skill(update.source.local_dir):
        raise AgentSkillUpdaterError(
            f"Skill '{update.source.name}' is a Git worktree; refusing file-level replacement."
        )

    local_signature = _validate_skill_payload(
        update.source.local_dir,
        entry_type=update.source.entry_type,
    )
    _validate_backup_root(backup_root)
    backup_dir = backup_root / update.source.name
    conflict_dir = backup_root / f"{update.source.name}.merge-conflicts"
    for destination in (backup_dir, conflict_dir):
        if os.path.lexists(destination):
            raise AgentSkillUpdaterError(
                f"Refusing to overwrite existing update evidence: {destination}"
            )

    with tempfile.TemporaryDirectory(
        prefix=f".{update.source.name}.merge-",
        dir=backup_root,
    ) as temp_dir:
        temp_root = Path(temp_dir)
        base_dir = _stage_update_base(update, temp_root / "base")
        merged_dir = temp_root / "merged"
        temporary_conflicts = temp_root / "conflicts"
        try:
            _merge_skill_directories(
                base_dir=base_dir,
                local_dir=update.source.local_dir,
                remote_dir=update.staged_dir,
                merged_dir=merged_dir,
                conflict_root=temporary_conflicts,
            )
        except AgentSkillMergeConflictError as exc:
            _require_remote_updates_enabled(update.source, update.installed_base_version)
            _rename_directory_exclusive(temporary_conflicts, conflict_dir)
            message = str(exc).replace(str(temporary_conflicts), str(conflict_dir))
            raise AgentSkillMergeConflictError(message) from exc

        _require_remote_updates_enabled(update.source, update.installed_base_version)
        _validate_skill_payload(merged_dir, entry_type=update.source.entry_type)
        _apply_payload_transaction(
            update,
            merged_dir,
            expected_original_signature=local_signature,
            backup_dir=backup_dir,
        )


def apply_observed_update(
    source: AgentSkillSource,
    observation: RemoteObservation,
    *,
    installed_base_version: str,
) -> TransactionOutcome:
    if not observation.revision or not observation.version:
        raise AgentSkillUpdaterError(
            f"Skill '{source.name}' requires an explicit Remote Observation."
        )
    _validate_remote_observation(source, observation)
    _require_remote_updates_enabled(source, installed_base_version)
    commit_state_validator = _metadata_commit_state_validator(source, observation)
    commit_state_validator(installed_base_version)
    with skill_update_lock(source.local_dir):
        _recover_skill_transactions_locked(source.local_dir)
        _require_remote_updates_enabled(source, installed_base_version)
        return _apply_metadata_only_transaction_locked(
            source,
            observation,
            installed_base_version,
            commit_state_validator,
        )


def refresh_skill_metadata_version(
    source: AgentSkillSource,
    installed_base_version: str,
    remote_version: Optional[str],
) -> bool:
    if not remote_version:
        raise AgentSkillUpdaterError(
            f"Skill '{source.name}' is missing the remote version required for metadata refresh."
        )
    outcome = apply_observed_update(
        source,
        RemoteObservation.from_source(
            source,
            revision=remote_version,
            version=remote_version,
        ),
        installed_base_version=installed_base_version,
    )
    return _legacy_metadata_result(outcome)


def _legacy_metadata_result(outcome: TransactionOutcome) -> bool:
    if outcome.status != "error":
        return outcome.applied
    if outcome.installed_state == "committed":
        raise AgentSkillUpdateCommittedError(
            outcome.error_message or "Metadata refresh committed with cleanup residue.",
            action=outcome.action,
            version=outcome.version,
        )
    raise AgentSkillUpdaterError(outcome.error_message or "Metadata refresh failed.")


def _validate_snapshot_metadata_refresh_state(
    source: AgentSkillSource,
    expected_installed_base: str,
) -> None:
    _require_remote_updates_enabled(source, expected_installed_base)
    if is_git_worktree_skill(source.local_dir):
        raise AgentSkillUpdaterError(
            f"Skill '{source.name}' is a Git worktree and requires Git-aware metadata refresh."
        )


def _source_contract(source: AgentSkillSource) -> SourceContract:
    return SourceContract(
        source=source.source,
        source_type=source.source_type,
        repo_url=sanitize_repo_url(source.repo_url) if source.repo_url else None,
        subpath=source.subpath,
        generator=source.generator,
        workflow_id=source.workflow_id,
    )


def _validate_remote_observation(
    source: AgentSkillSource,
    observation: RemoteObservation,
) -> None:
    if observation.source_contract != _source_contract(source):
        raise AgentSkillUpdaterError(
            f"Remote Observation source contract does not match Skill '{source.name}'."
        )
    if not re.fullmatch(r"[0-9a-fA-F]{40}", observation.revision):
        raise AgentSkillUpdaterError(
            f"Remote Observation for '{source.name}' requires an exact full revision."
        )


def _metadata_commit_state_validator(
    source: AgentSkillSource,
    observation: RemoteObservation,
) -> Callable[[str], None]:
    if observation.git_identity is None:
        return lambda expected_base: _validate_snapshot_metadata_refresh_state(
            source,
            expected_base,
        )
    identity = observation.git_identity
    result = GitWorktreeResult(
        status="up_to_date",
        local_version=identity.local_revision,
        remote_version=observation.revision,
        relation="equal",
        working_tree_dirty=False,
        branch=identity.branch,
        remote_ref=identity.remote_ref,
    )
    return lambda expected_base: _verify_git_apply_preconditions(
        source,
        result,
        expected_base,
    )


def make_backup_root(skills_root: Optional[Path] = None) -> Path:
    root = skills_root or get_agent_skills_dir()
    backup_root = root / f".backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup_root.mkdir(parents=True, exist_ok=False)
    return backup_root


def _validate_backup_root(backup_root: Path) -> None:
    if _is_filesystem_link(backup_root) or not backup_root.is_dir():
        raise AgentSkillUpdaterError(f"Backup root must be a regular directory: {backup_root}")


def _rename_directory_exclusive(source: Path, destination: Path) -> None:
    if not source.is_dir() or _is_filesystem_link(source):
        raise AgentSkillUpdaterError(f"Backup source must be a regular directory: {source}")
    if os.path.lexists(destination):
        raise AgentSkillUpdaterError(f"Refusing to overwrite existing directory: {destination}")
    os.rename(source, destination)


def _publish_payload_backup(
    original_dir: Path,
    staging_dir: Path,
    backup_dir: Path,
    expected_signature: str,
    entry_type: str,
) -> None:
    staging_dir.mkdir(exist_ok=False)
    _copy_directory_contents(original_dir, staging_dir)
    _validate_skill_payload(
        staging_dir,
        expected_signature,
        entry_type=entry_type,
    )
    _rename_directory_exclusive(staging_dir, backup_dir)


def fetch_remote_commit_sha(repo_url: str) -> str:
    return _fetch_remote_commit_sha(repo_url)


def fetch_source_remote_observation(source: AgentSkillSource) -> RemoteObservation:
    _require_remote_probe_ready(source)
    if not source.repo_url:
        raise AgentSkillUpdaterError(f"Skill '{source.name}' is missing repoUrl metadata.")
    revision = _fetch_remote_commit_sha(source.repo_url)
    _require_remote_probe_ready(source)
    if source.source_type == "git-generated" and _is_openspec_source(source):
        version = _fetch_remote_package_version_at_revision(
            source.repo_url,
            revision,
            "package.json",
        )
        _require_remote_probe_ready(source)
    else:
        version = revision
    return RemoteObservation.from_source(
        source,
        revision=revision,
        version=version,
    )


def fetch_source_remote_version(source: AgentSkillSource) -> str:
    return fetch_source_remote_observation(source).version


def _fetch_remote_package_version_at_revision(
    repo_url: str,
    revision: str,
    normalized_path: str,
) -> str:
    owner, repo = _parse_github_repo(repo_url)
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{revision}/{normalized_path}"
    request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AgentSkillUpdaterError(
            f"Package metadata request failed for {owner}/{repo}@{revision}: HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise AgentSkillUpdaterError(
            f"Package metadata request failed for {owner}/{repo}@{revision}: {exc.reason}"
        ) from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise AgentSkillUpdaterError(
            f"Invalid package metadata for {owner}/{repo}@{revision}: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("version"), str) or not payload["version"]:
        raise AgentSkillUpdaterError(
            f"Package metadata for {owner}/{repo}@{revision} has no version."
        )
    return payload["version"]


def git_clone_repo(repo_url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", repo_url, str(destination)], cwd=destination.parent)


def probe_git_worktree(source: AgentSkillSource) -> GitWorktreeResult:
    _require_remote_updates_enabled(source)
    with skill_update_lock(source.local_dir):
        _recover_skill_transactions_locked(source.local_dir)
        _require_remote_updates_enabled(source)
        return _probe_git_worktree_unlocked(source)


def _probe_git_worktree_unlocked(source: AgentSkillSource) -> GitWorktreeResult:
    _require_remote_updates_enabled(source)
    repo_dir = source.local_dir
    _validate_skill_root(repo_dir)
    if not is_git_worktree_skill(repo_dir):
        raise AgentSkillUpdaterError(f"Skill '{source.name}' is not a root Git worktree.")
    _validate_git_control_entry(repo_dir)
    if source.subpath != ".":
        raise AgentSkillUpdaterError(
            f"Git worktree skill '{source.name}' must use repository-root subpath '.'."
        )
    if not source.repo_url:
        raise AgentSkillUpdaterError(
            f"Git worktree skill '{source.name}' is missing repoUrl metadata."
        )
    expected_metadata_path = repo_dir / ".openskills.json"
    if source.metadata_path is None or not _same_path(
        source.metadata_path,
        expected_metadata_path,
    ):
        raise AgentSkillUpdaterError(
            f"Git worktree skill '{source.name}' must use metadata at {expected_metadata_path}."
        )
    _read_installed_base_version(source)
    _validate_skill_payload(repo_dir, entry_type=source.entry_type)

    top_level = Path(_git_output(repo_dir, ["rev-parse", "--show-toplevel"])).resolve()
    if os.path.normcase(str(top_level)) != os.path.normcase(str(repo_dir.resolve())):
        raise AgentSkillUpdaterError(
            f"Skill '{source.name}' has a .git control entry but is not the Git worktree root."
        )

    branch = _git_output(repo_dir, ["symbolic-ref", "--short", "HEAD"])
    remote_ref = _git_remote_ref(repo_dir, branch)
    _verify_git_source_configuration(source, branch, remote_ref)
    remote_branch = remote_ref.removeprefix("refs/remotes/origin/")
    _require_remote_probe_ready(source)
    _git_fetch_remote_branch(repo_dir, source.repo_url, remote_branch, remote_ref)
    _require_remote_updates_enabled(source)
    _verify_git_source_configuration(source, branch, remote_ref)
    local_version = _git_output(repo_dir, ["rev-parse", "--verify", "HEAD^{commit}"])
    remote_version = _git_output(repo_dir, ["rev-parse", "--verify", f"{remote_ref}^{{commit}}"])
    _git_require_payload_at_revision(repo_dir, local_version, source.entry_type)
    _git_require_payload_at_revision(repo_dir, remote_version, source.entry_type)
    tracked_control_paths = sorted(
        set(_git_tracked_control_paths(repo_dir, local_version))
        | set(_git_tracked_control_paths(repo_dir, remote_version))
    )
    if tracked_control_paths:
        raise AgentSkillUpdaterError(
            f"Git worktree '{source.name}' tracks updater control entries: "
            f"{', '.join(tracked_control_paths)}. Remove them from Git before updating."
        )
    relation = _git_commit_relation(repo_dir, local_version, remote_version)
    ignored_conflicts = (
        _git_ignored_payload_conflicts(repo_dir, local_version, remote_version)
        if relation == "behind"
        else ()
    )
    working_tree_dirty = _git_worktree_has_payload_changes(repo_dir) or bool(ignored_conflicts)
    if relation == "behind":
        status = "update_available"
        error_message = None
    elif relation in {"equal", "ahead"}:
        status = "up_to_date"
        error_message = None
    else:
        status = "error"
        error_message = (
            f"Git worktree '{source.name}' has diverged from {remote_ref}; "
            "merge or rebase explicitly before updating."
        )

    _require_remote_updates_enabled(source)
    return GitWorktreeResult(
        status=status,
        local_version=local_version,
        remote_version=remote_version,
        relation=relation,
        working_tree_dirty=working_tree_dirty,
        branch=branch,
        remote_ref=remote_ref,
        ignored_conflicts=ignored_conflicts,
        error_message=error_message,
    )


def update_git_worktree_skill(source: AgentSkillSource) -> GitWorktreeResult:
    _require_remote_updates_enabled(source)
    with skill_update_lock(source.local_dir):
        _recover_skill_transactions_locked(source.local_dir)
        _require_remote_updates_enabled(source)
        result = _update_git_worktree_skill_locked(source)
    if not isinstance(result, _PendingGitMetadataUpdate):
        return result
    observation = RemoteObservation.from_source(
        source,
        revision=result.result.remote_version,
        version=result.result.remote_version,
        git_identity=GitIdentityEvidence(
            local_revision=result.result.local_version,
            branch=result.result.branch,
            remote_ref=result.result.remote_ref,
        ),
    )
    outcome = apply_observed_update(
        source,
        observation,
        installed_base_version=result.installed_base,
    )
    result.result.status = outcome.status
    result.result.applied = outcome.applied
    result.result.action = outcome.action
    result.result.error_message = outcome.error_message
    result.result.installed_state = outcome.installed_state
    result.result.diagnostic_journal = outcome.diagnostic_journal
    result.result.cleanup_residue = outcome.cleanup_residue
    return result.result


def _update_git_worktree_skill_locked(
    source: AgentSkillSource,
) -> GitWorktreeResult | _PendingGitMetadataUpdate:
    result = _probe_git_worktree_unlocked(source)
    if result.status == "error":
        raise AgentSkillUpdaterError(result.error_message or f"Unable to update '{source.name}'.")

    installed_base = _read_installed_base_version(source)
    metadata_snapshot, original_metadata = _require_remote_updates_enabled(
        source,
        installed_base,
    )
    if result.relation in {"equal", "ahead"}:
        if installed_base != result.remote_version:
            return _PendingGitMetadataUpdate(result, installed_base)
        return result

    if result.working_tree_dirty:
        conflict_suffix = (
            f" Ignored paths would be overwritten: {', '.join(result.ignored_conflicts)}."
            if result.ignored_conflicts
            else ""
        )
        raise AgentSkillUpdaterError(
            f"Git worktree '{source.name}' has payload changes; refusing automatic fast-forward. "
            f"Commit, stash, or discard them explicitly first.{conflict_suffix}"
        )

    _git_require_payload_at_revision(
        source.local_dir,
        result.remote_version,
        source.entry_type,
    )
    _verify_git_apply_preconditions(source, result, installed_base)
    metadata_path = source.metadata_path
    if metadata_path is None or metadata_snapshot is None or original_metadata is None:
        raise AgentSkillUpdaterError(
            f"Git worktree '{source.name}' requires canonical metadata."
        )
    metadata_update = AgentSkillUpdate(
        source=source,
        staged_dir=None,
        status="up_to_date",
        installed_base_version=installed_base,
        local_version=result.local_version,
        remote_version=result.remote_version,
    )
    expected_metadata, metadata_changed = _build_refreshed_metadata(
        metadata_update,
        metadata_snapshot,
    )
    expected_metadata_bytes = (
        _json_payload_bytes(expected_metadata)
        if metadata_changed or original_metadata is None
        else original_metadata
    )
    transaction_root = Path(
        tempfile.mkdtemp(
            prefix=f".{source.name}.git-update-",
            dir=source.local_dir.parent,
        )
    )
    original_payload = transaction_root / "original"
    incoming_payload = transaction_root / "incoming"
    state: dict[str, object] = {}
    try:
        original_payload.mkdir()
        _copy_directory_contents(source.local_dir, original_payload)
        original_signature = _validate_skill_payload(
            original_payload,
            entry_type=source.entry_type,
        )
        _stage_git_revision_payload(
            source.local_dir,
            result.remote_version,
            incoming_payload,
            source.entry_type,
        )
        expected_signature = _validate_skill_payload(
            incoming_payload,
            entry_type=source.entry_type,
        )
        _prepare_transaction_metadata_files(
            transaction_root,
            original_metadata,
            expected_metadata_bytes,
        )
        state = {
            "version": GIT_TRANSACTION_STATE_VERSION,
            "transactionType": GIT_TRANSACTION_TYPE,
            "skillName": source.name,
            "skillDir": str(source.local_dir.resolve()),
            "entryType": source.entry_type,
            "phase": GIT_TRANSACTION_PHASE_PREPARED,
            "metadataPhase": METADATA_PHASE_PREPARED,
            "originalBranch": result.branch,
            "originalHead": result.local_version,
            "expectedHead": result.remote_version,
            "originalSignature": original_signature,
            "incomingSignature": expected_signature,
            "expectedSignature": expected_signature,
            "originalMetadataPresent": original_metadata is not None,
            "originalMetadataSha256": _sha256_bytes(original_metadata),
            "expectedMetadataPresent": True,
            "expectedMetadataSha256": _sha256_bytes(expected_metadata_bytes),
        }
        _write_json_atomic(transaction_root / TRANSACTION_STATE_FILENAME, state)
        _write_bytes_atomic(transaction_root / TRANSACTION_MARKER_FILENAME, b"1\n")
    except BaseException as exc:  # preparation must leave the worktree untouched
        try:
            _remove_transaction_tree(transaction_root)
        except (OSError, AgentSkillUpdaterError) as cleanup_exc:
            raise AgentSkillUpdaterError(
                f"Unable to prepare Git update for '{source.name}': {exc}. "
                f"Temporary snapshot cleanup also failed at {transaction_root}: {cleanup_exc}"
            ) from exc
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise AgentSkillUpdaterError(
            f"Unable to prepare Git update for '{source.name}': {exc}"
        ) from exc

    try:
        _verify_git_apply_preconditions(source, result, installed_base)
        _set_git_transaction_phase(
            transaction_root,
            state,
            GIT_TRANSACTION_PHASE_APPLYING,
        )
        branch_ref = f"refs/heads/{result.branch}"
        _require_remote_updates_enabled(source, installed_base)
        _run(
            [
                "git",
                "-C",
                str(source.local_dir),
                "update-ref",
                branch_ref,
                result.remote_version,
                result.local_version,
            ],
            cwd=source.local_dir,
        )
        current_branch = _git_output(
            source.local_dir,
            ["symbolic-ref", "--short", "HEAD"],
        )
        if current_branch != result.branch:
            try:
                _run(
                    [
                        "git",
                        "-C",
                        str(source.local_dir),
                        "update-ref",
                        branch_ref,
                        result.local_version,
                        result.remote_version,
                    ],
                    cwd=source.local_dir,
                )
            except AgentSkillUpdaterError as rollback_exc:
                raise AgentSkillUpdaterError(
                    f"Git branch changed while applying update for '{source.name}', and the "
                    f"explicit branch ref could not be restored: {rollback_exc}"
                ) from rollback_exc
            raise AgentSkillUpdaterError(
                f"Git branch changed while applying update for '{source.name}': "
                f"{result.branch} -> {current_branch}."
            )
        _run(
            [
                "git",
                "-C",
                str(source.local_dir),
                "read-tree",
                "--reset",
                "-u",
                result.remote_version,
            ],
            cwd=source.local_dir,
        )
        committed_signature = _validate_skill_payload(
            source.local_dir,
            entry_type=source.entry_type,
        )
        updated_head = _validate_git_worktree_revision(
            source.local_dir,
            result.branch,
            result.remote_version,
            committed_signature,
            source.entry_type,
        )
        _set_git_transaction_expected_signature(
            transaction_root,
            state,
            committed_signature,
        )
        _require_remote_updates_enabled(source, installed_base)
        _verify_git_source_configuration(source, result.branch, result.remote_ref)
        _validate_transaction_metadata(source.local_dir, state, expected=False)
        _commit_transaction_metadata(
            transaction_root,
            state,
            metadata_path,
            expected_metadata_bytes,
            original_metadata,
            phases=LEGACY_METADATA_PHASES,
        )
        _validate_transaction_metadata(source.local_dir, state, expected=True)
        _validate_git_transaction_worktree(source.local_dir, state, expected=True)
        _verify_git_source_configuration(source, result.branch, result.remote_ref)
        _require_remote_updates_enabled(source, result.remote_version)
        _set_git_transaction_phase(
            transaction_root,
            state,
            GIT_TRANSACTION_PHASE_COMMITTED,
        )
        _validate_git_transaction_worktree(source.local_dir, state, expected=True)
    except BaseException as exc:  # clean fast-forward has an explicit rollback contract
        if state.get("phase") == GIT_TRANSACTION_PHASE_PREPARED:
            try:
                _remove_transaction_tree(transaction_root)
            except (OSError, AgentSkillUpdaterError) as cleanup_exc:
                raise AgentSkillUpdaterError(
                    f"Git update for '{source.name}' was cancelled before apply, but snapshot "
                    f"cleanup failed at {transaction_root}: {cleanup_exc}"
                ) from exc
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise AgentSkillUpdaterError(
                f"Git update for '{source.name}' was cancelled before apply: {exc}"
            ) from exc
        try:
            recovery_path = _rollback_git_fast_forward(
                source.local_dir,
                result.local_version,
                result.remote_version,
                result.branch,
                original_payload,
                incoming_payload,
                transaction_root,
                state["originalSignature"],
                state["incomingSignature"],
                source.entry_type,
            )
            metadata_recovery = _rollback_transaction_metadata(
                transaction_root,
                _decode_legacy_metadata_phase(state["metadataPhase"]),
                metadata_path,
                original_metadata,
                expected_metadata_bytes,
            )
            if metadata_recovery is None:
                _validate_transaction_metadata(source.local_dir, state, expected=False)
            else:
                state["recoveryPath"] = str(metadata_recovery)
            _set_git_transaction_phase(
                transaction_root,
                state,
                GIT_TRANSACTION_PHASE_ROLLED_BACK,
            )
        except BaseException as rollback_exc:  # preserve both failures
            raise AgentSkillUpdaterError(
                f"Git update failed for '{source.name}': {exc}. Rollback also failed: {rollback_exc}. "
                f"Recovery snapshots remain at {transaction_root}."
            ) from exc
        recovery_path = recovery_path or metadata_recovery
        try:
            _remove_transaction_tree(transaction_root)
        except (OSError, AgentSkillUpdaterError) as cleanup_exc:
            raise AgentSkillUpdaterError(
                f"Git update failed for '{source.name}'; HEAD, payload, and metadata were restored, "
                f"but snapshot cleanup failed at {transaction_root}: {cleanup_exc}"
            ) from exc
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        recovery_suffix = (
            f" Concurrent data was preserved at {recovery_path}."
            if recovery_path is not None
            else ""
        )
        raise AgentSkillUpdaterError(
            f"Git update failed for '{source.name}'; HEAD, payload, and metadata were restored: "
            f"{exc}.{recovery_suffix}"
        ) from exc

    try:
        _remove_transaction_tree(transaction_root)
    except (OSError, AgentSkillUpdaterError) as cleanup_exc:
        raise AgentSkillUpdateCommittedError(
            f"Git update for '{source.name}' committed, but temporary snapshot cleanup failed "
            f"at {transaction_root}: {cleanup_exc}",
            action="fast_forwarded",
            version=updated_head,
        ) from cleanup_exc

    result.status = "up_to_date"
    result.local_version = updated_head
    result.relation = "equal"
    result.working_tree_dirty = _git_worktree_has_payload_changes(source.local_dir)
    result.applied = True
    result.action = "fast_forwarded"
    return result


def _git_remote_ref(repo_dir: Path, branch: str) -> str:
    configured_remote = _git_config_value(repo_dir, f"branch.{branch}.remote")
    configured_merge = _git_config_value(repo_dir, f"branch.{branch}.merge")
    if not configured_remote or not configured_merge:
        raise AgentSkillUpdaterError(
            f"Current branch '{branch}' requires an explicit upstream; refusing update."
        )
    if configured_remote != "origin":
        raise AgentSkillUpdaterError(
            f"Current branch '{branch}' tracks non-origin remote; refusing update."
        )
    prefix = "refs/heads/"
    if not configured_merge.startswith(prefix) or configured_merge == prefix:
        raise AgentSkillUpdaterError(
            f"Current branch '{branch}' has an invalid upstream branch; refusing update."
        )
    return f"refs/remotes/origin/{configured_merge[len(prefix):]}"


def _git_config_value(repo_dir: Path, key: str) -> Optional[str]:
    result = subprocess.run(
        _resolve_command(["git", "-C", str(repo_dir), "config", "--get", key]),
        cwd=repo_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode == 1 and not result.stdout.strip():
        return None
    if result.returncode != 0:
        message = result.stderr.strip() or f"Unable to read Git config key {key}."
        raise AgentSkillUpdaterError(message)
    return result.stdout.strip() or None


def _git_fetch_remote_branch(
    repo_dir: Path,
    repo_url: str,
    branch: str,
    remote_ref: str,
) -> None:
    refspec = f"+refs/heads/{branch}:{remote_ref}"
    _run(
        ["git", "-C", str(repo_dir), "fetch", "--quiet", "--no-tags", repo_url, refspec],
        cwd=repo_dir,
    )


def _git_commit_relation(repo_dir: Path, local_version: str, remote_version: str) -> str:
    if same_git_commit(local_version, remote_version):
        return "equal"
    if _git_is_ancestor(repo_dir, local_version, remote_version):
        return "behind"
    if _git_is_ancestor(repo_dir, remote_version, local_version):
        return "ahead"
    return "diverged"


def _git_is_ancestor(repo_dir: Path, ancestor: str, descendant: str) -> bool:
    _validate_git_control_entry(repo_dir)
    result = subprocess.run(
        _resolve_command(
            ["git", "-C", str(repo_dir), "merge-base", "--is-ancestor", ancestor, descendant]
        ),
        cwd=repo_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    message = result.stderr.strip() or result.stdout.strip() or "git merge-base failed"
    raise AgentSkillUpdaterError(message)


def _git_worktree_has_payload_changes(repo_dir: Path) -> bool:
    _validate_git_control_entry(repo_dir)
    result = subprocess.run(
        _resolve_command(
            ["git", "-C", str(repo_dir), "status", "--porcelain=v1", "-z", "--untracked-files=all"]
        ),
        cwd=repo_dir,
        capture_output=True,
        text=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip() or "git status failed"
        raise AgentSkillUpdaterError(message)

    records = result.stdout.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4:
            raise AgentSkillUpdaterError(f"Unexpected git status record in '{repo_dir}'.")
        status = record[:2].decode("ascii", errors="replace")
        paths = [os.fsdecode(record[3:])]
        if "R" in status or "C" in status:
            if index >= len(records):
                raise AgentSkillUpdaterError(f"Incomplete git rename record in '{repo_dir}'.")
            paths.append(os.fsdecode(records[index]))
            index += 1
        if any(is_skill_payload_path(repo_dir / Path(path.replace("/", os.sep)), repo_dir) for path in paths):
            return True
    return False


def _git_ignored_payload_conflicts(
    repo_dir: Path,
    local_version: str,
    remote_version: str,
) -> tuple[str, ...]:
    ignored_paths = _git_path_list(
        repo_dir,
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
    )
    changed_paths = _git_path_list(
        repo_dir,
        ["diff", "--name-only", "-z", local_version, remote_version],
    )
    payload_ignored = [
        path
        for path in ignored_paths
        if is_skill_payload_path(repo_dir / Path(path.replace("/", os.sep)), repo_dir)
    ]
    payload_changed = [
        path
        for path in changed_paths
        if is_skill_payload_path(repo_dir / Path(path.replace("/", os.sep)), repo_dir)
    ]
    collisions = {
        ignored
        for ignored in payload_ignored
        if any(_git_paths_overlap(ignored, changed) for changed in payload_changed)
    }
    return tuple(sorted(collisions))


def _git_tracked_control_paths(repo_dir: Path, revision: str) -> tuple[str, ...]:
    tracked_paths = _git_path_list(
        repo_dir,
        ["ls-tree", "-r", "--name-only", "-z", revision],
    )
    return tuple(
        sorted(
            path
            for path in tracked_paths
            if not is_skill_payload_path(
                repo_dir / Path(path.replace("/", os.sep)),
                repo_dir,
            )
        )
    )


def _git_path_list(repo_dir: Path, arguments: list[str]) -> list[str]:
    _validate_git_control_entry(repo_dir)
    result = subprocess.run(
        _resolve_command(["git", "-C", str(repo_dir), *arguments]),
        cwd=repo_dir,
        capture_output=True,
        text=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip() or "Git path query failed"
        raise AgentSkillUpdaterError(message)
    return [os.fsdecode(value).replace("\\", "/") for value in result.stdout.split(b"\0") if value]


def _git_paths_overlap(left: str, right: str) -> bool:
    left_parts = tuple(part.casefold() for part in PurePosixPath(left).parts)
    right_parts = tuple(part.casefold() for part in PurePosixPath(right).parts)
    common_length = min(len(left_parts), len(right_parts))
    return left_parts[:common_length] == right_parts[:common_length]


def _verify_git_apply_preconditions(
    source: AgentSkillSource,
    result: GitWorktreeResult,
    installed_base: str,
) -> None:
    _require_remote_updates_enabled(source, installed_base)
    repo_dir = source.local_dir
    current_branch = _git_output(repo_dir, ["symbolic-ref", "--short", "HEAD"])
    if current_branch != result.branch:
        raise AgentSkillUpdaterError(
            f"Git branch changed during update for '{source.name}': "
            f"{result.branch} -> {current_branch}."
        )
    _verify_git_source_configuration(source, current_branch, result.remote_ref)

    current_head = _git_output(repo_dir, ["rev-parse", "--verify", "HEAD^{commit}"])
    if not same_git_commit(current_head, result.local_version):
        raise AgentSkillUpdaterError(
            f"Git HEAD changed during update for '{source.name}': "
            f"{result.local_version} -> {current_head}."
        )
    current_remote = _git_output(
        repo_dir,
        ["rev-parse", "--verify", f"{result.remote_ref}^{{commit}}"],
    )
    if not same_git_commit(current_remote, result.remote_version):
        raise AgentSkillUpdaterError(
            f"Remote tracking ref changed during update for '{source.name}'; rerun the update."
        )
    if _git_worktree_has_payload_changes(repo_dir):
        raise AgentSkillUpdaterError(
            f"Git worktree '{source.name}' changed during update; refusing automatic fast-forward."
        )
    conflicts = _git_ignored_payload_conflicts(repo_dir, current_head, current_remote)
    if conflicts:
        raise AgentSkillUpdaterError(
            f"Ignored paths would be overwritten in '{source.name}': {', '.join(conflicts)}."
        )


def _verify_git_source_configuration(
    source: AgentSkillSource,
    branch: str,
    expected_remote_ref: str,
) -> None:
    if source.repo_url is None:
        raise AgentSkillUpdaterError(f"Git skill '{source.name}' is missing repoUrl metadata.")
    origin_url = _git_output(source.local_dir, ["config", "--get", "remote.origin.url"])
    if canonical_repo_identity(origin_url) != canonical_repo_identity(source.repo_url):
        raise AgentSkillUpdaterError(
            f"Git origin for '{source.name}' does not match metadata repoUrl: "
            f"{sanitize_repo_url(origin_url)} != {sanitize_repo_url(source.repo_url)}."
        )
    if _git_remote_ref(source.local_dir, branch) != expected_remote_ref:
        raise AgentSkillUpdaterError(f"Git upstream changed during update for '{source.name}'.")


def _rollback_git_fast_forward(
    repo_dir: Path,
    original_head: str,
    expected_head: str,
    original_branch: str,
    original_payload: Path,
    incoming_payload: Path,
    transaction_root: Path,
    expected_original_signature: str,
    expected_incoming_signature: str,
    entry_type: str,
) -> Optional[Path]:
    original_signature = _validate_skill_payload(
        original_payload,
        entry_type=entry_type,
    )
    if original_signature != expected_original_signature:
        raise AgentSkillUpdaterError(
            f"Original Git recovery snapshot is incomplete at {transaction_root}."
        )
    incoming_signature = _validate_skill_payload(
        incoming_payload,
        entry_type=entry_type,
    )
    if incoming_signature != expected_incoming_signature:
        raise AgentSkillUpdaterError(
            f"Incoming Git recovery snapshot is incomplete at {transaction_root}."
        )
    failed_root = _transaction_subdirectory(transaction_root, "failed", create=True)
    existing_attempts = _transaction_attempt_directories(failed_root)
    _safe_recovery_directory(repo_dir, transaction_root, create=False)
    recovery_path: Optional[Path] = None
    for source_dir in [*existing_attempts, repo_dir]:
        preserved = _preserve_unexpected_payload(
            transaction_root,
            source_dir,
            original_payload,
            incoming_payload,
            repo_dir,
        )
        recovery_path = preserved or recovery_path

    branch_ref = f"refs/heads/{original_branch}"
    current_ref = _git_output(repo_dir, ["rev-parse", "--verify", f"{branch_ref}^{{commit}}"])
    if same_git_commit(current_ref, expected_head):
        _run(
            ["git", "-C", str(repo_dir), "update-ref", branch_ref, original_head, expected_head],
            cwd=repo_dir,
        )
    elif not same_git_commit(current_ref, original_head):
        raise AgentSkillUpdaterError(
            f"Git branch ref changed after the interrupted update: {current_ref}."
        )
    _validate_git_rollback_ref(repo_dir, original_head, original_branch)
    quarantine_dir = Path(tempfile.mkdtemp(prefix="attempt-", dir=failed_root))
    _move_payload_files(repo_dir, quarantine_dir)
    _prune_empty_payload_directories(repo_dir)
    preserved = _preserve_unexpected_payload(
        transaction_root,
        quarantine_dir,
        original_payload,
        incoming_payload,
        repo_dir,
    )
    recovery_path = preserved or recovery_path
    durable_recovery_path = _safe_recovery_directory(
        repo_dir,
        transaction_root,
        create=False,
    )
    if (
        recovery_path is None
        and durable_recovery_path is not None
        and any(durable_recovery_path.iterdir())
    ):
        recovery_path = durable_recovery_path
    _install_payload_without_overwrite(original_payload, repo_dir)
    _validate_skill_payload(repo_dir, original_signature, entry_type=entry_type)
    # The explicit ref compare-and-swap cannot redirect a concurrently checked-out branch.
    # read-tree updates the index/worktree without changing any ref.
    _validate_git_rollback_ref(repo_dir, original_head, original_branch)
    _run(
        ["git", "-C", str(repo_dir), "read-tree", "--reset", "-u", original_head],
        cwd=repo_dir,
    )
    _validate_git_rollback_ref(repo_dir, original_head, original_branch)
    restored_head = _git_output(repo_dir, ["rev-parse", "--verify", "HEAD^{commit}"])
    if not same_git_commit(restored_head, original_head):
        raise AgentSkillUpdaterError(
            f"Git rollback verification failed: {restored_head} != {original_head}."
        )
    if _git_worktree_has_payload_changes(repo_dir):
        raise AgentSkillUpdaterError(f"Git rollback left payload changes in '{repo_dir}'.")
    return recovery_path


def _validate_git_rollback_ref(
    repo_dir: Path,
    original_head: str,
    original_branch: str,
) -> None:
    current_branch = _git_output(repo_dir, ["symbolic-ref", "--short", "HEAD"])
    if current_branch != original_branch:
        raise AgentSkillUpdaterError(
            f"Git rollback requires branch '{original_branch}', but '{current_branch}' is checked out."
        )
    current_head = _git_output(repo_dir, ["rev-parse", "--verify", "HEAD^{commit}"])
    if not same_git_commit(current_head, original_head):
        raise AgentSkillUpdaterError(
            f"Git HEAD changed after the interrupted update: {current_head}. "
            "Recovery snapshots were preserved for manual inspection."
        )


def _stage_git_revision_payload(
    repo_dir: Path,
    revision: str,
    destination: Path,
    entry_type: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive_path = destination.parent / "incoming.zip"
    extract_root = destination.parent / "incoming-archive"
    _run(
        [
            "git",
            "-C",
            str(repo_dir),
            "archive",
            "--format=zip",
            "--output",
            str(archive_path),
            revision,
        ],
        cwd=repo_dir,
    )
    extract_root.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        _validate_zip_members(archive, extract_root)
        archive.extractall(extract_root)
    destination.mkdir(parents=True, exist_ok=False)
    _copy_directory_contents(extract_root, destination)
    _validate_skill_payload(destination, entry_type=entry_type)


def _preserve_metadata_content(
    transaction_root: Path,
    skill_dir: Path,
    content: Optional[bytes],
) -> Path:
    recovery_root = _safe_recovery_directory(
        skill_dir,
        transaction_root,
        create=True,
    )
    metadata_recovery = _safe_recovery_subdirectory(
        recovery_root,
        Path("control-metadata"),
    )
    if content is None:
        name = ".openskills.json.concurrent-deletion"
        payload = b"Metadata was deleted concurrently during an update.\n"
    else:
        digest = hashlib.sha256(content).hexdigest()[:16]
        name = f".openskills.json.concurrent-{digest}.json"
        payload = content
    destination = _safe_recovery_path(metadata_recovery, Path(name))
    if destination.exists():
        if not destination.is_file() or destination.read_bytes() != payload:
            raise AgentSkillUpdaterError(
                f"Metadata recovery destination already contains different data: {destination}"
            )
    else:
        with destination.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    return recovery_root


def _rollback_transaction_metadata(
    transaction_root: Path,
    metadata_phase: str,
    metadata_path: Path,
    original_content: Optional[bytes],
    expected_content: bytes,
) -> Optional[Path]:
    known_contents = (original_content, expected_content)
    rollback_capture_proves_absence = False

    if os.path.lexists(metadata_path):
        if _is_filesystem_link(metadata_path) or not metadata_path.is_file():
            raise AgentSkillUpdaterError(
                f"Metadata path changed to a non-file during rollback: {metadata_path}"
            )
        current = metadata_path.read_bytes()
        if current == original_content:
            return None
        if current not in known_contents:
            return _preserve_metadata_content(
                transaction_root,
                metadata_path.parent,
                current,
            )

        rollback_capture = _next_metadata_rollback_capture(transaction_root)
        os.replace(metadata_path, rollback_capture)
        if _is_filesystem_link(rollback_capture) or not rollback_capture.is_file():
            raise AgentSkillUpdaterError(
                f"Captured rollback metadata is unsafe: {rollback_capture}"
            )
        captured_content = rollback_capture.read_bytes()
        if captured_content not in known_contents:
            _publish_metadata_file_if_absent(rollback_capture, metadata_path)
            return _preserve_metadata_content(
                transaction_root,
                metadata_path.parent,
                captured_content,
            )
        rollback_capture_proves_absence = True

    concurrent_artifact = _find_concurrent_metadata_artifact(
        transaction_root,
        known_contents,
    )
    if concurrent_artifact is not None:
        concurrent_content = concurrent_artifact.read_bytes()
        if not _publish_metadata_file_if_absent(concurrent_artifact, metadata_path):
            raise AgentSkillUpdaterError(
                f"Concurrent metadata could not be restored at {metadata_path}; "
                f"transaction data was retained."
            )
        return _preserve_metadata_content(
            transaction_root,
            metadata_path.parent,
            concurrent_content,
        )

    rollback_capture_proves_absence = (
        rollback_capture_proves_absence
        or _has_known_metadata_rollback_capture(transaction_root, known_contents)
    )

    if not os.path.lexists(metadata_path) and _metadata_deletion_is_ambiguous(
        transaction_root,
        metadata_phase,
        rollback_capture_proves_absence,
    ):
        raise AgentSkillUpdaterError(
            f"Metadata disappeared during the ambiguous '{metadata_phase}' phase at "
            f"{metadata_path}; transaction data was retained."
        )

    if original_content is None:
        return None
    restore_source = _next_metadata_restore_source(transaction_root, original_content)
    if not _publish_metadata_file_if_absent(restore_source, metadata_path):
        if _is_filesystem_link(metadata_path) or not metadata_path.is_file():
            raise AgentSkillUpdaterError(
                f"Metadata path changed to a non-file during rollback: {metadata_path}"
            )
        current = metadata_path.read_bytes()
        if current == original_content:
            return None
        if current not in known_contents:
            return _preserve_metadata_content(
                transaction_root,
                metadata_path.parent,
                current,
            )
        raise AgentSkillUpdaterError(
            f"Concurrent metadata prevented rollback at {metadata_path}."
        )
    if metadata_path.read_bytes() != original_content:
        raise AgentSkillUpdaterError(
            f"Restored metadata failed verification at {metadata_path}."
        )
    return None


def _next_metadata_rollback_capture(transaction_root: Path) -> Path:
    index = 1
    while True:
        candidate = transaction_root / f"metadata.rollback-{index:04d}"
        if not os.path.lexists(candidate):
            return candidate
        index += 1


def _next_metadata_restore_source(transaction_root: Path, content: bytes) -> Path:
    index = 1
    while True:
        candidate = transaction_root / f"metadata.restore-{index:04d}"
        if not os.path.lexists(candidate):
            _write_bytes_atomic(candidate, content)
            return candidate
        index += 1


def _find_concurrent_metadata_artifact(
    transaction_root: Path,
    known_contents: tuple[Optional[bytes], bytes],
) -> Optional[Path]:
    candidates = list(sorted(transaction_root.glob("metadata.rollback-*"), reverse=True))
    candidates.append(transaction_root / "metadata.displaced")
    for candidate in candidates:
        if not os.path.lexists(candidate):
            continue
        if _is_filesystem_link(candidate) or not candidate.is_file():
            raise AgentSkillUpdaterError(f"Metadata recovery artifact is unsafe: {candidate}")
        if candidate.read_bytes() not in known_contents:
            return candidate
    return None


def _has_known_metadata_rollback_capture(
    transaction_root: Path,
    known_contents: tuple[Optional[bytes], bytes],
) -> bool:
    for candidate in transaction_root.glob("metadata.rollback-*"):
        if _is_filesystem_link(candidate) or not candidate.is_file():
            raise AgentSkillUpdaterError(f"Metadata recovery artifact is unsafe: {candidate}")
        if candidate.read_bytes() in known_contents:
            return True
    return False


def _metadata_deletion_is_ambiguous(
    transaction_root: Path,
    metadata_phase: str,
    rollback_capture_proves_absence: bool,
) -> bool:
    if rollback_capture_proves_absence:
        return False
    displaced_exists = os.path.lexists(transaction_root / "metadata.displaced")
    if metadata_phase == COORDINATOR_PHASE_METADATA_CAPTURED:
        return not displaced_exists
    if metadata_phase == COORDINATOR_PHASE_CAPTURING_METADATA:
        return not displaced_exists
    if metadata_phase == COORDINATOR_PHASE_METADATA_PUBLISH_FAILED:
        return not displaced_exists
    return metadata_phase in {
        COORDINATOR_PHASE_PREPARED,
        COORDINATOR_PHASE_PUBLISHING_METADATA,
        COORDINATOR_PHASE_METADATA_PUBLISHED,
    }


def _decode_legacy_metadata_phase(phase: str) -> str:
    return {
        METADATA_PHASE_PREPARED: COORDINATOR_PHASE_PREPARED,
        METADATA_PHASE_CAPTURING: COORDINATOR_PHASE_CAPTURING_METADATA,
        METADATA_PHASE_CAPTURED: COORDINATOR_PHASE_METADATA_CAPTURED,
        METADATA_PHASE_PUBLISHING: COORDINATOR_PHASE_PUBLISHING_METADATA,
        METADATA_PHASE_PUBLISH_FAILED: COORDINATOR_PHASE_METADATA_PUBLISH_FAILED,
        METADATA_PHASE_PUBLISHED: COORDINATOR_PHASE_METADATA_PUBLISHED,
    }[phase]


def _git_require_payload_at_revision(
    repo_dir: Path,
    revision: str,
    entry_type: str,
) -> None:
    _validate_git_control_entry(repo_dir)
    required_path, expected_type = _payload_contract(entry_type)
    result = subprocess.run(
        _resolve_command(
            ["git", "-C", str(repo_dir), "cat-file", "-t", f"{revision}:{required_path}"]
        ),
        cwd=repo_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0 or result.stdout.strip() != expected_type:
        raise AgentSkillUpdaterError(
            f"Remote revision {revision} does not contain the required {required_path} "
            f"{expected_type}; refusing update."
        )


def _git_output(repo_dir: Path, arguments: list[str]) -> str:
    _validate_git_control_entry(repo_dir)
    result = subprocess.run(
        _resolve_command(["git", "-C", str(repo_dir), *arguments]),
        cwd=repo_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Git command failed"
        raise AgentSkillUpdaterError(message)
    value = result.stdout.strip()
    if not value:
        raise AgentSkillUpdaterError(f"Git command returned no value: {' '.join(arguments)}")
    return value


def sanitize_repo_url(value: str) -> str:
    normalized = value.strip().replace("\\", "/").rstrip("/")
    scp_match = re.fullmatch(r"[^/@]+@([^:]+):(.+)", normalized)
    if scp_match:
        host, path = scp_match.groups()
        normalized = f"https://{host}/{path}"

    if "://" in normalized:
        parsed = urllib.parse.urlsplit(normalized)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        path = parsed.path.rstrip("/")
        scheme = "https" if host.casefold() == "github.com" else parsed.scheme.casefold()
        normalized = f"{scheme}://{host}{port}{path}"
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized


def canonical_repo_identity(value: str) -> str:
    sanitized = sanitize_repo_url(value)
    if "://" in sanitized:
        parsed = urllib.parse.urlsplit(sanitized)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{host}{port}{parsed.path.rstrip('/')}".casefold()
    return sanitized.casefold()


def _stage_git_skill_at_ref(source: AgentSkillSource, stage_root: Path, ref: str) -> Path:
    if not source.repo_url:
        raise AgentSkillUpdaterError(f"Skill '{source.name}' is missing repoUrl metadata.")
    normalized_ref = _require_git_commit(ref, f"Skill '{source.name}' staging")
    owner, repo = _parse_github_repo(source.repo_url)
    _require_remote_probe_ready(source)
    repo_root = _download_repo_archive(owner, repo, normalized_ref, stage_root)
    normalized_subpath = normalize_skill_subpath(source.subpath)
    skill_path = repo_root
    if normalized_subpath != ".":
        skill_path = repo_root.joinpath(*PurePosixPath(normalized_subpath).parts)
        try:
            skill_path.resolve().relative_to(repo_root.resolve())
        except ValueError as exc:
            raise AgentSkillUpdaterError(
                f"Subpath '{source.subpath}' escapes the downloaded repository."
            ) from exc
    if not skill_path.exists():
        raise AgentSkillUpdaterError(f"Subpath '{source.subpath}' not found for '{source.name}'.")
    if not (skill_path / "SKILL.md").exists():
        raise AgentSkillUpdaterError(f"Remote skill '{source.name}' does not contain SKILL.md.")

    destination = stage_root / source.name
    destination.mkdir(parents=True, exist_ok=False)
    _copy_directory_contents(skill_path, destination)
    _validate_skill_payload(destination, entry_type="single-skill")
    _require_remote_probe_ready(source)
    return destination


def _stage_update_base(update: AgentSkillUpdate, stage_root: Path) -> Optional[Path]:
    if _is_openspec_source(update.source):
        return None
    base_version = update.installed_base_version
    if not update.source.repo_url or not base_version or base_version == "unknown":
        raise AgentSkillUpdaterError(
            f"Skill '{update.source.name}' has no exact installed base to merge."
        )
    _require_remote_updates_enabled(update.source, base_version)
    staged_base = _stage_git_skill_at_ref(update.source, stage_root, base_version)
    _require_remote_updates_enabled(update.source, base_version)
    return staged_base


def _merge_skill_directories(
    *,
    base_dir: Optional[Path],
    local_dir: Path,
    remote_dir: Path,
    merged_dir: Path,
    conflict_root: Path,
) -> None:
    conflicts: list[str] = []
    merged_dir.mkdir(parents=True, exist_ok=True)
    base_node = _build_payload_node(base_dir)
    local_node = _build_payload_node(local_dir)
    remote_node = _build_payload_node(remote_dir)
    merged_node = _merge_payload_nodes(
        base_node=base_node,
        local_node=local_node,
        remote_node=remote_node,
        base_root=base_dir,
        local_root=local_dir,
        remote_root=remote_dir,
        relative_path=Path(),
        conflict_root=conflict_root,
        conflicts=conflicts,
    )

    if conflicts:
        conflict_list = "\n".join(f"- {item}" for item in conflicts)
        raise AgentSkillMergeConflictError(
            "Local skill changes conflict with the remote update. "
            f"No files were overwritten. Review {conflict_root} and resolve:\n{conflict_list}"
        )
    _write_payload_node(merged_node, merged_dir)


def _build_payload_node(root: Optional[Path]) -> _PayloadNode:
    if root is None or not os.path.lexists(root):
        return ABSENT_PAYLOAD_NODE
    _validate_skill_root(root)

    def build(current: Path) -> _PayloadNode:
        if _is_filesystem_link(current):
            raise AgentSkillUpdaterError(f"Skill payload contains an unsupported link: {current}")
        if current.is_file():
            return _PayloadNode("file", content=current.read_bytes())
        if not current.is_dir():
            raise AgentSkillUpdaterError(f"Skill payload contains an unsupported entry: {current}")
        children: list[tuple[str, _PayloadNode]] = []
        entries = sorted(current.iterdir(), key=lambda item: (item.name.casefold(), item.name))
        _validate_portable_child_names(entries, current)
        for child in entries:
            if not is_skill_payload_path(child, root):
                continue
            children.append((child.name, build(child)))
        return _PayloadNode("directory", children=tuple(children))

    return build(root)


def _merge_payload_nodes(
    *,
    base_node: _PayloadNode,
    local_node: _PayloadNode,
    remote_node: _PayloadNode,
    base_root: Optional[Path],
    local_root: Path,
    remote_root: Path,
    relative_path: Path,
    conflict_root: Path,
    conflicts: list[str],
) -> _PayloadNode:
    if local_node == remote_node:
        return local_node
    if local_node == base_node:
        return remote_node
    if remote_node == base_node:
        return local_node

    if local_node.kind == "directory" and remote_node.kind == "directory":
        base_children = dict(base_node.children) if base_node.kind == "directory" else {}
        local_children = dict(local_node.children)
        remote_children = dict(remote_node.children)
        merged_children: list[tuple[str, _PayloadNode]] = []
        for name in sorted(
            base_children.keys() | local_children.keys() | remote_children.keys(),
            key=str.casefold,
        ):
            child_path = relative_path / name
            merged_child = _merge_payload_nodes(
                base_node=base_children.get(name, ABSENT_PAYLOAD_NODE),
                local_node=local_children.get(name, ABSENT_PAYLOAD_NODE),
                remote_node=remote_children.get(name, ABSENT_PAYLOAD_NODE),
                base_root=base_root,
                local_root=local_root,
                remote_root=remote_root,
                relative_path=child_path,
                conflict_root=conflict_root,
                conflicts=conflicts,
            )
            if merged_child.kind != "absent":
                merged_children.append((name, merged_child))
        return _PayloadNode("directory", children=tuple(merged_children))

    if (
        base_node.kind == "file"
        and local_node.kind == "file"
        and remote_node.kind == "file"
        and base_root is not None
    ):
        merged = _merge_text_file(
            base_root / relative_path,
            local_root / relative_path,
            remote_root / relative_path,
        )
        if merged is not None:
            return _PayloadNode("file", content=merged)

    display_path = relative_path.as_posix() or "."
    _write_node_conflict_files(
        conflict_root,
        relative_path,
        base_node,
        local_node,
        remote_node,
        base_root,
        local_root,
        remote_root,
    )
    conflicts.append(display_path)
    return ABSENT_PAYLOAD_NODE


def _write_payload_node(node: _PayloadNode, destination: Path) -> None:
    if node.kind != "directory":
        raise AgentSkillUpdaterError("Merged Skill payload root must remain a directory.")
    _validate_portable_node_child_names(node, destination)
    destination.mkdir(parents=True, exist_ok=True)
    for name, child in node.children:
        child_destination = destination / name
        if child.kind == "file":
            child_destination.parent.mkdir(parents=True, exist_ok=True)
            child_destination.write_bytes(child.content or b"")
        elif child.kind == "directory":
            _write_payload_node(child, child_destination)
        else:
            raise AgentSkillUpdaterError(
                f"Unexpected merged payload node at {child_destination}: {child.kind}"
            )


def _validate_portable_child_names(entries: list[Path], directory: Path) -> None:
    seen: dict[str, str] = {}
    for entry in entries:
        normalized = entry.name.casefold()
        previous = seen.get(normalized)
        if previous is not None and previous != entry.name:
            raise AgentSkillUpdaterError(
                f"Skill payload contains names that collide on case-insensitive filesystems in "
                f"'{directory}': {previous}, {entry.name}."
            )
        seen[normalized] = entry.name


def _validate_portable_node_child_names(node: _PayloadNode, destination: Path) -> None:
    seen: dict[str, str] = {}
    for name, _ in node.children:
        normalized = name.casefold()
        previous = seen.get(normalized)
        if previous is not None and previous != name:
            raise AgentSkillUpdaterError(
                f"Merged Skill payload contains names that collide on case-insensitive filesystems "
                f"in '{destination}': {previous}, {name}."
            )
        seen[normalized] = name


def _write_node_conflict_files(
    conflict_root: Path,
    relative_path: Path,
    base_node: _PayloadNode,
    local_node: _PayloadNode,
    remote_node: _PayloadNode,
    base_root: Optional[Path],
    local_root: Path,
    remote_root: Path,
) -> None:
    destination = conflict_root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    for label, node, root in (
        ("base", base_node, base_root),
        ("local", local_node, local_root),
        ("remote", remote_node, remote_root),
    ):
        variant = destination.with_name(f"{destination.name}.{label}")
        if node.kind == "file" and root is not None:
            shutil.copy2(root / relative_path, variant)
        elif node.kind == "directory" and root is not None:
            _copy_directory_contents(root / relative_path, variant)
        else:
            variant.with_name(f"{variant.name}.absent").write_text(
                "absent\n",
                encoding="utf-8",
            )


def _merge_text_file(base_file: Path, local_file: Path, remote_file: Path) -> Optional[bytes]:
    try:
        base_file.read_text(encoding="utf-8")
        local_file.read_text(encoding="utf-8")
        remote_file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None

    result = subprocess.run(
        _resolve_command(["git", "merge-file", "-p", str(local_file), str(base_file), str(remote_file)]),
        capture_output=True,
        text=False,
    )
    if result.returncode == 0:
        return result.stdout
    return None


def _relative_file_paths(root: Optional[Path]) -> set[Path]:
    if root is None or not root.exists():
        return set()
    return {file_path.relative_to(root) for file_path in iter_skill_payload_files(root)}


def _relative_directory_paths(root: Optional[Path]) -> set[Path]:
    if root is None or not root.exists():
        return set()
    return {
        directory.relative_to(root)
        for directory in iter_skill_payload_directories(root)
    }


def _copy_directory_contents(source_dir: Path, destination_dir: Path) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    for relative_path in sorted(
        _relative_directory_paths(source_dir),
        key=lambda item: item.as_posix(),
    ):
        (destination_dir / relative_path).mkdir(parents=True, exist_ok=True)
    for relative_path in sorted(_relative_file_paths(source_dir), key=lambda item: item.as_posix()):
        source = source_dir / relative_path
        destination = destination_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _validate_skill_payload(
    payload_dir: Path,
    expected_signature: Optional[str] = None,
    *,
    entry_type: str,
) -> str:
    required_name, required_kind = _payload_contract(entry_type)
    required_path = payload_dir / required_name
    required_type_matches = (
        required_path.is_file() if required_kind == "blob" else required_path.is_dir()
    )
    if not required_type_matches:
        raise AgentSkillUpdaterError(
            f"{entry_type} payload at '{payload_dir}' is missing its required content root."
        )
    signature = directory_signature(payload_dir)
    if expected_signature is not None and signature != expected_signature:
        raise AgentSkillUpdaterError(f"Skill payload verification failed at '{payload_dir}'.")
    return signature


def _apply_payload_transaction(
    update: AgentSkillUpdate,
    merged_dir: Path,
    expected_original_signature: Optional[str] = None,
    backup_dir: Optional[Path] = None,
) -> None:
    skill_dir = update.source.local_dir
    expected_signature = _validate_skill_payload(
        merged_dir,
        entry_type=update.source.entry_type,
    )
    with skill_update_lock(skill_dir):
        _recover_skill_transactions_locked(skill_dir)
        metadata_snapshot_object, original_metadata = _require_remote_updates_enabled(
            update.source,
            update.installed_base_version,
        )
        _validate_skill_root(skill_dir)
        if is_git_worktree_skill(skill_dir):
            raise AgentSkillUpdaterError(
                f"Skill '{update.source.name}' became a Git worktree during staging; "
                "refusing snapshot replacement."
            )
        original_signature = _validate_skill_payload(
            skill_dir,
            entry_type=update.source.entry_type,
        )
        if (
            expected_original_signature is not None
            and original_signature != expected_original_signature
        ):
            raise AgentSkillUpdaterError(
                f"Skill '{update.source.name}' changed after backup/merge; refusing to overwrite it."
            )
        metadata_path = update.source.metadata_path
        if metadata_path is None or metadata_snapshot_object is None or original_metadata is None:
            raise AgentSkillUpdaterError(
                f"Remotely managed skill '{update.source.name}' requires canonical metadata."
            )
        expected_metadata, metadata_changed = _build_refreshed_metadata(
            update,
            metadata_snapshot_object,
        )
        expected_metadata_bytes = (
            _json_payload_bytes(expected_metadata)
            if metadata_changed or original_metadata is None
            else original_metadata
        )
        transaction_root = Path(
            tempfile.mkdtemp(prefix=f".{update.source.name}.update-", dir=skill_dir.parent)
        )
        incoming_dir = transaction_root / "incoming"
        original_dir = transaction_root / "original"
        displaced_dir = transaction_root / "displaced"
        failed_dir = transaction_root / "failed"
        state = {
            "version": TRANSACTION_STATE_VERSION,
            "transactionType": SNAPSHOT_TRANSACTION_TYPE,
            "skillName": update.source.name,
            "skillDir": str(skill_dir.resolve()),
            "phase": TRANSACTION_PHASE_PREPARING,
            "metadataPhase": METADATA_PHASE_PREPARED,
            "originalSignature": original_signature,
            "expectedSignature": expected_signature,
            "originalMetadataPresent": original_metadata is not None,
            "originalMetadataSha256": _sha256_bytes(original_metadata),
            "expectedMetadataPresent": True,
            "expectedMetadataSha256": _sha256_bytes(expected_metadata_bytes),
        }
        state_written = False

        try:
            _write_transaction_state(transaction_root, state)
            state_written = True
            _write_bytes_atomic(transaction_root / TRANSACTION_MARKER_FILENAME, b"1\n")
            incoming_dir.mkdir()
            original_dir.mkdir()
            displaced_dir.mkdir()
            failed_dir.mkdir()
            _copy_directory_contents(skill_dir, original_dir)
            _validate_skill_payload(
                original_dir,
                original_signature,
                entry_type=update.source.entry_type,
            )
            _copy_directory_contents(merged_dir, incoming_dir)
            _validate_skill_payload(
                incoming_dir,
                expected_signature,
                entry_type=update.source.entry_type,
            )
            _prepare_transaction_metadata_files(
                transaction_root,
                original_metadata,
                expected_metadata_bytes,
            )
            _set_transaction_phase(transaction_root, state, TRANSACTION_PHASE_PREPARED)

            _set_transaction_phase(transaction_root, state, TRANSACTION_PHASE_MOVING_ORIGINAL)
            _require_remote_updates_enabled(
                update.source,
                update.installed_base_version,
            )
            if is_git_worktree_skill(skill_dir):
                raise AgentSkillUpdaterError(
                    f"Skill '{update.source.name}' became a Git worktree during apply; "
                    "refusing snapshot replacement."
                )
            _move_payload_files(skill_dir, displaced_dir)
            if directory_signature(displaced_dir) != original_signature:
                raise AgentSkillUpdaterError(
                    f"Skill '{update.source.name}' changed while its update was being applied."
                )
            original_directories = _relative_directory_paths(original_dir)
            remaining_files = _relative_file_paths(skill_dir)
            remaining_directories = _relative_directory_paths(skill_dir) - original_directories
            if remaining_files or remaining_directories:
                raise AgentSkillUpdaterError(
                    f"Skill '{update.source.name}' gained payload entries while its update was being applied."
                )
            _prune_empty_payload_directories(skill_dir)

            _set_transaction_phase(transaction_root, state, TRANSACTION_PHASE_INSTALLING)
            _install_payload_without_overwrite(incoming_dir, skill_dir)
            _validate_skill_payload(
                skill_dir,
                expected_signature,
                entry_type=update.source.entry_type,
            )

            _set_transaction_phase(
                transaction_root,
                state,
                TRANSACTION_PHASE_COMMITTING_METADATA,
            )
            _require_remote_updates_enabled(
                update.source,
                update.installed_base_version,
            )
            if is_git_worktree_skill(skill_dir):
                raise AgentSkillUpdaterError(
                    f"Skill '{update.source.name}' became a Git worktree before metadata commit; "
                    "rolling back snapshot replacement."
                )
            _validate_transaction_metadata(skill_dir, state, expected=False)
            _commit_transaction_metadata(
                transaction_root,
                state,
                metadata_path,
                expected_metadata_bytes,
                original_metadata,
                phases=LEGACY_METADATA_PHASES,
            )
            _validate_snapshot_commit_state(update, state, expected_signature)
            if backup_dir is not None:
                _publish_payload_backup(
                    original_dir,
                    transaction_root / "backup",
                    backup_dir,
                    original_signature,
                    update.source.entry_type,
                )
            _validate_snapshot_commit_state(update, state, expected_signature)
            _set_transaction_phase(transaction_root, state, TRANSACTION_PHASE_COMMITTED)
        except Exception as exc:  # noqa: BLE001 - all apply failures share one rollback contract
            if not state_written:
                try:
                    _remove_transaction_tree(transaction_root)
                except (OSError, AgentSkillUpdaterError) as cleanup_exc:
                    raise AgentSkillUpdaterError(
                        f"Failed to prepare update for '{update.source.name}': {exc}. "
                        f"Temporary transaction cleanup also failed at {transaction_root}: "
                        f"{cleanup_exc}"
                    ) from exc
                raise AgentSkillUpdaterError(
                    f"Failed to prepare update for '{update.source.name}': {exc}"
                ) from exc
            try:
                if state["phase"] in {
                    TRANSACTION_PHASE_PREPARING,
                    TRANSACTION_PHASE_PREPARED,
                }:
                    _validate_skill_payload(
                        skill_dir,
                        state["originalSignature"],
                        entry_type=update.source.entry_type,
                    )
                    recovery_path = _rollback_transaction_metadata(
                        transaction_root,
                        _decode_legacy_metadata_phase(state["metadataPhase"]),
                        metadata_path,
                        original_metadata,
                        expected_metadata_bytes,
                    )
                    if recovery_path is None:
                        _validate_transaction_metadata(skill_dir, state, expected=False)
                    else:
                        state["recoveryPath"] = str(recovery_path)
                    _set_transaction_phase(
                        transaction_root,
                        state,
                        TRANSACTION_PHASE_ROLLED_BACK,
                    )
                else:
                    recovery_path = _restore_payload_transaction(transaction_root, state)
            except Exception as rollback_exc:  # noqa: BLE001 - retain durable recovery data
                raise AgentSkillUpdaterError(
                    f"Failed to apply update for '{update.source.name}': {exc}. "
                    f"Rollback also failed: {rollback_exc}. Recovery data remains at {transaction_root}."
                ) from exc
            try:
                _remove_transaction_tree(transaction_root)
            except (OSError, AgentSkillUpdaterError) as cleanup_exc:
                raise AgentSkillUpdaterError(
                    f"Failed to apply update for '{update.source.name}'; the original payload was "
                    f"restored, but transaction cleanup failed at {transaction_root}: {cleanup_exc}"
                ) from exc
            recovery_suffix = (
                f" Unexpected concurrent files were preserved at {recovery_path}."
                if recovery_path is not None
                else ""
            )
            raise AgentSkillUpdaterError(
                f"Failed to apply update for '{update.source.name}'; the original payload was "
                f"restored: {exc}.{recovery_suffix}"
            ) from exc

        # The payload and metadata are committed before cleanup. If cleanup is interrupted,
        # the durable committed phase lets the next registry sync verify and remove it safely.
        try:
            _remove_transaction_tree(transaction_root)
        except (OSError, AgentSkillUpdaterError) as cleanup_exc:
            raise AgentSkillUpdateCommittedError(
                f"Update for '{update.source.name}' committed, but transaction cleanup failed "
                f"at {transaction_root}: {cleanup_exc}",
                action="payload_merged",
                version=update.remote_version,
            ) from cleanup_exc


def _validate_snapshot_commit_state(
    update: AgentSkillUpdate,
    state: dict[str, object],
    expected_signature: str,
) -> None:
    skill_dir = update.source.local_dir
    _validate_skill_payload(
        skill_dir,
        expected_signature,
        entry_type=update.source.entry_type,
    )
    _validate_transaction_metadata(skill_dir, state, expected=True)
    if is_git_worktree_skill(skill_dir):
        raise AgentSkillUpdaterError(
            f"Skill '{update.source.name}' became a Git worktree before transaction commit; "
            "rolling back snapshot replacement."
        )
    _require_remote_updates_enabled(update.source, update.remote_version)


def _move_payload_files(
    source_root: Path,
    destination_root: Path,
) -> list[Path]:
    moved: list[Path] = []
    for relative_path in sorted(
        _relative_directory_paths(source_root),
        key=lambda item: item.as_posix(),
    ):
        (destination_root / relative_path).mkdir(parents=True, exist_ok=True)
    for relative_path in sorted(_relative_file_paths(source_root), key=lambda item: item.as_posix()):
        source = source_root / relative_path
        destination = destination_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        moved.append(relative_path)
    return moved


def _install_payload_without_overwrite(source_root: Path, destination_root: Path) -> None:
    destination_root.mkdir(parents=True, exist_ok=True)
    for relative_path in sorted(
        _relative_directory_paths(source_root),
        key=lambda item: item.as_posix(),
    ):
        destination = destination_root / relative_path
        if destination.exists() and not destination.is_dir():
            raise FileExistsError(f"Payload path appeared during update: {destination}")
        destination.mkdir(parents=True, exist_ok=True)
    for relative_path in sorted(_relative_file_paths(source_root), key=lambda item: item.as_posix()):
        source = source_root / relative_path
        destination = destination_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_payload_file_exclusive(source, destination)


def _copy_payload_file_exclusive(source: Path, destination: Path) -> None:
    with source.open("rb") as source_handle, destination.open("xb") as destination_handle:
        shutil.copyfileobj(source_handle, destination_handle)
        destination_handle.flush()
        os.fsync(destination_handle.fileno())
    shutil.copystat(source, destination)


def _remove_transaction_tree(transaction_root: Path) -> None:
    def make_writable_and_retry(function, path_value, _error_info):
        os.chmod(path_value, stat.S_IWRITE)
        function(path_value)

    if not transaction_root.exists():
        return
    if _is_filesystem_link(transaction_root) or not transaction_root.is_dir():
        raise AgentSkillUpdaterError(
            f"Transaction root is not a safe directory: {transaction_root}"
        )
    state_path = transaction_root / TRANSACTION_STATE_FILENAME
    marker_path = transaction_root / TRANSACTION_MARKER_FILENAME
    for control_path in (state_path, marker_path):
        if os.path.lexists(control_path) and (
            _is_filesystem_link(control_path) or not control_path.is_file()
        ):
            raise AgentSkillUpdaterError(
                f"Transaction control path is not a file: {control_path}"
            )
    state_bytes = state_path.read_bytes() if state_path.is_file() else None
    marker_bytes = marker_path.read_bytes() if marker_path.is_file() else None

    try:
        for child in transaction_root.iterdir():
            if child.name in {TRANSACTION_STATE_FILENAME, TRANSACTION_MARKER_FILENAME}:
                continue
            if child.is_dir() and not _is_filesystem_link(child):
                shutil.rmtree(child, onerror=make_writable_and_retry)
            else:
                try:
                    child.unlink()
                except PermissionError:
                    os.chmod(child, stat.S_IWRITE)
                    child.unlink()
        if marker_path.exists():
            marker_path.unlink()
        if state_path.exists():
            state_path.unlink()
        transaction_root.rmdir()
    except Exception as exc:
        try:
            if transaction_root.is_dir():
                if state_bytes is not None and not state_path.is_file():
                    _write_bytes_atomic(state_path, state_bytes)
                if marker_bytes is not None and not marker_path.is_file():
                    _write_bytes_atomic(marker_path, marker_bytes)
        except BaseException as restore_exc:
            raise AgentSkillUpdaterError(
                f"Transaction cleanup failed at {transaction_root}: {exc}. "
                f"Control-state restoration also failed: {restore_exc}"
            ) from exc
        raise


def _looks_like_transaction_name(name: str) -> bool:
    return name.startswith(".") and (
        ".update-" in name
        or ".git-update-" in name
        or ".metadata-update-" in name
        or ".transaction-" in name
    )


def _validate_transaction_root(transaction_root: Path, skills_root: Path) -> None:
    if _is_filesystem_link(transaction_root):
        raise AgentSkillUpdaterError(
            f"Transaction root must not be a symlink or junction: {transaction_root}"
        )
    if not transaction_root.is_dir():
        raise AgentSkillUpdaterError(f"Transaction root is not a directory: {transaction_root}")
    if not _same_path(transaction_root.parent, skills_root) or not _same_path(
        transaction_root.resolve().parent,
        skills_root,
    ):
        raise AgentSkillUpdaterError(
            f"Transaction root points outside skills root {skills_root}: {transaction_root}"
        )
    for filename in (TRANSACTION_STATE_FILENAME, TRANSACTION_MARKER_FILENAME):
        path = transaction_root / filename
        if os.path.lexists(path) and (_is_filesystem_link(path) or not path.is_file()):
            raise AgentSkillUpdaterError(f"Transaction control path is unsafe: {path}")


def _transaction_subdirectory(
    transaction_root: Path,
    name: str,
    *,
    create: bool = False,
    required: bool = True,
) -> Optional[Path]:
    path = transaction_root / name
    if not os.path.lexists(path):
        if create:
            path.mkdir()
        elif required:
            raise AgentSkillUpdaterError(
                f"Transaction directory is missing at {transaction_root}: {name}"
            )
        else:
            return None
    if _is_filesystem_link(path) or not path.is_dir():
        raise AgentSkillUpdaterError(f"Transaction directory is unsafe: {path}")
    if not _same_path(path.resolve().parent, transaction_root):
        raise AgentSkillUpdaterError(
            f"Transaction directory points outside its transaction root: {path}"
        )
    return path


def _transaction_file(
    transaction_root: Path,
    name: str,
    *,
    required: bool,
) -> Optional[Path]:
    path = transaction_root / name
    if not os.path.lexists(path):
        if required:
            raise AgentSkillUpdaterError(
                f"Transaction file is missing at {transaction_root}: {name}"
            )
        return None
    if _is_filesystem_link(path) or not path.is_file():
        raise AgentSkillUpdaterError(f"Transaction file is unsafe: {path}")
    if not _same_path(path.resolve().parent, transaction_root):
        raise AgentSkillUpdaterError(
            f"Transaction file points outside its transaction root: {path}"
        )
    return path


def _transaction_attempt_directories(failed_root: Path) -> list[Path]:
    attempts: list[Path] = []
    for path in sorted(failed_root.iterdir(), key=lambda item: item.name.casefold()):
        if _is_filesystem_link(path) or not path.is_dir():
            raise AgentSkillUpdaterError(f"Transaction attempt directory is unsafe: {path}")
        if not _same_path(path.resolve().parent, failed_root):
            raise AgentSkillUpdaterError(
                f"Transaction attempt points outside failed directory: {path}"
            )
        attempts.append(path)
    return attempts


def _safe_direct_child_directory(
    parent: Path,
    name: str,
    *,
    create: bool,
    label: str,
) -> Optional[Path]:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise AgentSkillUpdaterError(f"Invalid {label.lower()} directory name: {name}")
    path = parent / name
    if not os.path.lexists(path):
        if not create:
            return None
        path.mkdir(exist_ok=True)
    if _is_filesystem_link(path) or not path.is_dir():
        raise AgentSkillUpdaterError(f"{label} directory is unsafe: {path}")
    if not _same_path(path.resolve().parent, parent):
        raise AgentSkillUpdaterError(f"{label} directory points outside its parent: {path}")
    return path


def _safe_recovery_directory(
    skill_dir: Path,
    transaction_root: Path,
    *,
    create: bool,
) -> Optional[Path]:
    return _safe_direct_child_directory(
        skill_dir.parent,
        f".recovery-{transaction_root.name[1:]}",
        create=create,
        label="Recovery",
    )


def _safe_recovery_path(recovery_root: Path, relative_path: Path) -> Path:
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise AgentSkillUpdaterError(f"Unsafe recovery path: {relative_path}")
    current = recovery_root
    for part in relative_path.parent.parts:
        if part in {"", "."}:
            continue
        current = current / part
        if not os.path.lexists(current):
            current.mkdir()
        if _is_filesystem_link(current) or not current.is_dir():
            raise AgentSkillUpdaterError(f"Recovery parent directory is unsafe: {current}")
        try:
            current.resolve().relative_to(recovery_root.resolve())
        except ValueError as exc:
            raise AgentSkillUpdaterError(
                f"Recovery path escapes recovery root: {current}"
            ) from exc
    destination = current / relative_path.name
    if os.path.lexists(destination) and _is_filesystem_link(destination):
        raise AgentSkillUpdaterError(f"Recovery destination is unsafe: {destination}")
    return destination


def _safe_recovery_subdirectory(recovery_root: Path, relative_path: Path) -> Path:
    path = _safe_recovery_path(recovery_root, relative_path)
    if not os.path.lexists(path):
        path.mkdir()
    if _is_filesystem_link(path) or not path.is_dir():
        raise AgentSkillUpdaterError(f"Recovery subdirectory is unsafe: {path}")
    return path


def recover_updates(skills_root: Path) -> list[TransactionOutcome]:
    outcomes: list[TransactionOutcome] = []
    if not skills_root.exists():
        return outcomes
    for transaction_root in sorted(skills_root.iterdir(), key=lambda path: path.name.casefold()):
        if not _looks_like_transaction_name(transaction_root.name):
            continue
        try:
            _validate_transaction_root(transaction_root, skills_root)
            if not _looks_like_transaction_directory(transaction_root):
                continue
            transaction_type, state = _read_recovery_state(
                transaction_root,
                skills_root,
            )
            skill_dir = _decoded_skill_dir(state)
            with skill_update_lock(skill_dir):
                outcome = _recover_decoded_transaction_locked(
                    transaction_root,
                    transaction_type,
                    state,
                )
            outcomes.append(outcome)
        except (AgentSkillUpdaterError, OSError, ValueError) as exc:
            outcomes.append(_uncertain_recovery_outcome(transaction_root, exc))
    return outcomes


def recover_incomplete_skill_transactions(skills_root: Path) -> None:
    if not skills_root.exists():
        return
    for transaction_root in sorted(skills_root.iterdir(), key=lambda path: path.name.casefold()):
        if not _looks_like_transaction_name(transaction_root.name):
            continue
        _validate_transaction_root(transaction_root, skills_root)
        if not _looks_like_transaction_directory(transaction_root):
            continue
        state_path = transaction_root / TRANSACTION_STATE_FILENAME
        if not state_path.is_file():
            error = AgentSkillUpdaterError(
                f"Update transaction state is missing at {transaction_root}; "
                "recovery data was preserved for manual inspection."
            )
            if _is_metadata_journal_name(transaction_root):
                raise AgentSkillRecoveryUncertainError(
                    _uncertain_recovery_outcome(transaction_root, error)
                ) from error
            raise error
        try:
            transaction_type, state = _read_recovery_state(transaction_root, skills_root)
        except (AgentSkillUpdaterError, OSError, ValueError) as exc:
            if not _is_metadata_journal_name(transaction_root):
                raise
            raise AgentSkillRecoveryUncertainError(
                _uncertain_recovery_outcome(transaction_root, exc)
            ) from exc
        skill_dir = _decoded_skill_dir(state)
        with skill_update_lock(skill_dir):
            outcome = _recover_decoded_transaction_locked(
                transaction_root,
                transaction_type,
                state,
            )
            if outcome.installed_state == "uncertain":
                raise AgentSkillRecoveryUncertainError(outcome)


def _recover_skill_transactions_locked(skill_dir: Path) -> None:
    prefixes = (
        f".{skill_dir.name}.update-",
        f".{skill_dir.name}.git-update-",
        f".{skill_dir.name}.metadata-update-",
        f".{skill_dir.name}.transaction-",
    )
    for transaction_root in sorted(skill_dir.parent.iterdir(), key=lambda path: path.name.casefold()):
        if not transaction_root.name.startswith(prefixes):
            continue
        _validate_transaction_root(transaction_root, skill_dir.parent)
        try:
            state_path = transaction_root / TRANSACTION_STATE_FILENAME
            if not state_path.is_file():
                if (transaction_root / TRANSACTION_MARKER_FILENAME).is_file():
                    raise AgentSkillUpdaterError(
                        f"Update transaction state is missing at {transaction_root}; "
                        "recovery data was preserved for manual inspection."
                    )
                continue
            transaction_type, state = _read_recovery_state(
                transaction_root,
                skill_dir.parent,
            )
            if not _same_path(_decoded_skill_dir(state), skill_dir):
                raise AgentSkillUpdaterError(
                    f"Transaction {transaction_root} belongs to a different skill directory."
                )
            outcome = _recover_decoded_transaction_locked(
                transaction_root,
                transaction_type,
                state,
            )
        except (AgentSkillUpdaterError, OSError, ValueError) as exc:
            if _is_metadata_journal_name(transaction_root):
                raise AgentSkillRecoveryUncertainError(
                    _uncertain_recovery_outcome(transaction_root, exc)
                ) from exc
            raise
        if outcome.installed_state == "uncertain":
            raise AgentSkillRecoveryUncertainError(outcome)


def _read_recovery_state(
    transaction_root: Path,
    skills_root: Path,
) -> tuple[str, dict | _MetadataJournal]:
    transaction_type = _read_transaction_type(transaction_root)
    if transaction_type == COORDINATOR_TRANSACTION_TYPE:
        state = _decode_coordinator_metadata_journal(
            _read_coordinator_transaction_state(transaction_root, skills_root)
        )
    elif transaction_type == METADATA_TRANSACTION_TYPE:
        state = _decode_legacy_metadata_journal(
            _read_metadata_transaction_state(transaction_root, skills_root)
        )
    else:
        state = _read_typed_transaction_state(
            transaction_root,
            skills_root,
            transaction_type,
        )
    return transaction_type, state


def _decoded_skill_dir(state: dict | _MetadataJournal) -> Path:
    return state.skill_dir if isinstance(state, _MetadataJournal) else Path(state["skillDir"])


def _recover_decoded_transaction_locked(
    transaction_root: Path,
    transaction_type: str,
    state: dict | _MetadataJournal,
) -> TransactionOutcome:
    if transaction_type in {COORDINATOR_TRANSACTION_TYPE, METADATA_TRANSACTION_TYPE}:
        if not isinstance(state, _MetadataJournal):
            raise AgentSkillUpdaterError(f"Invalid decoded metadata journal: {transaction_root}")
        return _recover_metadata_journal_locked(transaction_root, state)
    if not isinstance(state, dict):
        raise AgentSkillUpdaterError(f"Invalid decoded transaction journal: {transaction_root}")
    original_phase = state["phase"]
    if transaction_type == GIT_TRANSACTION_TYPE:
        _recover_git_transaction_from_state(transaction_root, state)
    else:
        _recover_transaction_from_state(transaction_root, state)
    return _legacy_recovery_outcome(state, original_phase)


def _read_coordinator_transaction_state(
    transaction_root: Path,
    skills_root: Path,
) -> dict:
    try:
        state_path = _transaction_file(
            transaction_root,
            TRANSACTION_STATE_FILENAME,
            required=True,
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentSkillUpdaterError(
            f"Cannot read Transaction Coordinator journal at {transaction_root}: {exc}"
        ) from exc
    required = {
        "version",
        "transactionType",
        "transactionKind",
        "skillName",
        "skillDir",
        "phase",
        "targetRevision",
        "targetVersion",
        "evidence",
    }
    if not isinstance(state, dict) or not required.issubset(state):
        raise AgentSkillUpdaterError(
            f"Incomplete Transaction Coordinator journal at {transaction_root}."
        )
    string_keys = required - {"version", "evidence"}
    if not all(isinstance(state[key], str) for key in string_keys):
        raise AgentSkillUpdaterError(
            f"Invalid Transaction Coordinator journal at {transaction_root}."
        )
    if (
        state["version"] != COORDINATOR_TRANSACTION_STATE_VERSION
        or state["transactionType"] != COORDINATOR_TRANSACTION_TYPE
        or state["transactionKind"] != METADATA_ONLY_TRANSACTION_KIND
        or state["phase"] not in COORDINATOR_PHASES
    ):
        raise AgentSkillUpdaterError(
            f"Unsupported Transaction Coordinator journal at {transaction_root}."
        )
    evidence = state["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {
        "beforeMetadata",
        "expectedMetadata",
    }:
        raise AgentSkillUpdaterError(
            f"Invalid Transaction Evidence at {transaction_root}."
        )
    for label, record in evidence.items():
        if not isinstance(record, dict) or set(record) != {"present", "sha256"}:
            raise AgentSkillUpdaterError(
                f"Invalid {label} Transaction Evidence at {transaction_root}."
            )
        if not isinstance(record["present"], bool):
            raise AgentSkillUpdaterError(
                f"Invalid {label} Transaction Evidence at {transaction_root}."
            )
        digest = record["sha256"]
        if record["present"]:
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise AgentSkillUpdaterError(
                    f"Invalid {label} Transaction Evidence at {transaction_root}."
                )
        elif digest is not None:
            raise AgentSkillUpdaterError(
                f"Invalid {label} Transaction Evidence at {transaction_root}."
            )
    skill_dir = Path(state["skillDir"])
    if not _same_path(skill_dir.parent, skills_root):
        raise AgentSkillUpdaterError(
            f"Transaction {transaction_root} points outside skills root {skills_root}."
        )
    expected_prefix = f".{state['skillName']}.transaction-"
    if skill_dir.name != state["skillName"] or not transaction_root.name.startswith(
        expected_prefix
    ):
        raise AgentSkillUpdaterError(
            f"Transaction identity mismatch at {transaction_root}."
        )
    return state


def _decode_coordinator_metadata_journal(state: dict) -> _MetadataJournal:
    evidence = state["evidence"]
    before = evidence["beforeMetadata"]
    expected = evidence["expectedMetadata"]
    return _MetadataJournal(
        skill_name=state["skillName"],
        skill_dir=Path(state["skillDir"]),
        phase=state["phase"],
        target_version=state["targetVersion"],
        evidence=_ControlMetadataEvidence(
            before_present=before["present"],
            before_sha256=before["sha256"],
            expected_sha256=expected["sha256"],
        ),
        writable_state=state,
    )


def _decode_legacy_metadata_journal(state: dict) -> _MetadataJournal:
    phase = state["phase"]
    if phase == TRANSACTION_PHASE_PREPARED:
        decoded_phase = COORDINATOR_PHASE_PREPARED
    elif phase == TRANSACTION_PHASE_COMMITTED:
        decoded_phase = COORDINATOR_PHASE_COMMITTED
    elif phase == TRANSACTION_PHASE_ROLLED_BACK:
        decoded_phase = COORDINATOR_PHASE_ROLLED_BACK
    else:
        decoded_phase = _decode_legacy_metadata_phase(state["metadataPhase"])
    return _MetadataJournal(
        skill_name=state["skillName"],
        skill_dir=Path(state["skillDir"]),
        phase=decoded_phase,
        target_version=state["targetVersion"],
        evidence=_ControlMetadataEvidence(
            before_present=state["originalMetadataPresent"],
            before_sha256=state["originalMetadataSha256"],
            expected_sha256=state["expectedMetadataSha256"],
        ),
        writable_state=None,
    )


def _recover_metadata_journal_locked(
    transaction_root: Path,
    journal: _MetadataJournal,
) -> TransactionOutcome:
    skill_dir = journal.skill_dir
    _validate_skill_root(skill_dir)
    original_content, expected_content = _read_verified_transaction_metadata_snapshots(
        transaction_root,
        journal.evidence,
    )
    if journal.phase == COORDINATOR_PHASE_COMMITTED:
        _validate_transaction_metadata(skill_dir, journal.evidence, expected=True)
        try:
            _remove_transaction_tree(transaction_root)
        except (AgentSkillUpdaterError, OSError) as exc:
            return TransactionOutcome(
                name=journal.skill_name,
                status="error",
                installed_state="committed",
                applied=True,
                action="metadata_refreshed",
                version=journal.target_version,
                error_message=(
                    f"Committed metadata update is valid, but cleanup failed at "
                    f"{transaction_root}: {exc}"
                ),
                cleanup_residue=transaction_root,
            )
        return TransactionOutcome(
            name=journal.skill_name,
            status="recovered",
            installed_state="committed",
            applied=True,
            action="metadata_refreshed",
            version=journal.target_version,
        )
    if journal.phase == COORDINATOR_PHASE_PREPARED:
        _validate_transaction_metadata(skill_dir, journal.evidence, expected=False)
        _remove_transaction_tree(transaction_root)
        return TransactionOutcome(
            name=journal.skill_name,
            status="recovered",
            installed_state="unchanged",
            applied=False,
            action="none",
            version=journal.target_version,
        )
    if journal.phase == COORDINATOR_PHASE_ROLLED_BACK:
        _validate_transaction_metadata(skill_dir, journal.evidence, expected=False)
        _remove_transaction_tree(transaction_root)
        return TransactionOutcome(
            name=journal.skill_name,
            status="recovered",
            installed_state="rolled_back",
            applied=False,
            action="none",
            version=journal.target_version,
        )

    recovery_path = _rollback_transaction_metadata(
        transaction_root,
        journal.phase,
        skill_dir / ".openskills.json",
        original_content,
        expected_content,
    )
    if recovery_path is not None:
        return TransactionOutcome(
            name=journal.skill_name,
            status="error",
            installed_state="uncertain",
            applied=False,
            action="none",
            version=journal.target_version,
            error_message=(
                f"Recovery could not prove the original Installed State; concurrent metadata "
                f"was preserved at {recovery_path}."
            ),
            diagnostic_journal=transaction_root,
        )
    _validate_transaction_metadata(skill_dir, journal.evidence, expected=False)
    if journal.writable_state is not None:
        _set_coordinator_phase(
            transaction_root,
            journal.writable_state,
            COORDINATOR_PHASE_ROLLED_BACK,
        )
    _remove_transaction_tree(transaction_root)
    return TransactionOutcome(
        name=journal.skill_name,
        status="recovered",
        installed_state="rolled_back",
        applied=False,
        action="none",
        version=journal.target_version,
    )


def _legacy_recovery_outcome(state: dict, original_phase: str) -> TransactionOutcome:
    committed = original_phase in {
        TRANSACTION_PHASE_COMMITTED,
        GIT_TRANSACTION_PHASE_COMMITTED,
    }
    return TransactionOutcome(
        name=state["skillName"],
        status="recovered",
        installed_state="committed" if committed else "rolled_back",
        applied=committed,
        action="none",
    )


def _transaction_skill_name_hint(transaction_root: Path) -> str:
    name = transaction_root.name.removeprefix(".")
    for marker in (".transaction-", ".metadata-update-", ".git-update-", ".update-"):
        if marker in name:
            return name.split(marker, 1)[0]
    return transaction_root.name


def _is_metadata_journal_name(transaction_root: Path) -> bool:
    return any(
        marker in transaction_root.name
        for marker in (".transaction-", ".metadata-update-")
    )


def _uncertain_recovery_outcome(
    transaction_root: Path,
    error: BaseException,
) -> TransactionOutcome:
    return TransactionOutcome(
        name=_transaction_skill_name_hint(transaction_root),
        status="error",
        installed_state="uncertain",
        applied=False,
        action="none",
        error_message=(
            f"Recovery is uncertain for Diagnostic Journal {transaction_root}: {error}"
        ),
        diagnostic_journal=transaction_root,
    )


def _recover_git_transaction_from_state(transaction_root: Path, state: dict) -> None:
    skill_dir = Path(state["skillDir"])
    _validate_transaction_root(transaction_root, skill_dir.parent)
    _validate_skill_root(skill_dir)
    phase = state["phase"]
    if phase == GIT_TRANSACTION_PHASE_COMMITTED:
        _validate_git_worktree_revision(
            skill_dir,
            state["originalBranch"],
            state["expectedHead"],
            state["expectedSignature"],
            state["entryType"],
        )
        _validate_safe_metadata_path(skill_dir)
        _remove_transaction_tree(transaction_root)
        return

    if phase == GIT_TRANSACTION_PHASE_PREPARED:
        _validate_git_worktree_revision(
            skill_dir,
            state["originalBranch"],
            state["originalHead"],
            state["originalSignature"],
            state["entryType"],
        )
        _validate_safe_metadata_path(skill_dir)
        _remove_transaction_tree(transaction_root)
        return

    if phase == GIT_TRANSACTION_PHASE_ROLLED_BACK:
        _validate_git_worktree_revision(
            skill_dir,
            state["originalBranch"],
            state["originalHead"],
            state["originalSignature"],
            state["entryType"],
        )
        if state.get("recoveryPath"):
            _validate_safe_metadata_path(skill_dir)
        else:
            _validate_transaction_metadata(skill_dir, state, expected=False)
        recovery_path = _safe_recovery_directory(
            skill_dir,
            transaction_root,
            create=False,
        )
        has_recovery_data = recovery_path is not None and any(recovery_path.iterdir())
        _remove_transaction_tree(transaction_root)
        if has_recovery_data:
            raise AgentSkillUpdaterError(
                f"Interrupted Git update for '{skill_dir.name}' was rolled back; concurrent data "
                f"was preserved at {recovery_path}."
            )
        return

    original_payload = _transaction_subdirectory(transaction_root, "original")
    incoming_payload = _transaction_subdirectory(transaction_root, "incoming")
    original_metadata, expected_metadata = _read_verified_transaction_metadata_snapshots(
        transaction_root,
        state,
    )
    _safe_recovery_directory(skill_dir, transaction_root, create=False)
    recovery_path = _rollback_git_fast_forward(
        skill_dir,
        state["originalHead"],
        state["expectedHead"],
        state["originalBranch"],
        original_payload,
        incoming_payload,
        transaction_root,
        state["originalSignature"],
        state["incomingSignature"],
        state["entryType"],
    )
    metadata_path = skill_dir / ".openskills.json"
    metadata_recovery = _rollback_transaction_metadata(
        transaction_root,
        _decode_legacy_metadata_phase(state["metadataPhase"]),
        metadata_path,
        original_metadata,
        expected_metadata,
    )
    recovery_path = recovery_path or metadata_recovery
    _validate_git_worktree_revision(
        skill_dir,
        state["originalBranch"],
        state["originalHead"],
        state["originalSignature"],
        state["entryType"],
    )
    if metadata_recovery is None:
        _validate_transaction_metadata(skill_dir, state, expected=False)
    if recovery_path is not None:
        state["recoveryPath"] = str(recovery_path)
    _set_git_transaction_phase(
        transaction_root,
        state,
        GIT_TRANSACTION_PHASE_ROLLED_BACK,
    )
    _remove_transaction_tree(transaction_root)
    if recovery_path is not None:
        raise AgentSkillUpdaterError(
            f"Interrupted Git update for '{skill_dir.name}' was rolled back; concurrent data "
            f"was preserved at {recovery_path}."
        )


def _read_verified_transaction_metadata_snapshots(
    transaction_root: Path,
    state: dict | _ControlMetadataEvidence,
) -> tuple[Optional[bytes], bytes]:
    evidence = _control_metadata_evidence(state)
    original_path = _transaction_file(
        transaction_root,
        "metadata.before",
        required=evidence.before_present,
    )
    if evidence.before_present:
        original_content = original_path.read_bytes()
        if _sha256_bytes(original_content) != evidence.before_sha256:
            raise AgentSkillUpdaterError(
                f"Transaction metadata snapshot verification failed: {original_path}"
            )
    else:
        if original_path is not None:
            raise AgentSkillUpdaterError(
                f"Transaction contains unexpected metadata snapshot: {original_path}"
            )
        original_content = None

    expected_path = _transaction_file(
        transaction_root,
        "metadata.expected",
        required=True,
    )
    expected_content = expected_path.read_bytes()
    if _sha256_bytes(expected_content) != evidence.expected_sha256:
        raise AgentSkillUpdaterError(
            f"Transaction metadata snapshot verification failed: {expected_path}"
        )
    return original_content, expected_content


def _validate_git_transaction_worktree(
    skill_dir: Path,
    state: dict,
    *,
    expected: bool,
) -> None:
    prefix = "expected" if expected else "original"
    _validate_git_worktree_revision(
        skill_dir,
        state["originalBranch"],
        state[f"{prefix}Head"],
        state[f"{prefix}Signature"],
        state["entryType"],
    )
    _validate_transaction_metadata(skill_dir, state, expected=expected)


def _validate_git_worktree_revision(
    skill_dir: Path,
    expected_branch: str,
    expected_head: str,
    expected_signature: str,
    entry_type: str,
) -> str:
    current_branch = _git_output(skill_dir, ["symbolic-ref", "--short", "HEAD"])
    if current_branch != expected_branch:
        raise AgentSkillUpdaterError(
            f"Git recovery for '{skill_dir.name}' requires branch "
            f"'{expected_branch}', but '{current_branch}' is checked out."
        )
    current_head = _git_output(skill_dir, ["rev-parse", "--verify", "HEAD^{commit}"])
    if not same_git_commit(current_head, expected_head):
        raise AgentSkillUpdaterError(
            f"Git transaction HEAD verification failed for '{skill_dir.name}'."
        )
    _validate_skill_payload(skill_dir, expected_signature, entry_type=entry_type)
    return current_head


def _recover_transaction_from_state(transaction_root: Path, state: dict) -> None:
    skill_dir = Path(state["skillDir"])
    _validate_transaction_root(transaction_root, skill_dir.parent)
    _validate_skill_root(skill_dir)
    phase = state["phase"]
    if phase == TRANSACTION_PHASE_COMMITTED:
        _validate_skill_payload(
            skill_dir,
            state["expectedSignature"],
            entry_type="single-skill",
        )
        _validate_safe_metadata_path(skill_dir)
        try:
            _remove_transaction_tree(transaction_root)
        except (OSError, AgentSkillUpdaterError) as exc:
            raise AgentSkillUpdaterError(
                f"Committed update is valid, but transaction cleanup failed at "
                f"{transaction_root}: {exc}"
            ) from exc
        return

    if phase == TRANSACTION_PHASE_ROLLED_BACK:
        _validate_skill_payload(
            skill_dir,
            state["originalSignature"],
            entry_type="single-skill",
        )
        if state.get("recoveryPath"):
            _validate_safe_metadata_path(skill_dir)
        else:
            _validate_transaction_metadata(skill_dir, state, expected=False)
        recovery_path = _safe_recovery_directory(
            skill_dir,
            transaction_root,
            create=False,
        )
        has_recovery_data = recovery_path is not None and any(recovery_path.iterdir())
        try:
            _remove_transaction_tree(transaction_root)
        except (OSError, AgentSkillUpdaterError) as exc:
            raise AgentSkillUpdaterError(
                f"Rolled-back update is valid, but transaction cleanup failed at "
                f"{transaction_root}: {exc}"
            ) from exc
        if has_recovery_data:
            raise AgentSkillUpdaterError(
                f"Interrupted update for '{skill_dir.name}' was rolled back; unexpected files "
                f"were preserved at {recovery_path}."
            )
        return

    if phase in {TRANSACTION_PHASE_PREPARING, TRANSACTION_PHASE_PREPARED}:
        _validate_skill_payload(
            skill_dir,
            state["originalSignature"],
            entry_type="single-skill",
        )
        _validate_safe_metadata_path(skill_dir)
        _set_transaction_phase(transaction_root, state, TRANSACTION_PHASE_ROLLED_BACK)
        try:
            _remove_transaction_tree(transaction_root)
        except (OSError, AgentSkillUpdaterError) as exc:
            raise AgentSkillUpdaterError(
                f"Prepared update was rolled back, but transaction cleanup failed at "
                f"{transaction_root}: {exc}"
            ) from exc
        return

    recovery_path = _restore_payload_transaction(transaction_root, state)
    _remove_transaction_tree(transaction_root)
    if recovery_path is not None:
        raise AgentSkillUpdaterError(
            f"Interrupted update for '{skill_dir.name}' was rolled back; unexpected files were "
            f"preserved at {recovery_path}."
        )


def _restore_payload_transaction(transaction_root: Path, state: dict) -> Optional[Path]:
    skill_dir = Path(state["skillDir"])
    original_dir = _transaction_subdirectory(transaction_root, "original")
    incoming_dir = _transaction_subdirectory(transaction_root, "incoming")
    failed_root = _transaction_subdirectory(transaction_root, "failed", create=True)
    if directory_signature(original_dir) != state["originalSignature"]:
        raise AgentSkillUpdaterError(
            f"Original payload snapshot is incomplete at {transaction_root}."
        )
    if directory_signature(incoming_dir) != state["expectedSignature"]:
        raise AgentSkillUpdaterError(
            f"Incoming payload snapshot is incomplete at {transaction_root}."
        )
    original_metadata, expected_metadata = _read_verified_transaction_metadata_snapshots(
        transaction_root,
        state,
    )
    displaced_dir = _transaction_subdirectory(
        transaction_root,
        "displaced",
        required=False,
    )
    existing_attempts = _transaction_attempt_directories(failed_root)
    _safe_recovery_directory(skill_dir, transaction_root, create=False)
    metadata_path = skill_dir / ".openskills.json"
    if os.path.lexists(metadata_path) and (
        _is_filesystem_link(metadata_path) or not metadata_path.is_file()
    ):
        raise AgentSkillUpdaterError(f"Skill metadata path is unsafe: {metadata_path}")

    recovery_path: Optional[Path] = None
    recovery_sources = ([displaced_dir] if displaced_dir is not None else []) + existing_attempts
    recovery_sources.append(skill_dir)
    for source_dir in recovery_sources:
        preserved = _preserve_unexpected_payload(
            transaction_root,
            source_dir,
            original_dir,
            incoming_dir,
            skill_dir,
        )
        recovery_path = preserved or recovery_path

    quarantine_dir = Path(tempfile.mkdtemp(prefix="attempt-", dir=failed_root))
    _move_payload_files(skill_dir, quarantine_dir)
    _prune_empty_payload_directories(skill_dir)
    preserved = _preserve_unexpected_payload(
        transaction_root,
        quarantine_dir,
        original_dir,
        incoming_dir,
        skill_dir,
    )
    recovery_path = preserved or recovery_path
    durable_recovery_path = _safe_recovery_directory(
        skill_dir,
        transaction_root,
        create=False,
    )
    if (
        recovery_path is None
        and durable_recovery_path is not None
        and any(durable_recovery_path.iterdir())
    ):
        recovery_path = durable_recovery_path
    if recovery_path is not None:
        state["recoveryPath"] = str(recovery_path)
        _write_transaction_state(transaction_root, state)
    _install_payload_without_overwrite(original_dir, skill_dir)
    _validate_skill_payload(
        skill_dir,
        state["originalSignature"],
        entry_type="single-skill",
    )

    metadata_recovery = _rollback_transaction_metadata(
        transaction_root,
        _decode_legacy_metadata_phase(state["metadataPhase"]),
        metadata_path,
        original_metadata,
        expected_metadata,
    )
    recovery_path = recovery_path or metadata_recovery
    if recovery_path is not None:
        state["recoveryPath"] = str(recovery_path)
        _write_transaction_state(transaction_root, state)
    if metadata_recovery is None:
        _validate_transaction_metadata(skill_dir, state, expected=False)
    _set_transaction_phase(transaction_root, state, TRANSACTION_PHASE_ROLLED_BACK)
    return recovery_path


def _preserve_unexpected_payload(
    transaction_root: Path,
    quarantine_dir: Path,
    original_dir: Path,
    incoming_dir: Path,
    skill_dir: Path,
) -> Optional[Path]:
    unexpected_files: list[Path] = []
    for relative_path in sorted(_relative_file_paths(quarantine_dir), key=lambda path: path.as_posix()):
        current = quarantine_dir / relative_path
        matches_original = _file_matches_snapshot(current, original_dir / relative_path)
        matches_incoming = _file_matches_snapshot(current, incoming_dir / relative_path)
        if not matches_original and not matches_incoming:
            unexpected_files.append(relative_path)

    original_directories = _relative_directory_paths(original_dir)
    incoming_directories = _relative_directory_paths(incoming_dir)
    unexpected_directories = _relative_directory_paths(quarantine_dir) - (
        original_directories | incoming_directories
    )
    if not unexpected_files and not unexpected_directories:
        return None

    recovery_root = _safe_recovery_directory(
        skill_dir,
        transaction_root,
        create=True,
    )
    for relative_path in sorted(unexpected_directories, key=lambda path: path.as_posix()):
        destination = _safe_recovery_path(recovery_root, relative_path)
        if os.path.lexists(destination) and not destination.is_dir():
            destination = _safe_recovery_path(
                recovery_root,
                Path(quarantine_dir.name) / relative_path,
            )
        _safe_recovery_subdirectory(
            recovery_root,
            destination.relative_to(recovery_root),
        )
    for relative_path in unexpected_files:
        source = quarantine_dir / relative_path
        destination = _safe_recovery_path(recovery_root, relative_path)
        if os.path.lexists(destination):
            if destination.is_file() and destination.read_bytes() == source.read_bytes():
                continue
            destination = _safe_recovery_path(
                recovery_root,
                Path(quarantine_dir.name) / relative_path,
            )
        if os.path.lexists(destination):
            if destination.is_file() and destination.read_bytes() == source.read_bytes():
                continue
            raise AgentSkillUpdaterError(
                f"Recovery destination already contains different data: {destination}"
            )
        _copy_payload_file_exclusive(source, destination)
    return recovery_root


def _file_matches_snapshot(current: Path, snapshot: Path) -> bool:
    return snapshot.is_file() and current.read_bytes() == snapshot.read_bytes()


def _commit_transaction_metadata(
    transaction_root: Path,
    state: dict,
    metadata_path: Path,
    content: bytes,
    expected_content: Optional[bytes],
    *,
    phases: _MetadataPhaseProtocol,
) -> None:
    if expected_content is None:
        if os.path.lexists(metadata_path):
            raise AgentSkillUpdaterError(
                f"Concurrent write detected while creating control file: {metadata_path}"
            )
    elif (
        not os.path.lexists(metadata_path)
        or _is_filesystem_link(metadata_path)
        or not metadata_path.is_file()
    ):
        raise AgentSkillUpdaterError(
            f"Concurrent write detected while replacing control file: {metadata_path}"
        )

    displaced = transaction_root / "metadata.displaced"
    if os.path.lexists(displaced):
        raise AgentSkillUpdaterError(
            f"Metadata capture path already exists in transaction: {displaced}"
        )
    phases.set_phase(
        transaction_root,
        state,
        phases.capturing,
    )
    if expected_content is not None:
        os.replace(metadata_path, displaced)
        if (
            _is_filesystem_link(displaced)
            or not displaced.is_file()
            or displaced.read_bytes() != expected_content
        ):
            _publish_metadata_file_if_absent(displaced, metadata_path)
            raise AgentSkillUpdaterError(
                f"Concurrent write detected while replacing control file: {metadata_path}"
            )
    phases.set_phase(
        transaction_root,
        state,
        phases.captured,
    )

    publish_source = _transaction_file(
        transaction_root,
        "metadata.publish",
        required=True,
    )
    if publish_source.read_bytes() != content:
        raise AgentSkillUpdaterError(
            f"Transaction metadata publish snapshot is invalid: {publish_source}"
        )
    phases.set_phase(
        transaction_root,
        state,
        phases.publishing,
    )
    try:
        published = _publish_metadata_file_if_absent(publish_source, metadata_path)
    except AgentSkillUpdaterError:
        phases.set_phase(
            transaction_root,
            state,
            phases.publish_failed,
        )
        raise
    if not published:
        phases.set_phase(
            transaction_root,
            state,
            phases.publish_failed,
        )
        raise AgentSkillUpdaterError(
            f"Concurrent write detected while publishing control file: {metadata_path}"
        )
    if (
        _is_filesystem_link(metadata_path)
        or not metadata_path.is_file()
        or not os.path.samefile(metadata_path, publish_source)
        or metadata_path.read_bytes() != content
    ):
        raise AgentSkillUpdaterError(
            f"Published transaction metadata failed verification: {metadata_path}"
        )
    phases.set_phase(
        transaction_root,
        state,
        phases.published,
    )


def _prepare_transaction_metadata_files(
    transaction_root: Path,
    original_content: Optional[bytes],
    expected_content: bytes,
) -> None:
    if original_content is not None:
        _write_bytes_atomic(transaction_root / "metadata.before", original_content)
    _write_bytes_atomic(transaction_root / "metadata.expected", expected_content)
    publish_source = transaction_root / "metadata.publish"
    _write_bytes_atomic(publish_source, expected_content)
    link_check = transaction_root / "metadata.link-check"
    try:
        os.link(publish_source, link_check)
    except OSError as exc:
        raise AgentSkillUpdaterError(
            f"Metadata transactions require same-volume hard-link support at {transaction_root}: {exc}"
        ) from exc
    link_check.unlink()


def _publish_metadata_file_if_absent(source: Path, destination: Path) -> bool:
    if _is_filesystem_link(source) or not source.is_file():
        return False
    try:
        os.link(source, destination)
    except FileExistsError:
        return False
    except OSError as exc:
        raise AgentSkillUpdaterError(
            f"Unable to publish transaction metadata at {destination}: {exc}"
        ) from exc
    return True


def _sha256_bytes(content: Optional[bytes]) -> Optional[str]:
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _validate_transaction_metadata(
    skill_dir: Path,
    state: dict | _ControlMetadataEvidence,
    *,
    expected: bool,
) -> None:
    evidence = _control_metadata_evidence(state)
    prefix = "expected" if expected else "original"
    metadata_path = skill_dir / ".openskills.json"
    should_exist = True if expected else evidence.before_present
    expected_hash = evidence.expected_sha256 if expected else evidence.before_sha256
    path_exists = os.path.lexists(metadata_path)
    regular_file = (
        path_exists
        and not _is_filesystem_link(metadata_path)
        and metadata_path.is_file()
    )
    if (should_exist and not regular_file) or (not should_exist and path_exists):
        raise AgentSkillUpdaterError(
            f"Transaction metadata verification failed for '{skill_dir.name}'."
        )
    if should_exist and _sha256_bytes(metadata_path.read_bytes()) != expected_hash:
        raise AgentSkillUpdaterError(
            f"Transaction metadata verification failed for '{skill_dir.name}'."
        )


def _control_metadata_evidence(
    value: dict | _ControlMetadataEvidence,
) -> _ControlMetadataEvidence:
    if isinstance(value, _ControlMetadataEvidence):
        return value
    return _ControlMetadataEvidence(
        before_present=value["originalMetadataPresent"],
        before_sha256=value["originalMetadataSha256"],
        expected_sha256=value["expectedMetadataSha256"],
    )


def _validate_safe_metadata_path(skill_dir: Path) -> None:
    metadata_path = skill_dir / ".openskills.json"
    if os.path.lexists(metadata_path) and (
        _is_filesystem_link(metadata_path) or not metadata_path.is_file()
    ):
        raise AgentSkillUpdaterError(f"Skill metadata path is unsafe: {metadata_path}")


def _write_transaction_state(transaction_root: Path, state: dict) -> None:
    _write_json_atomic(transaction_root / TRANSACTION_STATE_FILENAME, state)


def _set_transaction_phase(transaction_root: Path, state: dict, phase: str) -> None:
    if phase not in TRANSACTION_PHASES:
        raise AgentSkillUpdaterError(f"Invalid transaction phase: {phase}")
    next_state = {**state, "phase": phase}
    _write_transaction_state(transaction_root, next_state)
    state.clear()
    state.update(next_state)


def _set_git_transaction_phase(transaction_root: Path, state: dict, phase: str) -> None:
    if phase not in GIT_TRANSACTION_PHASES:
        raise AgentSkillUpdaterError(f"Invalid Git transaction phase: {phase}")
    next_state = {**state, "phase": phase}
    _write_json_atomic(transaction_root / TRANSACTION_STATE_FILENAME, next_state)
    state.clear()
    state.update(next_state)


def _set_transaction_metadata_phase(
    transaction_root: Path,
    state: dict,
    phase: str,
) -> None:
    if phase not in METADATA_PHASES:
        raise AgentSkillUpdaterError(f"Invalid metadata transaction phase: {phase}")
    next_state = {**state, "metadataPhase": phase}
    _write_json_atomic(transaction_root / TRANSACTION_STATE_FILENAME, next_state)
    state.clear()
    state.update(next_state)


def _set_coordinator_phase(
    transaction_root: Path,
    state: dict,
    phase: str,
) -> None:
    if phase not in COORDINATOR_PHASES:
        raise AgentSkillUpdaterError(f"Invalid Transaction Coordinator phase: {phase}")
    next_state = {**state, "phase": phase}
    _write_json_atomic(transaction_root / TRANSACTION_STATE_FILENAME, next_state)
    state.clear()
    state.update(next_state)


def _set_coordinator_metadata_phase(
    transaction_root: Path,
    state: dict,
    phase: str,
) -> None:
    _set_coordinator_phase(transaction_root, state, phase)


LEGACY_METADATA_PHASES = _MetadataPhaseProtocol(
    set_phase=_set_transaction_metadata_phase,
    capturing=METADATA_PHASE_CAPTURING,
    captured=METADATA_PHASE_CAPTURED,
    publishing=METADATA_PHASE_PUBLISHING,
    publish_failed=METADATA_PHASE_PUBLISH_FAILED,
    published=METADATA_PHASE_PUBLISHED,
)
COORDINATOR_METADATA_PHASES = _MetadataPhaseProtocol(
    set_phase=_set_coordinator_metadata_phase,
    capturing=COORDINATOR_PHASE_CAPTURING_METADATA,
    captured=COORDINATOR_PHASE_METADATA_CAPTURED,
    publishing=COORDINATOR_PHASE_PUBLISHING_METADATA,
    publish_failed=COORDINATOR_PHASE_METADATA_PUBLISH_FAILED,
    published=COORDINATOR_PHASE_METADATA_PUBLISHED,
)


def _set_git_transaction_expected_signature(
    transaction_root: Path,
    state: dict,
    expected_signature: str,
) -> None:
    next_state = {**state, "expectedSignature": expected_signature}
    _write_json_atomic(transaction_root / TRANSACTION_STATE_FILENAME, next_state)
    state.clear()
    state.update(next_state)


def _read_git_transaction_state(transaction_root: Path, skills_root: Path) -> dict:
    try:
        state_path = _transaction_file(
            transaction_root,
            TRANSACTION_STATE_FILENAME,
            required=True,
        )
        state = json.loads(
            state_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise AgentSkillUpdaterError(
            f"Cannot read Git update transaction state at {transaction_root}: {exc}"
        ) from exc
    required = {
        "version",
        "transactionType",
        "skillName",
        "skillDir",
        "entryType",
        "phase",
        "metadataPhase",
        "originalBranch",
        "originalHead",
        "expectedHead",
        "originalSignature",
        "incomingSignature",
        "expectedSignature",
        "originalMetadataPresent",
        "originalMetadataSha256",
        "expectedMetadataPresent",
        "expectedMetadataSha256",
    }
    if not isinstance(state, dict) or not required.issubset(state):
        raise AgentSkillUpdaterError(
            f"Incomplete Git update transaction state at {transaction_root}."
        )
    string_keys = {
        "transactionType",
        "skillName",
        "skillDir",
        "entryType",
        "phase",
        "metadataPhase",
        "originalBranch",
        "originalHead",
        "expectedHead",
        "originalSignature",
        "incomingSignature",
        "expectedSignature",
    }
    if not all(isinstance(state[key], str) for key in string_keys):
        raise AgentSkillUpdaterError(f"Invalid Git update transaction state at {transaction_root}.")
    _validate_transaction_metadata_state(state, transaction_root)
    if (
        state["version"] != GIT_TRANSACTION_STATE_VERSION
        or state["transactionType"] != GIT_TRANSACTION_TYPE
        or state["entryType"] not in {"single-skill", "skill-pack"}
        or state["phase"] not in GIT_TRANSACTION_PHASES
        or state["metadataPhase"] not in METADATA_PHASES
        or normalize_git_commit(state["originalHead"]) is None
        or normalize_git_commit(state["expectedHead"]) is None
    ):
        raise AgentSkillUpdaterError(
            f"Unsupported Git update transaction state at {transaction_root}."
        )
    if "recoveryPath" in state and not isinstance(state["recoveryPath"], str):
        raise AgentSkillUpdaterError(f"Invalid Git update transaction state at {transaction_root}.")
    skill_dir = Path(state["skillDir"])
    if not _same_path(skill_dir.parent, skills_root):
        raise AgentSkillUpdaterError(
            f"Git transaction {transaction_root} points outside skills root {skills_root}."
        )
    expected_prefix = f".{state['skillName']}.git-update-"
    if skill_dir.name != state["skillName"] or not transaction_root.name.startswith(expected_prefix):
        raise AgentSkillUpdaterError(f"Git transaction identity mismatch at {transaction_root}.")
    return state


def _read_transaction_state(transaction_root: Path, skills_root: Path) -> dict:
    try:
        state_path = _transaction_file(
            transaction_root,
            TRANSACTION_STATE_FILENAME,
            required=True,
        )
        state = json.loads(
            state_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise AgentSkillUpdaterError(
            f"Cannot read update transaction state at {transaction_root}: {exc}"
        ) from exc
    required = {
        "version",
        "transactionType",
        "skillName",
        "skillDir",
        "phase",
        "metadataPhase",
        "originalSignature",
        "expectedSignature",
        "originalMetadataPresent",
        "originalMetadataSha256",
        "expectedMetadataPresent",
        "expectedMetadataSha256",
    }
    if not isinstance(state, dict) or not required.issubset(state):
        raise AgentSkillUpdaterError(f"Incomplete update transaction state at {transaction_root}.")
    string_keys = {
        "transactionType",
        "skillName",
        "skillDir",
        "phase",
        "metadataPhase",
        "originalSignature",
        "expectedSignature",
    }
    if not all(isinstance(state[key], str) for key in string_keys):
        raise AgentSkillUpdaterError(f"Invalid update transaction state at {transaction_root}.")
    _validate_transaction_metadata_state(state, transaction_root)
    if "recoveryPath" in state and not isinstance(state["recoveryPath"], str):
        raise AgentSkillUpdaterError(f"Invalid update transaction state at {transaction_root}.")
    if (
        state["version"] != TRANSACTION_STATE_VERSION
        or state["transactionType"] != SNAPSHOT_TRANSACTION_TYPE
        or state["phase"] not in TRANSACTION_PHASES
        or state["metadataPhase"] not in METADATA_PHASES
    ):
        raise AgentSkillUpdaterError(f"Unsupported update transaction state at {transaction_root}.")
    skill_dir = Path(state["skillDir"])
    if not _same_path(skill_dir.parent, skills_root):
        raise AgentSkillUpdaterError(
            f"Transaction {transaction_root} points outside skills root {skills_root}."
        )
    expected_prefix = f".{state['skillName']}.update-"
    if skill_dir.name != state["skillName"] or not transaction_root.name.startswith(expected_prefix):
        raise AgentSkillUpdaterError(f"Transaction identity mismatch at {transaction_root}.")
    return state


def _read_transaction_type(transaction_root: Path) -> str:
    try:
        state_path = _transaction_file(
            transaction_root,
            TRANSACTION_STATE_FILENAME,
            required=True,
        )
        state = json.loads(
            state_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise AgentSkillUpdaterError(
            f"Cannot read update transaction state at {transaction_root}: {exc}"
        ) from exc
    if not isinstance(state, dict):
        raise AgentSkillUpdaterError(f"Invalid update transaction state at {transaction_root}.")
    transaction_type = state.get("transactionType")
    if transaction_type in {
        COORDINATOR_TRANSACTION_TYPE,
        GIT_TRANSACTION_TYPE,
        SNAPSHOT_TRANSACTION_TYPE,
        METADATA_TRANSACTION_TYPE,
    }:
        return transaction_type
    raise AgentSkillUpdaterError(
        f"Unsupported transaction type at {transaction_root}: {transaction_type}"
    )


def _read_typed_transaction_state(
    transaction_root: Path,
    skills_root: Path,
    transaction_type: str,
) -> dict:
    if transaction_type == GIT_TRANSACTION_TYPE:
        return _read_git_transaction_state(transaction_root, skills_root)
    if transaction_type == SNAPSHOT_TRANSACTION_TYPE:
        return _read_transaction_state(transaction_root, skills_root)
    return _read_metadata_transaction_state(transaction_root, skills_root)


def _read_metadata_transaction_state(transaction_root: Path, skills_root: Path) -> dict:
    try:
        state_path = _transaction_file(
            transaction_root,
            TRANSACTION_STATE_FILENAME,
            required=True,
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentSkillUpdaterError(
            f"Cannot read metadata transaction state at {transaction_root}: {exc}"
        ) from exc
    required = {
        "version",
        "transactionType",
        "skillName",
        "skillDir",
        "phase",
        "metadataPhase",
        "originalMetadataPresent",
        "originalMetadataSha256",
        "expectedMetadataPresent",
        "expectedMetadataSha256",
        "targetVersion",
    }
    if not isinstance(state, dict) or not required.issubset(state):
        raise AgentSkillUpdaterError(
            f"Incomplete metadata transaction state at {transaction_root}."
        )
    string_keys = {
        "transactionType",
        "skillName",
        "skillDir",
        "phase",
        "metadataPhase",
        "targetVersion",
    }
    if not all(isinstance(state[key], str) for key in string_keys):
        raise AgentSkillUpdaterError(
            f"Invalid metadata transaction state at {transaction_root}."
        )
    _validate_transaction_metadata_state(state, transaction_root)
    if (
        state["version"] != METADATA_TRANSACTION_STATE_VERSION
        or state["transactionType"] != METADATA_TRANSACTION_TYPE
        or state["phase"] not in METADATA_TRANSACTION_PHASES
        or state["metadataPhase"] not in METADATA_PHASES
        or (
            state["phase"] == TRANSACTION_PHASE_PREPARED
            and state["metadataPhase"] != METADATA_PHASE_PREPARED
        )
        or (
            state["phase"] == TRANSACTION_PHASE_COMMITTED
            and state["metadataPhase"] != METADATA_PHASE_PUBLISHED
        )
    ):
        raise AgentSkillUpdaterError(
            f"Unsupported metadata transaction state at {transaction_root}."
        )
    if "recoveryPath" in state and not isinstance(state["recoveryPath"], str):
        raise AgentSkillUpdaterError(
            f"Invalid metadata transaction state at {transaction_root}."
        )
    skill_dir = Path(state["skillDir"])
    if not _same_path(skill_dir.parent, skills_root):
        raise AgentSkillUpdaterError(
            f"Metadata transaction {transaction_root} points outside skills root {skills_root}."
        )
    expected_prefix = f".{state['skillName']}.metadata-update-"
    if skill_dir.name != state["skillName"] or not transaction_root.name.startswith(
        expected_prefix
    ):
        raise AgentSkillUpdaterError(
            f"Metadata transaction identity mismatch at {transaction_root}."
        )
    return state


def _validate_transaction_metadata_state(state: dict, transaction_root: Path) -> None:
    if not isinstance(state["originalMetadataPresent"], bool) or state[
        "expectedMetadataPresent"
    ] is not True:
        raise AgentSkillUpdaterError(f"Invalid update transaction state at {transaction_root}.")
    original_digest = state["originalMetadataSha256"]
    if state["originalMetadataPresent"]:
        if not isinstance(original_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", original_digest
        ):
            raise AgentSkillUpdaterError(
                f"Invalid update transaction metadata hash at {transaction_root}."
            )
    elif original_digest is not None:
        raise AgentSkillUpdaterError(
            f"Invalid update transaction metadata state at {transaction_root}."
        )
    expected_digest = state["expectedMetadataSha256"]
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise AgentSkillUpdaterError(
            f"Invalid update transaction metadata hash at {transaction_root}."
        )


def _looks_like_transaction_directory(path: Path) -> bool:
    if not _looks_like_transaction_name(path.name):
        return False
    return (
        (path / TRANSACTION_STATE_FILENAME).is_file()
        or (path / TRANSACTION_MARKER_FILENAME).is_file()
    )


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


@contextmanager
def skill_update_lock(skill_dir: Path) -> Iterator[None]:
    lock_root = _safe_direct_child_directory(
        skill_dir.parent,
        ".skills-updater-locks",
        create=True,
        label="Lock",
    )
    if lock_root is None:
        raise AgentSkillUpdaterError("Lock directory could not be created.")
    identity = hashlib.sha256(str(skill_dir.resolve()).casefold().encode("utf-8")).hexdigest()[:24]
    lock_path = lock_root / f"{identity}.lock"
    if os.path.lexists(lock_path) and (
        _is_filesystem_link(lock_path) or not lock_path.is_file()
    ):
        raise AgentSkillUpdaterError(f"Lock file is unsafe: {lock_path}")
    try:
        handle = lock_path.open("x+b")
    except FileExistsError:
        if _is_filesystem_link(lock_path) or not lock_path.is_file():
            raise AgentSkillUpdaterError(f"Lock file is unsafe: {lock_path}")
        handle = lock_path.open("r+b")
    with handle:
        if _is_filesystem_link(lock_path) or not lock_path.is_file():
            raise AgentSkillUpdaterError(f"Lock file is unsafe: {lock_path}")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AgentSkillUpdaterError(
                f"Skill '{skill_dir.name}' is already being updated by another process."
            ) from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _prune_empty_payload_directories(root: Path) -> None:
    directories: list[Path] = []
    for current_root, directory_names, _ in os.walk(
        root,
        topdown=True,
        onerror=_raise_walk_error,
        followlinks=False,
    ):
        current_path = Path(current_root)
        directory_names[:] = [
            name
            for name in directory_names
            if is_skill_payload_path(current_path / name, root)
        ]
        directories.extend(current_path / name for name in directory_names)

    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            if directory.exists() and any(directory.iterdir()):
                continue
            raise


def _stage_openspec_generated_skill(
    source: AgentSkillSource,
    stage_root: Path,
    revision: Optional[str] = None,
) -> Path:
    if not source.repo_url:
        raise AgentSkillUpdaterError(f"Generated skill '{source.name}' is missing repoUrl metadata.")
    repo_url = source.repo_url
    _require_remote_probe_ready(source)
    exact_revision = revision or _fetch_remote_commit_sha(repo_url)
    exact_revision = _require_git_commit(
        exact_revision,
        f"Generated skill '{source.name}' source revision",
    )
    owner, repo = _parse_github_repo(repo_url)
    _require_remote_probe_ready(source)
    repo_root = _download_repo_archive(owner, repo, exact_revision, stage_root)
    _require_remote_probe_ready(source)
    _run(["npm", "ci", "--ignore-scripts"], cwd=repo_root)
    _require_remote_probe_ready(source)
    _run(["node", "build.js"], cwd=repo_root)
    _require_remote_probe_ready(source)

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
        [
            "node",
            "--input-type=module",
            "-e",
            node_script,
            str(destination),
            f"openspec-{source.workflow_id}",
        ],
        cwd=repo_root,
    )
    _require_remote_probe_ready(source)
    return destination


def _download_repo_archive(owner: str, repo: str, ref: str, temp_root: Path) -> Path:
    commit = _require_git_commit(ref, f"Archive download for {owner}/{repo}")
    archive_url = f"https://codeload.github.com/{owner}/{repo}/zip/{commit}"
    request = urllib.request.Request(archive_url, headers={"User-Agent": DEFAULT_USER_AGENT})
    temp_root.mkdir(parents=True, exist_ok=True)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise AgentSkillUpdaterError(
            f"Download failed for {owner}/{repo}@{commit}: HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise AgentSkillUpdaterError(
            f"Download failed for {owner}/{repo}@{commit}: {exc.reason}"
        ) from exc

    zip_path = temp_root / f"{repo}-{commit}.zip"
    zip_path.write_bytes(payload)
    extract_dir = temp_root / f"{repo}-{commit}"
    extract_dir.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(zip_path) as archive:
        _validate_zip_members(archive, extract_dir)
        archive.extractall(extract_dir)

    extracted_entries = list(extract_dir.iterdir())
    if len(extracted_entries) != 1 or not extracted_entries[0].is_dir():
        raise AgentSkillUpdaterError(f"Unexpected archive layout for {owner}/{repo}@{commit}.")
    return extracted_entries[0]


def _validate_zip_members(archive: zipfile.ZipFile, extract_dir: Path) -> None:
    seen: set[str] = set()
    root = extract_dir.resolve()
    for member in archive.infolist():
        raw_name = member.filename
        relative = PurePosixPath(raw_name)
        windows_path = PureWindowsPath(raw_name)
        if (
            not raw_name
            or "\\" in raw_name
            or "\0" in raw_name
            or relative.is_absolute()
            or bool(windows_path.drive)
            or bool(windows_path.root)
            or ".." in relative.parts
        ):
            raise AgentSkillUpdaterError(f"Archive contains an unsafe path: {raw_name!r}")
        normalized = relative.as_posix().rstrip("/")
        key = normalized.casefold()
        if not normalized or key in seen:
            raise AgentSkillUpdaterError(
                f"Archive contains a duplicate or ambiguous path: {raw_name!r}"
            )
        seen.add(key)
        destination = extract_dir.joinpath(*relative.parts).resolve()
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise AgentSkillUpdaterError(
                f"Archive path escapes the extraction root: {raw_name!r}"
            ) from exc
        file_type = (member.external_attr >> 16) & 0o170000
        if file_type == stat.S_IFLNK:
            raise AgentSkillUpdaterError(f"Archive contains a symbolic link: {raw_name!r}")


def _parse_github_repo(repo_url: str) -> tuple[str, str]:
    raw_url = repo_url.strip().replace("\\", "/").rstrip("/")
    if "://" in raw_url:
        raw_parsed = urllib.parse.urlparse(raw_url)
        if raw_parsed.query or raw_parsed.fragment or raw_parsed.params:
            raise AgentSkillUpdaterError(f"Invalid GitHub repo URL: {sanitize_repo_url(repo_url)}")
    sanitized_url = sanitize_repo_url(repo_url)
    parsed = urllib.parse.urlparse(sanitized_url)
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        raise AgentSkillUpdaterError(f"Only GitHub URLs are supported: {sanitized_url}")
    path = parsed.path.removesuffix(".git")
    parts = [part for part in path.split("/") if part]
    if (
        len(parts) != 2
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)
    ):
        raise AgentSkillUpdaterError(f"Invalid GitHub repo URL: {sanitized_url}")
    return parts[0], parts[1]


def _apply_metadata_only_transaction_locked(
    source: AgentSkillSource,
    observation: RemoteObservation,
    installed_base_version: str,
    commit_state_validator: Callable[[str], None],
) -> TransactionOutcome:
    metadata_version = (
        observation.version if _is_openspec_source(source) else observation.revision
    )
    update = AgentSkillUpdate(
        source=source,
        staged_dir=None,
        status="up_to_date",
        installed_base_version=installed_base_version,
        local_version=_read_local_version(source),
        remote_version=metadata_version,
    )
    commit_state_validator(installed_base_version)
    metadata_snapshot, original_content = _require_remote_updates_enabled(
        source,
        installed_base_version,
    )
    metadata_path = source.metadata_path
    if metadata_path is None or metadata_snapshot is None or original_content is None:
        raise AgentSkillUpdaterError(
            f"Remotely managed skill '{source.name}' requires canonical metadata."
        )
    metadata, changed = _build_refreshed_metadata(update, metadata_snapshot)
    if not changed:
        return TransactionOutcome(
            name=source.name,
            status="up_to_date",
            installed_state="unchanged",
            applied=False,
            action="none",
            version=metadata_version,
        )
    expected_content = _json_payload_bytes(metadata)
    transaction_root = Path(
        tempfile.mkdtemp(
            prefix=f".{source.name}.transaction-",
            dir=source.local_dir.parent,
        )
    )
    state = {
        "version": COORDINATOR_TRANSACTION_STATE_VERSION,
        "transactionType": COORDINATOR_TRANSACTION_TYPE,
        "transactionKind": METADATA_ONLY_TRANSACTION_KIND,
        "skillName": source.name,
        "skillDir": str(source.local_dir.resolve()),
        "phase": COORDINATOR_PHASE_PREPARED,
        "targetRevision": observation.revision,
        "targetVersion": observation.version,
        "evidence": {
            "beforeMetadata": {
                "present": True,
                "sha256": _sha256_bytes(original_content),
            },
            "expectedMetadata": {
                "present": True,
                "sha256": _sha256_bytes(expected_content),
            },
        },
    }
    evidence = _ControlMetadataEvidence(
        before_present=True,
        before_sha256=_sha256_bytes(original_content),
        expected_sha256=_sha256_bytes(expected_content),
    )
    try:
        _prepare_transaction_metadata_files(
            transaction_root,
            original_content,
            expected_content,
        )
        _write_json_atomic(transaction_root / TRANSACTION_STATE_FILENAME, state)
        _write_bytes_atomic(transaction_root / TRANSACTION_MARKER_FILENAME, b"1\n")
    except Exception as exc:
        try:
            _remove_transaction_tree(transaction_root)
        except (OSError, AgentSkillUpdaterError) as cleanup_exc:
            raise AgentSkillUpdaterError(
                f"Unable to prepare metadata refresh for '{source.name}': {exc}. "
                f"Temporary transaction cleanup also failed at {transaction_root}: {cleanup_exc}"
            ) from exc
        raise AgentSkillUpdaterError(
            f"Unable to prepare metadata refresh for '{source.name}': {exc}"
        ) from exc

    try:
        commit_state_validator(installed_base_version)
        _require_remote_updates_enabled(
            source,
            installed_base_version,
        )
        _commit_transaction_metadata(
            transaction_root,
            state,
            metadata_path,
            expected_content,
            original_content,
            phases=COORDINATOR_METADATA_PHASES,
        )
        _validate_transaction_metadata(source.local_dir, evidence, expected=True)
        commit_state_validator(metadata_version)
        _set_coordinator_phase(
            transaction_root,
            state,
            COORDINATOR_PHASE_COMMITTED,
        )
    except Exception as exc:
        if state["phase"] == COORDINATOR_PHASE_PREPARED:
            _remove_transaction_tree(transaction_root)
            return TransactionOutcome(
                name=source.name,
                status="error",
                installed_state="unchanged",
                applied=False,
                action="none",
                version=metadata_version,
                error_message=f"Metadata refresh failed for '{source.name}': {exc}",
            )
        try:
            recovery_path = _rollback_transaction_metadata(
                transaction_root,
                state["phase"],
                metadata_path,
                original_content,
                expected_content,
            )
            if recovery_path is not None:
                return TransactionOutcome(
                    name=source.name,
                    status="error",
                    installed_state="uncertain",
                    applied=False,
                    action="none",
                    version=metadata_version,
                    error_message=(
                        f"Metadata refresh failed for '{source.name}': {exc}. "
                        f"Concurrent metadata was preserved at {recovery_path}."
                    ),
                    diagnostic_journal=transaction_root,
                )
            _validate_transaction_metadata(source.local_dir, evidence, expected=False)
            _set_coordinator_phase(
                transaction_root,
                state,
                COORDINATOR_PHASE_ROLLED_BACK,
            )
            _remove_transaction_tree(transaction_root)
        except Exception as rollback_exc:
            return TransactionOutcome(
                name=source.name,
                status="error",
                installed_state="uncertain",
                applied=False,
                action="none",
                version=metadata_version,
                error_message=(
                    f"Metadata refresh failed for '{source.name}': {exc}. "
                    f"Rollback also failed: {rollback_exc}. Recovery data remains at "
                    f"{transaction_root}."
                ),
                diagnostic_journal=transaction_root,
            )
        return TransactionOutcome(
            name=source.name,
            status="error",
            installed_state="rolled_back",
            applied=False,
            action="none",
            version=metadata_version,
            error_message=f"Metadata refresh failed for '{source.name}': {exc}",
        )

    try:
        _remove_transaction_tree(transaction_root)
    except (OSError, AgentSkillUpdaterError) as cleanup_exc:
        return TransactionOutcome(
            name=source.name,
            status="error",
            installed_state="committed",
            applied=True,
            action="metadata_refreshed",
            version=metadata_version,
            error_message=(
                f"Metadata refresh for '{source.name}' committed, but transaction cleanup "
                f"failed at {transaction_root}: {cleanup_exc}"
            ),
            cleanup_residue=transaction_root,
        )
    return TransactionOutcome(
        name=source.name,
        status="up_to_date",
        installed_state="committed",
        applied=True,
        action="metadata_refreshed",
        version=metadata_version,
    )


def _build_refreshed_metadata(
    update: AgentSkillUpdate,
    metadata_snapshot: dict,
) -> tuple[dict, bool]:
    metadata = dict(metadata_snapshot)
    original = dict(metadata)
    for key, value in {
        "source": update.source.source,
        "sourceType": update.source.source_type,
        "repoUrl": sanitize_repo_url(update.source.repo_url) if update.source.repo_url else None,
        "subpath": update.source.subpath,
        "generator": update.source.generator,
        "workflowId": update.source.workflow_id,
    }.items():
        if value is not None:
            metadata[key] = value
    metadata["installedAt"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if update.remote_version:
        if _is_openspec_source(update.source):
            metadata["generatedByVersion"] = update.remote_version
        else:
            metadata["installedBaseVersion"] = update.remote_version
    return metadata, metadata != original


def _json_payload_bytes(payload: dict) -> bytes:
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write_json_atomic(path: Path, payload: dict) -> None:
    _write_bytes_atomic(path, _json_payload_bytes(payload))


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f"{path.name}.tmp-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _read_local_version(source: AgentSkillSource) -> str:
    if _is_openspec_source(source):
        version = _read_generated_by_version(source.local_dir / "SKILL.md")
        return version or "unknown"

    if is_git_worktree_skill(source.local_dir):
        return _git_output(source.local_dir, ["rev-parse", "--verify", "HEAD^{commit}"])

    return _read_installed_base_version(source)


def _read_local_only_version(source: AgentSkillSource) -> str:
    if is_git_worktree_skill(source.local_dir):
        return _git_output(source.local_dir, ["rev-parse", "--verify", "HEAD^{commit}"])
    return "local"


def _read_installed_base_version(source: AgentSkillSource) -> str:
    if _is_openspec_source(source):
        version = _read_generated_by_version(source.local_dir / "SKILL.md")
        return version or "unknown"

    if source.metadata_path and os.path.lexists(source.metadata_path):
        metadata = _read_json_object(source.metadata_path, "Skill metadata")
        if "sourceCommitSha" in metadata:
            raise AgentSkillUpdaterError(
                "Legacy sourceCommitSha is unsupported; rename it to installedBaseVersion "
                f"in {source.metadata_path}."
            )
        installed_base = _as_optional_str(metadata.get("installedBaseVersion"))
        normalized = normalize_git_commit(installed_base)
        if installed_base and normalized is None:
            raise AgentSkillUpdaterError(
                f"installedBaseVersion in {source.metadata_path} must be a 12-40 character "
                "hexadecimal Git commit SHA."
            )
        if normalized:
            return normalized
    raise AgentSkillUpdaterError(
        f"installedBaseVersion is required in .openskills.json for remotely managed Skill "
        f"'{source.name}'."
    )


def _read_generated_by_version(skill_file: Path) -> Optional[str]:
    if not skill_file.exists():
        return None
    for line in skill_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("generatedBy:"):
            _, _, value = line.partition(":")
            return value.strip().strip('"')
    return None


def _fetch_remote_commit_sha(repo_url: str) -> str:
    remote_url = sanitize_repo_url(repo_url)
    _parse_github_repo(remote_url)
    try:
        result = subprocess.run(
            _resolve_command(["git", "ls-remote", remote_url, "HEAD"]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise AgentSkillUpdaterError(
                f"Unable to resolve remote commit for {remote_url}: "
                f"git ls-remote exited {result.returncode}."
            )
    except OSError as exc:
        raise AgentSkillUpdaterError(
            f"Unable to resolve remote commit for {remote_url}: "
            f"git ls-remote failed with {type(exc).__name__}."
        ) from exc
    lines = result.stdout.strip().splitlines()
    if len(lines) != 1:
        raise AgentSkillUpdaterError(
            f"Unable to resolve remote commit for {remote_url}: unexpected git ls-remote output."
        )
    fields = lines[0].split()
    if len(fields) != 2 or fields[1] != "HEAD" or not re.fullmatch(r"[0-9a-fA-F]{40}", fields[0]):
        raise AgentSkillUpdaterError(
            f"Unable to resolve remote commit for {remote_url}: invalid HEAD response."
        )
    return fields[0].lower()


def _run(command: list[str], cwd: Path) -> None:
    if len(command) >= 3 and command[0] == "git" and command[1] == "-C":
        _validate_git_control_entry(Path(command[2]))
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
    if not resolved:
        raise AgentSkillUpdaterError(f"Command is not available on PATH: {executable}")
    return [resolved, *command[1:]]


def _is_openspec_source(source: AgentSkillSource) -> bool:
    return (source.repo_url or "").rstrip("/") == OPENSPEC_REPO and source.source_type == "git-generated"


def _as_optional_str(value: object) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    return None


def _read_update_policy(metadata: dict, metadata_path: Path) -> Optional[str]:
    value = metadata.get("updatePolicy")
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentSkillUpdaterError(f"updatePolicy must be a string in {metadata_path}.")
    if value != LOCAL_ONLY_UPDATE_POLICY:
        raise AgentSkillUpdaterError(f"Unsupported updatePolicy in {metadata_path}: {value}")
    return value


def _metadata_optional_string(metadata: dict, key: str, metadata_path: Path) -> Optional[str]:
    value = metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise AgentSkillUpdaterError(
            f"{key} must be a non-empty string in {metadata_path}."
        )
    return value


def _read_source_metadata(
    source: AgentSkillSource,
) -> tuple[Optional[dict], Optional[bytes], Optional[str]]:
    if source.metadata_path is None:
        if source.update_policy is not None:
            raise AgentSkillUpdaterError(
                f"Skill '{source.name}' declares updatePolicy without a metadata file."
            )
        return None, None, None

    expected_path = source.local_dir / ".openskills.json"
    if not _same_path(source.metadata_path, expected_path):
        raise AgentSkillUpdaterError(
            f"Skill '{source.name}' must use metadata at {expected_path}."
        )
    if (
        not os.path.lexists(source.metadata_path)
        or _is_filesystem_link(source.metadata_path)
        or not source.metadata_path.is_file()
    ):
        raise AgentSkillUpdaterError(
            f"Skill metadata must remain a regular file: {source.metadata_path}"
        )

    content = source.metadata_path.read_bytes()
    metadata = _decode_json_object(content, source.metadata_path, "Skill metadata")
    metadata_policy = _read_update_policy(metadata, source.metadata_path)
    if metadata_policy != source.update_policy:
        raise AgentSkillUpdaterError(
            f"updatePolicy changed for '{source.name}'; reload its metadata before updating."
        )
    return metadata, content, metadata_policy


def _validate_remote_source_fields(source: AgentSkillSource) -> None:
    required = {
        "source": source.source,
        "sourceType": source.source_type,
        "repoUrl": source.repo_url,
        "subpath": source.subpath,
    }
    missing = [key for key, value in required.items() if not isinstance(value, str) or not value]
    if missing:
        raise AgentSkillUpdaterError(
            f"Skill '{source.name}' is missing remote source fields: {', '.join(missing)}."
        )

    normalized_subpath = normalize_skill_subpath(source.subpath)
    if normalized_subpath != source.subpath:
        raise AgentSkillUpdaterError(
            f"Skill '{source.name}' has a non-canonical subpath: {source.subpath}"
        )
    sanitized_repo = sanitize_repo_url(source.repo_url)
    if sanitized_repo.startswith("https://github.com/"):
        owner, repo = _parse_github_repo(sanitized_repo)
        if source.source.casefold() != f"{owner}/{repo}".casefold():
            raise AgentSkillUpdaterError(
                f"Skill '{source.name}' source does not match repoUrl: "
                f"{source.source} != {owner}/{repo}."
            )
    elif not is_git_worktree_skill(source.local_dir):
        raise AgentSkillUpdaterError(
            f"Snapshot skill '{source.name}' requires a GitHub repoUrl."
        )

    if source.entry_type == "skill-pack":
        if source.source_type != "git-pack" or source.subpath != ".":
            raise AgentSkillUpdaterError(
                f"Remotely managed skill pack '{source.name}' requires "
                "sourceType 'git-pack' and subpath '.'."
            )
        if source.generator is not None or source.workflow_id is not None:
            raise AgentSkillUpdaterError(
                f"Skill pack '{source.name}' cannot declare generator metadata."
            )
        return

    if source.source_type == "git":
        if source.generator is not None or source.workflow_id is not None:
            raise AgentSkillUpdaterError(
                f"Git skill '{source.name}' cannot declare generator metadata."
            )
        return

    if source.source_type != "git-generated" or not _is_openspec_source(source):
        raise AgentSkillUpdaterError(
            f"Skill '{source.name}' has unsupported remote sourceType: {source.source_type}."
        )
    if source.generator != "dist/core/shared/skill-generation.js":
        raise AgentSkillUpdaterError(
            f"Generated skill '{source.name}' has unsupported generator metadata."
        )
    if source.subpath != ".":
        raise AgentSkillUpdaterError(
            f"Generated skill '{source.name}' requires subpath '.'."
        )
    if not source.workflow_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", source.workflow_id):
        raise AgentSkillUpdaterError(
            f"Generated skill '{source.name}' requires a safe workflowId metadata value."
        )


def _read_source_contract(
    source: AgentSkillSource,
    expected_installed_base: Optional[str] = None,
) -> tuple[Optional[dict], Optional[bytes], Optional[str]]:
    metadata, content, metadata_policy = _read_source_metadata(source)
    if metadata_policy == LOCAL_ONLY_UPDATE_POLICY:
        return metadata, content, metadata_policy

    _validate_remote_source_fields(source)
    if metadata is None:
        if os.path.lexists(source.local_dir):
            raise AgentSkillUpdaterError(
                f"Remotely managed skill '{source.name}' requires .openskills.json metadata."
            )
        if expected_installed_base is not None:
            raise AgentSkillUpdaterError(
                f"Skill '{source.name}' has no metadata file for installed-base validation."
            )
        return None, None, None

    if "sourceCommitSha" in metadata:
        raise AgentSkillUpdaterError(
            f"Legacy sourceCommitSha is unsupported in {source.metadata_path}."
        )

    current_contract = {
        key: _metadata_optional_string(metadata, key, source.metadata_path)
        for key in SOURCE_PROVENANCE_FIELDS
    }
    if current_contract["repoUrl"] is not None:
        current_contract["repoUrl"] = sanitize_repo_url(current_contract["repoUrl"])
    expected_contract = {
        "source": source.source,
        "sourceType": source.source_type,
        "repoUrl": sanitize_repo_url(source.repo_url) if source.repo_url else None,
        "subpath": source.subpath,
        "generator": source.generator,
        "workflowId": source.workflow_id,
    }
    changed_fields = [
        key for key in SOURCE_PROVENANCE_FIELDS if current_contract[key] != expected_contract[key]
    ]
    if changed_fields:
        raise AgentSkillUpdaterError(
            f"Source contract changed for '{source.name}': {', '.join(changed_fields)}. "
            "Reload registry metadata before remote access or mutation."
        )

    current_entry_type = detect_skill_entry_type(source.local_dir)
    if current_entry_type != source.entry_type:
        raise AgentSkillUpdaterError(
            f"Skill entry type changed for '{source.name}': "
            f"{source.entry_type} -> {current_entry_type}."
        )

    if _is_openspec_source(source):
        if expected_installed_base is not None:
            current_base = _read_generated_by_version(source.local_dir / "SKILL.md")
            if current_base != expected_installed_base:
                raise AgentSkillUpdaterError(
                    f"Generated base version changed for '{source.name}'; refusing stale update data."
                )
        return metadata, content, metadata_policy

    current_base = normalize_git_commit(
        _metadata_optional_string(metadata, "installedBaseVersion", source.metadata_path)
    )
    if current_base is None:
        raise AgentSkillUpdaterError(
            f"installedBaseVersion is required in {source.metadata_path}."
        )
    if expected_installed_base is not None:
        expected_base = normalize_git_commit(expected_installed_base)
        if expected_base is None or not same_git_commit(current_base, expected_base):
            raise AgentSkillUpdaterError(
                f"installedBaseVersion changed for '{source.name}'; refusing stale update data."
            )

    return metadata, content, metadata_policy


def _source_update_policy(source: AgentSkillSource) -> Optional[str]:
    _, _, update_policy = _read_source_metadata(source)
    return update_policy


def _is_local_only_source(source: AgentSkillSource) -> bool:
    return _source_update_policy(source) == LOCAL_ONLY_UPDATE_POLICY


def _require_remote_updates_enabled(
    source: AgentSkillSource,
    expected_installed_base: Optional[str] = None,
) -> tuple[Optional[dict], Optional[bytes]]:
    metadata, content, update_policy = _read_source_contract(
        source,
        expected_installed_base,
    )
    if update_policy == LOCAL_ONLY_UPDATE_POLICY:
        raise AgentSkillUpdaterError(
            f"Skill '{source.name}' is local-only; remote probing and updates are disabled."
        )
    return metadata, content


def _require_remote_probe_ready(source: AgentSkillSource) -> None:
    _require_remote_updates_enabled(source)
    prefixes = (
        f".{source.local_dir.name}.update-",
        f".{source.local_dir.name}.git-update-",
        f".{source.local_dir.name}.metadata-update-",
    )
    for path in source.local_dir.parent.iterdir():
        if path.name.startswith(prefixes) and _looks_like_transaction_directory(path):
            raise AgentSkillUpdaterError(
                f"Skill '{source.name}' has a pending update transaction at {path}; "
                "recover it before remote access."
            )
