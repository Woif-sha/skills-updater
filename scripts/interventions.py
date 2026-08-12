#!/usr/bin/env python3
"""Inventory, retain, validate, and clean durable Intervention Records."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Optional


INTERVENTION_SCHEMA_VERSION = 1
INTERVENTION_RECORD_TYPES = frozenset({"content-conflict", "recovery-required"})
CONTENT_RESOLUTION_STATES = frozenset({"unresolved", "resolved", "abandoned"})
RECOVERY_STATES = frozenset({"required", "committed", "rolled_back"})
ARTIFACT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
RECORD_TYPE_ROLES = {
    "content-conflict": ("intervention-record",),
    "recovery-required": ("intervention-record", "diagnostic-journal"),
}
TOMBSTONE_MEMBER_STATES = frozenset(
    {"pending", "moving", "tombstoned", "deleting", "deleted"}
)


class InterventionError(ValueError):
    """Raised when an Intervention Record violates the durable contract."""


def get_interventions_dir() -> Path:
    return Path.home() / ".agents" / "interventions"


def inventory_interventions(
    interventions_root: Path,
    *,
    now: Optional[datetime] = None,
) -> list[dict[str, object]]:
    """Return the stable, read-only inventory of Intervention Records."""

    if not os.path.lexists(interventions_root):
        return []
    _require_regular_directory(interventions_root, "Intervention root")
    observed_at = _utc_datetime(now or datetime.now(timezone.utc), "inventory time")
    inventory: list[dict[str, object]] = []
    for record_path in sorted(interventions_root.iterdir(), key=lambda path: path.name):
        if record_path.name.endswith(".complete.json"):
            tombstone = Path(str(record_path)[: -len(".complete.json")])
            if not os.path.lexists(tombstone):
                inventory.append(_inventory_tombstone(tombstone))
            continue
        if record_path.name.startswith(".tombstone-"):
            inventory.append(_inventory_tombstone(record_path))
            continue
        if record_path.name.startswith("."):
            continue
        if not record_path.is_dir() or record_path.is_symlink():
            raise InterventionError(
                f"Intervention root contains an unsupported entry: {record_path}"
            )
        manifest = _read_manifest(record_path)
        inventory.append(_inventory_item(manifest, observed_at))
    return inventory


def is_diagnostic_journal_retained(
    interventions_root: Path,
    diagnostic_journal: Path,
) -> bool:
    """Return whether a recovery record currently owns the journal retention."""

    if not os.path.lexists(interventions_root):
        return False
    _require_regular_directory(interventions_root, "Intervention root")
    journal = diagnostic_journal.resolve()
    artifact_id = _recovery_artifact_id(journal)
    record_path = interventions_root / artifact_id
    if os.path.lexists(record_path):
        _require_regular_directory(record_path, "Intervention record")
        manifest = _read_manifest(record_path)
        return (
            manifest["recordType"] == "recovery-required"
            and manifest["diagnosticReferences"] == [str(journal)]
        )
    tombstone = interventions_root / f".tombstone-{artifact_id}"
    if not os.path.lexists(tombstone):
        return False
    _require_regular_directory(tombstone, "Intervention tombstone")
    intent = _read_tombstone_intent(tombstone)
    _validate_tombstone_members(tombstone, intent)
    members = intent["members"]
    journal_member = members[1] if len(members) == 2 else None
    return (
        intent.get("recordType") == "recovery-required"
        and journal_member is not None
        and journal_member["source"] == str(journal)
        and journal_member["state"] != "deleted"
    )


def mark_content_conflict(
    interventions_root: Path,
    artifact_id: str,
    resolution: str,
    *,
    now: Optional[datetime] = None,
) -> dict[str, object]:
    """Mark a content conflict resolved or abandoned and start retention."""

    _validate_artifact_id(artifact_id)
    if resolution not in {"resolved", "abandoned"}:
        raise InterventionError("Content conflicts may only be marked resolved or abandoned.")
    _require_regular_directory(interventions_root, "Intervention root")
    record_path = interventions_root / artifact_id
    _require_regular_directory(record_path, "Intervention record")
    manifest = _read_manifest(record_path)
    if manifest["recordType"] != "content-conflict":
        raise InterventionError(f"Intervention Record is not a content conflict: {artifact_id}")
    current = manifest["resolutionState"]
    if current != "unresolved" and current != resolution:
        raise InterventionError(
            f"Content conflict '{artifact_id}' is already marked {current}."
        )
    observed_at = _utc_datetime(now or datetime.now(timezone.utc), "resolution time")
    if current == "unresolved":
        manifest["resolutionState"] = resolution
        manifest["retentionStartedAt"] = _format_utc_timestamp(observed_at)
        manifest["retentionExpiresAt"] = _format_utc_timestamp(
            observed_at + timedelta(days=15)
        )
        _write_json_atomic(record_path / "manifest.json", manifest)
    return _inventory_item(manifest, observed_at)


def publish_recovery_required(
    interventions_root: Path,
    skill_name: str,
    diagnostic_journal: Path,
    *,
    now: Optional[datetime] = None,
) -> Path:
    """Publish a minimal recovery record that only references its journal."""

    _require_regular_directory(diagnostic_journal, "Diagnostic Journal")
    journal_path = diagnostic_journal.resolve()
    artifact_id = _recovery_artifact_id(journal_path)
    observed_at = _utc_datetime(now or datetime.now(timezone.utc), "creation time")
    manifest = {
        "schemaVersion": INTERVENTION_SCHEMA_VERSION,
        "artifactId": artifact_id,
        "recordType": "recovery-required",
        "skillName": skill_name,
        "createdAt": _format_utc_timestamp(observed_at),
        "resolutionState": None,
        "recoveryState": "required",
        "retentionStartedAt": None,
        "retentionExpiresAt": None,
        "retentionGroup": [
            {"role": "intervention-record", "path": artifact_id},
            {"role": "diagnostic-journal", "path": str(journal_path)},
        ],
        "diagnosticReferences": [str(journal_path)],
    }
    _ensure_interventions_root(interventions_root)
    destination = interventions_root / artifact_id
    if os.path.lexists(destination):
        _require_regular_directory(destination, "Intervention record")
        existing = _read_manifest(destination)
        if existing["diagnosticReferences"] != manifest["diagnosticReferences"]:
            raise InterventionError(
                f"Recovery artifact identity collision at {destination}"
            )
        return destination
    draft = Path(tempfile.mkdtemp(prefix=".draft-", dir=interventions_root))
    try:
        _write_json_atomic(draft / "manifest.json", manifest)
        os.rename(draft, destination)
    except BaseException:
        if draft.exists():
            shutil.rmtree(draft)
        raise
    return destination


def _recovery_artifact_id(journal_path: Path) -> str:
    artifact_hash = hashlib.sha256(str(journal_path).encode("utf-8")).hexdigest()[:24]
    return f"recovery-required-{artifact_hash}"


def validate_recovery_required(
    interventions_root: Path,
    artifact_id: str,
    validator: Callable[[Path], str],
    *,
    now: Optional[datetime] = None,
) -> dict[str, object]:
    """Run the selected journal validator and start retention after proof."""

    _validate_artifact_id(artifact_id)
    _require_regular_directory(interventions_root, "Intervention root")
    record_path = interventions_root / artifact_id
    _require_regular_directory(record_path, "Intervention record")
    manifest = _read_manifest(record_path)
    if manifest["recordType"] != "recovery-required":
        raise InterventionError(f"Intervention Record is not recovery-required: {artifact_id}")
    observed_at = _utc_datetime(now or datetime.now(timezone.utc), "validation time")
    if manifest["recoveryState"] in {"committed", "rolled_back"}:
        return _inventory_item(manifest, observed_at)
    references = manifest["diagnosticReferences"]
    if len(references) != 1:
        raise InterventionError(
            f"Recovery Intervention must reference exactly one Diagnostic Journal: {artifact_id}"
        )
    journal = Path(references[0])
    _require_regular_directory(journal, "Diagnostic Journal")
    installed_state = validator(journal.resolve())
    if installed_state not in {"committed", "rolled_back"}:
        raise InterventionError(
            f"Diagnostic Journal '{journal}' did not prove committed or rolled_back state."
        )
    manifest["recoveryState"] = installed_state
    manifest["validatedJournalEvidence"] = _diagnostic_journal_evidence(journal)
    manifest["retentionStartedAt"] = _format_utc_timestamp(observed_at)
    manifest["retentionExpiresAt"] = _format_utc_timestamp(
        observed_at + timedelta(days=15)
    )
    _write_json_atomic(record_path / "manifest.json", manifest)
    return _inventory_item(manifest, observed_at)


def cleanup_intervention(
    interventions_root: Path,
    artifact_id: str,
    *,
    now: Optional[datetime] = None,
) -> dict[str, object]:
    """Clean one expired retention group through a recoverable tombstone."""

    _validate_artifact_id(artifact_id)
    observed_at = _utc_datetime(now or datetime.now(timezone.utc), "cleanup time")
    if not os.path.lexists(interventions_root):
        return _cleanup_result(artifact_id, status="already_cleaned", cleaned=True)
    _require_regular_directory(interventions_root, "Intervention root")
    tombstone = interventions_root / f".tombstone-{artifact_id}"
    if os.path.lexists(tombstone) or os.path.lexists(_tombstone_completion(tombstone)):
        return _resume_tombstone(tombstone)
    record_path = interventions_root / artifact_id
    if not os.path.lexists(record_path):
        return _cleanup_result(artifact_id, status="already_cleaned", cleaned=True)
    _require_regular_directory(record_path, "Intervention record")
    manifest = _read_manifest(record_path)
    item = _inventory_item(manifest, observed_at)
    if not item["cleanup_eligible"]:
        raise InterventionError(
            f"Intervention Record '{artifact_id}' is not eligible for cleanup."
        )
    members = _retention_members(interventions_root, manifest, tombstone)
    installed_state = _installed_state(manifest)
    draft = Path(tempfile.mkdtemp(prefix=".tombstone-draft-", dir=interventions_root))
    intent = {
        "schemaVersion": INTERVENTION_SCHEMA_VERSION,
        "artifactId": artifact_id,
        "recordType": manifest["recordType"],
        "installedState": installed_state,
        "inventory": item,
        "members": members,
    }
    try:
        _write_json_atomic(draft / "tombstone.json", intent)
        os.rename(draft, tombstone)
    except BaseException:
        if draft.exists():
            shutil.rmtree(draft)
        raise
    return _resume_tombstone(tombstone)


def _resume_tombstone(tombstone: Path) -> dict[str, object]:
    if os.path.lexists(tombstone):
        _require_regular_directory(tombstone, "Intervention tombstone")
    intent = _read_tombstone_intent(tombstone)
    artifact_id, installed_state = _validate_tombstone_envelope(tombstone, intent)
    members = intent.get("members")
    if not isinstance(members, list) or not members:
        raise InterventionError(f"Intervention tombstone has no retention group: {tombstone}")
    try:
        _validate_tombstone_members(tombstone, intent)
        for index in range(len(members)):
            _resume_tombstone_member(tombstone, intent, index)
        _remove_tombstone(tombstone)
    except (InterventionError, OSError) as exc:
        return _cleanup_result(
            artifact_id,
            status="error",
            cleaned=False,
            installed_state=installed_state,
            error_message=f"Intervention cleanup failed at {tombstone}: {exc}",
            cleanup_residue=str(_tombstone_residue(tombstone)),
        )
    return _cleanup_result(
        artifact_id,
        status="cleaned",
        cleaned=True,
        installed_state=installed_state,
    )


def _inventory_tombstone(tombstone: Path) -> dict[str, object]:
    intent = _read_tombstone_intent(tombstone)
    _validate_tombstone_envelope(tombstone, intent)
    _validate_tombstone_members(tombstone, intent)
    item = intent.get("inventory")
    required = {
        "artifact_id",
        "record_type",
        "skill_name",
        "resolution_state",
        "recovery_state",
        "retention_started_at",
        "retention_expires_at",
        "retention_group",
        "diagnostic_references",
        "cleanup_eligible",
    }
    if not isinstance(item, dict) or not required.issubset(item):
        raise InterventionError(f"Intervention tombstone inventory is invalid: {tombstone}")
    return {
        **item,
        "cleanup_status": "residue",
        "cleanup_residue": str(_tombstone_residue(tombstone)),
    }


def _read_tombstone_intent(tombstone: Path) -> dict:
    intent_path = tombstone / "tombstone.json"
    if not os.path.lexists(intent_path):
        intent_path = _tombstone_completion(tombstone)
    if intent_path.is_symlink() or not intent_path.is_file():
        raise InterventionError(f"Intervention tombstone intent is missing: {intent_path}")
    try:
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InterventionError(f"Invalid Intervention tombstone {intent_path}: {exc}") from exc
    if not isinstance(intent, dict):
        raise InterventionError(f"Intervention tombstone intent must be an object: {intent_path}")
    return intent


def _validate_tombstone_envelope(
    tombstone: Path,
    intent: dict,
) -> tuple[str, str]:
    artifact_id = _validate_artifact_id(intent.get("artifactId"))
    expected_tombstone = tombstone.parent / f".tombstone-{artifact_id}"
    if (
        tombstone != expected_tombstone
        or intent.get("schemaVersion") != INTERVENTION_SCHEMA_VERSION
    ):
        raise InterventionError(f"Intervention tombstone identity mismatch: {tombstone}")
    installed_state = intent.get("installedState")
    if (
        not isinstance(installed_state, str)
        or installed_state not in {"unchanged", "committed", "rolled_back"}
    ):
        raise InterventionError(f"Invalid Installed State in tombstone: {tombstone}")
    return artifact_id, installed_state


def _validate_tombstone_members(tombstone: Path, intent: dict) -> None:
    artifact_id = _validate_artifact_id(intent.get("artifactId"))
    record_type = intent.get("recordType")
    if not isinstance(record_type, str) or record_type not in INTERVENTION_RECORD_TYPES:
        raise InterventionError(f"Invalid record type in tombstone: {tombstone}")
    members = intent.get("members")
    expected_roles = list(RECORD_TYPE_ROLES[record_type])
    if (
        not isinstance(members, list)
        or any(not isinstance(member, dict) for member in members)
        or [member.get("role") for member in members] != expected_roles
    ):
        raise InterventionError(f"Incomplete retention group in tombstone: {tombstone}")
    record_source = tombstone.parent / artifact_id
    for member in members:
        if not isinstance(member, dict) or set(member) != {
            "role",
            "source",
            "destination",
            "required",
            "state",
            "evidence",
        }:
            raise InterventionError(f"Invalid retention member in tombstone: {tombstone}")
        role = member["role"]
        if (
            not isinstance(role, str)
            or not isinstance(member["source"], str)
            or not isinstance(member["destination"], str)
            or not isinstance(member["required"], bool)
        ):
            raise InterventionError(f"Invalid retention member types in tombstone: {tombstone}")
        source = Path(member["source"])
        state = member["state"]
        if not isinstance(state, str) or state not in TOMBSTONE_MEMBER_STATES:
            raise InterventionError(f"Invalid retention state in tombstone: {tombstone}")
        if Path(member["destination"]) != tombstone / role:
            raise InterventionError(f"Invalid retention destination in tombstone: {tombstone}")
        if role == "intervention-record":
            if source != record_source or member["required"] is not True:
                raise InterventionError(f"Invalid record source in tombstone: {tombstone}")
        else:
            _validate_diagnostic_journal_path(tombstone.parent, source)
            if member["required"] is not True:
                raise InterventionError(f"Invalid journal member in tombstone: {tombstone}")
        _validate_tombstone_member_state(tombstone, member)
    item = intent.get("inventory")
    if not isinstance(item, dict) or item.get("artifact_id") != artifact_id:
        raise InterventionError(f"Invalid inventory identity in tombstone: {tombstone}")
    expected_group = [
        {"role": "intervention-record", "path": artifact_id},
    ]
    if record_type == "recovery-required":
        journal_path = members[1]["source"]
        expected_group.append({"role": "diagnostic-journal", "path": journal_path})
        if item.get("diagnostic_references") != [journal_path]:
            raise InterventionError(f"Invalid journal reference in tombstone: {tombstone}")
    if item.get("retention_group") != expected_group:
        raise InterventionError(f"Invalid retention inventory in tombstone: {tombstone}")


def _resume_tombstone_member(tombstone: Path, intent: dict, index: int) -> None:
    member = intent["members"][index]
    source = Path(member["source"])
    destination = Path(member["destination"])
    if member["state"] in {"tombstoned", "deleting", "deleted"}:
        return
    if member["state"] == "pending":
        member["state"] = "moving"
        _write_tombstone_intent(tombstone, intent)
    if os.path.lexists(source):
        os.rename(source, destination)
    member["state"] = "tombstoned"
    _write_tombstone_intent(tombstone, intent)


def _validate_tombstone_member_state(tombstone: Path, member: dict) -> None:
    source = Path(member["source"])
    destination = Path(member["destination"])
    source_exists = os.path.lexists(source)
    destination_exists = os.path.lexists(destination)
    state = member["state"]
    if source_exists:
        _require_regular_directory(source, "Retention member")
    if destination_exists:
        _require_regular_directory(destination, "Tombstoned retention member")
    if state == "pending" and (not source_exists or destination_exists):
        raise InterventionError(f"Pending retention member is incomplete: {source}")
    if state == "moving" and source_exists == destination_exists:
        raise InterventionError(f"Moving retention member is ambiguous: {source}")
    if state == "tombstoned" and (source_exists or not destination_exists):
        raise InterventionError(f"Tombstoned retention member is incomplete: {destination}")
    if state in {"deleting", "deleted"} and source_exists:
        raise InterventionError(f"Deleted retention source reappeared: {source}")
    if state == "deleted" and destination_exists:
        raise InterventionError(f"Deleted retention member reappeared: {destination}")
    if state in {"pending", "moving", "tombstoned"}:
        location = source if source_exists else destination
        _validate_member_evidence(location, member["role"], member["evidence"])


def _validate_member_evidence(location: Path, role: str, evidence: object) -> None:
    if not isinstance(evidence, dict):
        raise InterventionError(f"Invalid retention evidence for {role}: {location}")
    if role == "intervention-record":
        if set(evidence) != {"manifestSha256"}:
            raise InterventionError(f"Invalid record evidence at {location}")
        observed = _sha256_file(location / "manifest.json")
        if observed != evidence["manifestSha256"]:
            raise InterventionError(f"Intervention manifest identity changed at {location}")
        return
    if set(evidence) != {"treeSha256"}:
        raise InterventionError(f"Invalid Diagnostic Journal evidence at {location}")
    observed = _diagnostic_journal_evidence(location)
    if observed != evidence:
        raise InterventionError(f"Diagnostic Journal identity changed at {location}")


def _diagnostic_journal_evidence(journal: Path) -> dict[str, str]:
    _require_regular_directory(journal, "Diagnostic Journal")
    digest = hashlib.sha256()
    for path in sorted(journal.rglob("*"), key=lambda item: item.relative_to(journal).as_posix()):
        if path.is_symlink():
            raise InterventionError(f"Diagnostic Journal contains an unsafe link: {path}")
        relative = path.relative_to(journal).as_posix()
        if path.is_dir():
            record = f"directory\0{relative}".encode("utf-8")
        elif path.is_file():
            record = f"file\0{relative}\0".encode("utf-8") + path.read_bytes()
        else:
            raise InterventionError(f"Diagnostic Journal contains an unsupported entry: {path}")
        digest.update(len(record).to_bytes(8, "big"))
        digest.update(record)
    return {"treeSha256": digest.hexdigest()}


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise InterventionError(f"Required retention evidence is missing: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_tombstone_intent(tombstone: Path, intent: dict) -> None:
    _write_json_atomic(tombstone / "tombstone.json", intent)


def _retention_members(
    interventions_root: Path,
    manifest: dict,
    tombstone: Path,
) -> list[dict[str, object]]:
    artifact_id = manifest["artifactId"]
    group = manifest["retentionGroup"]
    roles = [member["role"] for member in group]
    expected_roles = list(RECORD_TYPE_ROLES[manifest["recordType"]])
    if roles != expected_roles:
        raise InterventionError(
            f"Intervention retention group is incomplete for '{artifact_id}'."
        )
    if group[0]["path"] != artifact_id:
        raise InterventionError(f"Intervention record member mismatch for '{artifact_id}'.")
    record = interventions_root / artifact_id
    members: list[dict[str, object]] = [
        {
            "role": "intervention-record",
            "source": str(record),
            "destination": str(tombstone / "intervention-record"),
            "required": True,
            "state": "pending",
            "evidence": {
                "manifestSha256": _sha256_file(record / "manifest.json"),
            },
        }
    ]
    if manifest["recordType"] == "recovery-required":
        journal = Path(group[1]["path"])
        _validate_diagnostic_journal_path(interventions_root, journal)
        if manifest["diagnosticReferences"] != [str(journal)]:
            raise InterventionError(
                f"Diagnostic Journal reference mismatch for '{artifact_id}'."
            )
        evidence = _diagnostic_journal_evidence(journal)
        if evidence != manifest.get("validatedJournalEvidence"):
            raise InterventionError(
                f"Diagnostic Journal identity changed after validation for '{artifact_id}'."
            )
        members.append(
            {
                "role": "diagnostic-journal",
                "source": str(journal),
                "destination": str(tombstone / "diagnostic-journal"),
                "required": True,
                "state": "pending",
                "evidence": evidence,
            }
        )
    return members


def _validate_diagnostic_journal_path(interventions_root: Path, journal: Path) -> None:
    if not journal.is_absolute():
        raise InterventionError("Diagnostic Journal references must be absolute paths.")
    skills_root = (interventions_root.parent / "skills").resolve()
    resolved = journal.resolve(strict=False)
    if resolved.parent != skills_root or not any(
        marker in resolved.name
        for marker in (".transaction-", ".metadata-update-", ".git-update-", ".update-")
    ):
        raise InterventionError(f"Diagnostic Journal is outside the managed Skill root: {journal}")


def _installed_state(manifest: dict) -> str:
    recovery_state = manifest["recoveryState"]
    return recovery_state if recovery_state in {"committed", "rolled_back"} else "unchanged"


def _cleanup_result(
    artifact_id: str,
    *,
    status: str,
    cleaned: bool,
    installed_state: str = "unchanged",
    error_message: Optional[str] = None,
    cleanup_residue: Optional[str] = None,
) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "status": status,
        "cleaned": cleaned,
        "installed_state": installed_state,
        "error_message": error_message,
        "cleanup_residue": cleanup_residue,
    }


def _remove_tombstone(tombstone: Path) -> None:
    intent = _read_tombstone_intent(tombstone)
    _validate_tombstone_members(tombstone, intent)
    for member in intent["members"]:
        destination = Path(member["destination"])
        if member["state"] == "tombstoned":
            member["state"] = "deleting"
            _write_tombstone_intent(tombstone, intent)
        if member["state"] == "deleting":
            if os.path.lexists(destination):
                shutil.rmtree(destination)
            member["state"] = "deleted"
            _write_tombstone_intent(tombstone, intent)
    _validate_tombstone_members(tombstone, intent)
    intent_path = tombstone / "tombstone.json"
    completion = _tombstone_completion(tombstone)
    if os.path.lexists(intent_path):
        os.rename(intent_path, completion)
    if os.path.lexists(tombstone):
        tombstone.rmdir()
    completion.unlink()


def _tombstone_completion(tombstone: Path) -> Path:
    return Path(f"{tombstone}.complete.json")


def _tombstone_residue(tombstone: Path) -> Path:
    return tombstone if os.path.lexists(tombstone) else _tombstone_completion(tombstone)


def _inventory_item(manifest: dict, observed_at: datetime) -> dict[str, object]:
    expires_at = manifest["retentionExpiresAt"]
    return {
        "artifact_id": manifest["artifactId"],
        "record_type": manifest["recordType"],
        "skill_name": manifest["skillName"],
        "resolution_state": manifest["resolutionState"],
        "recovery_state": manifest["recoveryState"],
        "retention_started_at": manifest["retentionStartedAt"],
        "retention_expires_at": expires_at,
        "retention_group": manifest["retentionGroup"],
        "diagnostic_references": manifest["diagnosticReferences"],
        "cleanup_eligible": (
            expires_at is not None
            and observed_at >= _parse_utc_timestamp(expires_at, "retentionExpiresAt")
        ),
    }


def _read_manifest(record_path: Path) -> dict:
    manifest_path = record_path / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise InterventionError(f"Intervention manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InterventionError(f"Invalid Intervention manifest {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise InterventionError(f"Intervention manifest must be an object: {manifest_path}")
    required = {
        "schemaVersion",
        "artifactId",
        "recordType",
        "skillName",
        "createdAt",
        "resolutionState",
        "recoveryState",
        "retentionStartedAt",
        "retentionExpiresAt",
        "retentionGroup",
        "diagnosticReferences",
    }
    if not required.issubset(manifest):
        missing = ", ".join(sorted(required - manifest.keys()))
        raise InterventionError(
            f"Intervention manifest is missing required fields at {manifest_path}: {missing}"
        )
    if manifest["schemaVersion"] != INTERVENTION_SCHEMA_VERSION:
        raise InterventionError(f"Unsupported Intervention schema at {manifest_path}")
    artifact_id = manifest["artifactId"]
    _validate_artifact_id(artifact_id)
    if artifact_id != record_path.name:
        raise InterventionError(f"Intervention artifact identity mismatch at {record_path}")
    if (
        not isinstance(manifest["recordType"], str)
        or manifest["recordType"] not in INTERVENTION_RECORD_TYPES
    ):
        raise InterventionError(f"Unsupported Intervention record type at {manifest_path}")
    if not isinstance(manifest["skillName"], str) or not manifest["skillName"]:
        raise InterventionError(f"Invalid Intervention skill name at {manifest_path}")
    _parse_utc_timestamp(manifest["createdAt"], "createdAt")
    _validate_record_states(manifest, manifest_path)
    _validate_retention(manifest, manifest_path)
    _validate_string_list(manifest["diagnosticReferences"], "diagnosticReferences")
    _validate_manifest_group(record_path, manifest)
    return manifest


def _validate_manifest_group(record_path: Path, manifest: dict) -> None:
    artifact_id = manifest["artifactId"]
    group = manifest["retentionGroup"]
    expected_roles = (
        ["intervention-record"]
        if manifest["recordType"] == "content-conflict"
        else ["intervention-record", "diagnostic-journal"]
    )
    if [member["role"] for member in group] != expected_roles:
        raise InterventionError(f"Incomplete Intervention retention group at {record_path}")
    if group[0]["path"] != artifact_id:
        raise InterventionError(f"Intervention record identity mismatch at {record_path}")
    references = manifest["diagnosticReferences"]
    if manifest["recordType"] == "content-conflict":
        for reference in references:
            posix = PurePosixPath(reference)
            windows = PureWindowsPath(reference)
            if (
                posix.is_absolute()
                or windows.is_absolute()
                or ".." in posix.parts
                or not posix.parts
                or posix.parts[0] != "conflicts"
            ):
                raise InterventionError(
                    f"Invalid content-conflict diagnostic reference at {record_path}"
                )
        return
    journal = Path(group[1]["path"])
    _validate_diagnostic_journal_path(record_path.parent, journal)
    if references != [str(journal)]:
        raise InterventionError(f"Diagnostic Journal reference mismatch at {record_path}")


def _validate_artifact_id(artifact_id: object) -> str:
    if not isinstance(artifact_id, str) or ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
        raise InterventionError(
            "Intervention operations require one stable artifact ID; paths and globs are forbidden."
        )
    return artifact_id


def _validate_record_states(manifest: dict, manifest_path: Path) -> None:
    if manifest["recordType"] == "content-conflict":
        if (
            not isinstance(manifest["resolutionState"], str)
            or manifest["resolutionState"] not in CONTENT_RESOLUTION_STATES
            or manifest["recoveryState"] is not None
        ):
            raise InterventionError(f"Invalid content-conflict state at {manifest_path}")
        if "validatedJournalEvidence" in manifest:
            raise InterventionError(
                f"Content conflicts cannot contain journal proof at {manifest_path}"
            )
        return
    if (
        manifest["resolutionState"] is not None
        or not isinstance(manifest["recoveryState"], str)
        or manifest["recoveryState"] not in RECOVERY_STATES
    ):
        raise InterventionError(f"Invalid recovery-required state at {manifest_path}")
    proof = manifest.get("validatedJournalEvidence")
    if manifest["recoveryState"] == "required":
        if proof is not None:
            raise InterventionError(
                f"Unvalidated recovery record contains proof at {manifest_path}"
            )
    elif (
        not isinstance(proof, dict)
        or set(proof) != {"treeSha256"}
        or any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in proof.values()
        )
    ):
        raise InterventionError(
            f"Settled recovery record is missing journal proof at {manifest_path}"
        )


def _validate_retention(manifest: dict, manifest_path: Path) -> None:
    started_at = manifest["retentionStartedAt"]
    expires_at = manifest["retentionExpiresAt"]
    retention_started = started_at is not None
    resolution_state = manifest["resolutionState"]
    recovery_state = manifest["recoveryState"]
    retention_required = (
        isinstance(resolution_state, str)
        and resolution_state in {"resolved", "abandoned"}
        if manifest["recordType"] == "content-conflict"
        else isinstance(recovery_state, str)
        and recovery_state in {"committed", "rolled_back"}
    )
    if retention_started != retention_required:
        raise InterventionError(
            f"Intervention state and retention window disagree at {manifest_path}"
        )
    if (started_at is None) != (expires_at is None):
        raise InterventionError(f"Incomplete Intervention retention state at {manifest_path}")
    if started_at is not None:
        start = _parse_utc_timestamp(started_at, "retentionStartedAt")
        expiry = _parse_utc_timestamp(expires_at, "retentionExpiresAt")
        created = _parse_utc_timestamp(manifest["createdAt"], "createdAt")
        if start < created or expiry - start != timedelta(days=15):
            raise InterventionError(f"Invalid Intervention retention window at {manifest_path}")
    group = manifest["retentionGroup"]
    if not isinstance(group, list) or not group:
        raise InterventionError(f"Intervention retention group is empty at {manifest_path}")
    for member in group:
        if (
            not isinstance(member, dict)
            or set(member) != {"role", "path"}
            or not isinstance(member["role"], str)
            or not member["role"]
            or not isinstance(member["path"], str)
            or not member["path"]
        ):
            raise InterventionError(f"Invalid Intervention retention group at {manifest_path}")


def _validate_string_list(value: object, field: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise InterventionError(f"Intervention {field} must be a list of strings.")


def _parse_utc_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise InterventionError(f"Intervention {field} must be a UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InterventionError(f"Invalid Intervention {field}: {value}") from exc
    return _utc_datetime(parsed, field)


def _utc_datetime(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InterventionError(f"Intervention {field} must include a timezone.")
    return value.astimezone(timezone.utc)


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _write_json_atomic(path: Path, payload: dict) -> None:
    content = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
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
        temp_path.replace(path)
        temp_path = None
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _require_regular_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise InterventionError(f"{label} must be a regular directory: {path}")


def _ensure_interventions_root(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _require_regular_directory(path, "Intervention root")
