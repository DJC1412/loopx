from __future__ import annotations

from uuid import uuid4
from pathlib import Path
from typing import Any

from .control_plane.coordination.legacy_writer_fence import legacy_todo_write_transaction
from .control_plane.coordination.local_authority_shadow_adapter import effective_runtime_root
from .control_plane.coordination.runtime_shadow_writer_adapter import (
    write_captured_todo_state,
    begin_todo_runtime_shadow_capture,
    settle_todo_runtime_shadow_capture,
)
from .state_refresh import now_local
from .control_plane.todos.contract import TODO_METADATA_FIELDS, format_todo_metadata_line
from .control_plane.todos.path_resolution import resolve_todo_state_path
from .control_plane.effect_runtime import EffectRuntimeRejected, effect_runtime_result
from .control_plane.todos.provider_followups import capture_canonical_followups_if_promoted
from .control_plane.todos.active_state_editing import (
    insert_into_existing_section,
    insert_new_section,
    replace_updated_at,
    section_bounds,
    todo_blocks,
)


def capture_followup_todos(
    *,
    registry_path: Path,
    goal_id: str,
    followups: list[str],
    evidence: str,
    task_class: str | None = None,
    action_kind: str | None = None,
    continuation_policy: str | None = None,
    capture_operation_id: str | None = None,
    required_write_scopes: list[str] | None = None,
    required_capabilities: list[str] | None = None,
    target_capabilities: list[str] | None = None,
    required_decision_scopes: Any = None,
    project: Path | None = None,
    state_file: Path | None = None,
    dry_run: bool = False,
    runtime_root_arg: str | None = None,
) -> dict[str, Any]:
    runtime_root = effective_runtime_root(registry_path, runtime_root_arg)
    operation_id = capture_operation_id if capture_operation_id is not None else f"followup-capture:{uuid4().hex}"
    intent = {"followups": followups, "evidence": evidence, "metadata": {
        key: value for key, value in {
            "task_class": task_class, "action_kind": action_kind,
            "continuation_policy": continuation_policy,
            "required_write_scopes": required_write_scopes,
            "required_capabilities": required_capabilities,
            "target_capabilities": target_capabilities,
            "required_decision_scopes": required_decision_scopes,
        }.items() if value is not None
    }}
    canonical = capture_canonical_followups_if_promoted(
        registry_path=registry_path, runtime_root=runtime_root, goal_id=goal_id,
        operation_id=operation_id, intent=intent, dry_run=dry_run,
        project=project, state_file=state_file,
    )
    if canonical is not None:
        return canonical
    if capture_operation_id is not None:
        raise ValueError("--capture-operation-id requires promoted canonical authority; legacy capture has no durable command receipt")

    resolved_project, resolved_state_file = resolve_todo_state_path(
        registry_path=registry_path,
        goal_id=goal_id,
        project=project,
        state_file=state_file,
    )

    with legacy_todo_write_transaction(
        registry_path, goal_id, resolved_state_file, None, "todo_capture_followups",
        dry_run, runtime_root=runtime_root,
    ):
        original = resolved_state_file.read_text(encoding="utf-8")
        lines = original.splitlines()
        bounds = section_bounds(lines, "agent")
        existing = todo_blocks(lines, bounds[0], bounds[1], role="agent",
                               source_section=bounds[2], text_limit=None) if bounds else []
        updated_at = now_local()
        try:
            result = effect_runtime_result("todos.followup_capture.plan", {
                "schema_version": "todo_followup_capture_plan_request_v0",
                "goal_id": goal_id, "operation_id": operation_id,
                "updated_at": updated_at, "dry_run": dry_run, "intent": intent,
                "existing_texts": [block["text"] for block in existing],
            })
        except EffectRuntimeRejected as exc:
            raise ValueError(str(exc)) from None
        if not isinstance(result, dict) or result.get("schema_version") != "todo_followup_capture_result_v0":
            raise ValueError("TypeScript follow-up capture plan shape mismatch")
        changed = result["changed"]
        recorded_count = result["recorded_count"]
        capture = begin_todo_runtime_shadow_capture(
            registry_path=registry_path, runtime_root=runtime_root, goal_id=goal_id,
            state_path=resolved_state_file, write_class="todo_capture_followups",
            original_text=original,
        )
        # This adapter renders accepted rows only. Do not call single-add here:
        # its duplicate/admission decisions would recreate a second batch owner.
        for item in result["items"]:
            if not item["added"]:
                continue
            metadata = format_todo_metadata_line(**{
                key: value for key, value in item.items() if key in TODO_METADATA_FIELDS
            })
            row = f"- [ ] {item['todo']}\n{metadata}"
            bounds = section_bounds(lines, "agent")
            if bounds:
                insert_into_existing_section(lines, bounds[0], bounds[1], row)
            else:
                insert_new_section(lines, "agent", row)

        if changed:
            new_text = "\n".join(lines) + ("\n" if original.endswith("\n") else "")
            new_text = replace_updated_at(new_text, updated_at)
            if not dry_run:
                write_captured_todo_state(capture, runtime_root=runtime_root, goal_id=goal_id,
                    state_path=resolved_state_file, text=new_text)

    result.update(state_file=str(resolved_state_file),
                  project=str(resolved_project) if resolved_project else None)
    if changed and not dry_run:
        from .control_plane.coordination.local_authority_shadow_observation import observe_local_authority_commit

        shadow = observe_local_authority_commit(
            registry_path=registry_path,
            runtime_root=runtime_root,
            goal_id=goal_id,
            observation_trigger=(
                f"todo_capture_followups:{recorded_count}:{updated_at}"
            ),
        )
        if shadow is not None:
            result["authority_shadow"] = shadow
    return settle_todo_runtime_shadow_capture(
        result, registry_path=registry_path, runtime_root=runtime_root,
        goal_id=goal_id, write_class="todo_capture_followups", capture=capture,
        observe_legacy=False, emit_disabled=False,
    )
