"""Capture transport and current-head delivery; TypeScript owns the batch."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...history import load_registry
from ...state_refresh import resolve_goal_state
from ..coordination.authority_source_capture import authority_registry_source
from ..coordination.local_authority import (
    LOCAL_AUTHORITY_SOURCES,
    LocalCoordinationAuthorityUnavailable,
    local_authority_is_promoted,
)
from ..effect_runtime import effect_runtime_result
from .provider_projection import settle_canonical_todo_projection


def capture_canonical_followups_if_promoted(
    *, registry_path: Path, runtime_root: Path, goal_id: str,
    operation_id: str, intent: dict[str, Any], dry_run: bool,
    project: Path | None, state_file: Path | None,
) -> dict[str, Any] | None:
    if not local_authority_is_promoted(runtime_root=runtime_root, goal_id=goal_id):
        return None
    with authority_registry_source(registry_path) as source:
        registry_source = dict(source)
        goal, resolved_project, resolved_state = resolve_goal_state(
            registry=load_registry(registry_path), goal_id=goal_id,
            project_override=project, state_file_override=state_file,
        )
        if goal is None:
            raise ValueError(f"goal {goal_id!r} is not present in the registry")
    result = effect_runtime_result("coordination.local_authority.followup_capture", {
        "schema_version": "loopx_coordination_followup_capture_request_v0",
        "runtime_root": str(runtime_root.resolve()), "goal_id": goal_id,
        "operation_id": operation_id, "intent": intent, "dry_run": dry_run,
        "registry_source": registry_source,
    })
    if (not isinstance(result, dict)
        or result.get("status") not in {"applied", "recovered", "replayed", "no_change", "planned"}
        or result.get("source_authority") not in LOCAL_AUTHORITY_SOURCES
        or result.get("decision_read_from_provider") is not True
        or result.get("legacy_fallback_used") is not False
        or not isinstance(result.get("items"), list)):
        payload = result if isinstance(result, dict) else {}
        raise LocalCoordinationAuthorityUnavailable(
            str(payload.get("reason") or "canonical follow-up capture unavailable"),
            code=str(payload.get("reason_code") or "followup_capture_unavailable"),
            payload={**payload, "capture_operation_id": operation_id},
        )
    payload = {**result, "ok": True, "dry_run": dry_run,
               "capture_operation_id": operation_id,
               "state_file": str(resolved_state),
               "project": str(resolved_project) if resolved_project else None,
               "idempotent_replay": result["status"] == "replayed"}
    return settle_canonical_todo_projection(
        payload, registry_path=registry_path, runtime_root=runtime_root, goal_id=goal_id,
        project=project, state_file=state_file,
    )
