#!/usr/bin/env python3
"""Inventory and explicitly manage durable Intervention Records."""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__:
    from .interventions import (
        InterventionError,
        cleanup_intervention,
        get_interventions_dir,
        inventory_interventions,
        mark_content_conflict,
        validate_recovery_required,
    )
    from .stdio_utils import JsonArgumentParser, configure_windows_utf8_stdio
else:
    sys.path.insert(0, str(Path(__file__).parent))
    from interventions import (  # noqa: E402
        InterventionError,
        cleanup_intervention,
        get_interventions_dir,
        inventory_interventions,
        mark_content_conflict,
        validate_recovery_required,
    )
    from stdio_utils import JsonArgumentParser, configure_windows_utf8_stdio  # noqa: E402


configure_windows_utf8_stdio()


def main() -> None:
    parser = JsonArgumentParser(
        description="Inventory and manage ~/.agents/interventions",
        json_error_factory=lambda message: {
            "status": "error",
            "operation": _requested_operation(),
            "error_message": message,
        },
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--resolve", metavar="ARTIFACT_ID")
    actions.add_argument("--abandon", metavar="ARTIFACT_ID")
    actions.add_argument("--validate", metavar="ARTIFACT_ID")
    actions.add_argument("--cleanup", metavar="ARTIFACT_ID")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    root = get_interventions_dir()
    if args.resolve is not None:
        operation = "resolve"
        payload = {
            "status": "ok",
            "operation": operation,
            "record": mark_content_conflict(root, args.resolve, "resolved"),
        }
    elif args.abandon is not None:
        operation = "abandon"
        payload = {
            "status": "ok",
            "operation": operation,
            "record": mark_content_conflict(root, args.abandon, "abandoned"),
        }
    elif args.validate is not None:
        operation = "validate"
        payload = {
            "status": "ok",
            "operation": operation,
            "record": validate_recovery_required(
                root,
                args.validate,
                _recover_diagnostic_journal,
            ),
        }
    elif args.cleanup is not None:
        operation = "cleanup"
        result = cleanup_intervention(root, args.cleanup)
        payload = {"operation": operation, **result}
    else:
        operation = "inventory"
        payload = {
            "status": "ok",
            "operation": operation,
            "records": inventory_interventions(root),
        }

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_human(payload)
    if payload["status"] == "error":
        raise SystemExit(1)


def _recover_diagnostic_journal(journal: Path) -> str:
    if __package__:
        from .agent_skill_updater import AgentSkillUpdaterError, validate_diagnostic_journal
    else:
        from agent_skill_updater import AgentSkillUpdaterError, validate_diagnostic_journal

    try:
        return validate_diagnostic_journal(journal)
    except AgentSkillUpdaterError as exc:
        raise InterventionError(str(exc)) from None


def _requested_operation() -> str:
    for option, operation in (
        ("--resolve", "resolve"),
        ("--abandon", "abandon"),
        ("--validate", "validate"),
        ("--cleanup", "cleanup"),
    ):
        if option in sys.argv[1:]:
            return operation
    return "inventory"


def _print_human(payload: dict[str, object]) -> None:
    operation = payload["operation"]
    if operation == "inventory":
        records = payload["records"]
        print(f"Intervention Records: {len(records)}")
        for record in records:
            print(
                f"- {record['artifact_id']}: {record['record_type']} "
                f"({record['resolution_state'] or record['recovery_state']})"
            )
        return
    if payload["status"] == "error":
        print(f"Error: {payload['error_message']}", file=sys.stderr)
        return
    print(f"Intervention {operation} completed: {payload.get('artifact_id', '')}")


def _run_cli() -> None:
    try:
        main()
    except (InterventionError, OSError, ValueError) as exc:
        payload = {
            "status": "error",
            "operation": _requested_operation(),
            "error_message": str(exc),
        }
        if "--json" in sys.argv[1:]:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    _run_cli()
