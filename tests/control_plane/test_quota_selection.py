"""Quota scope is distinct from execution ownership; all data are synthetic."""

import pytest
import json
from pathlib import Path

from canonical_authority_fixture import initialize_canonical_authority
from loopx.control_plane.testing.canary_harness import (
    write_fixture_registry,
    run_json_cli_result,
)
from loopx.control_plane.coordination.runtime_shadow import (
    build_todo_runtime_shadow_projection,
)
from loopx.control_plane.todos.active_state_todo_parser import parse_active_state_todos

from loopx.control_plane.todos.quota_summary import (
    compact_quota_todo_summary_for_payload,
    summarize_user_todos_for_quota,
)
from loopx.control_plane.agents.agent_scope import _agent_scope_no_candidate_frontier


def summary(items):
    return {
        "schema_version": "todo_summary_v0",
        "source_section": "User Todo",
        "items": items,
        "first_open_items": items,
        "total_count": len(items),
        "open_count": len(items),
        "done_count": 0,
        "deferred_count": 0,
    }


def item(todo_id, **fields):
    return {
        "todo_id": todo_id,
        "text": "Synthetic scoped work",
        "index": 1,
        "priority": "P1",
        "status": "open",
        "task_class": "user_gate",
        **fields,
    }


@pytest.mark.parametrize("scope", [{"global_gate": True}, {"blocks_agent": "agent-b"}])
@pytest.mark.parametrize("exclusions", [[], ["agent-b"]])
def test_explicit_user_gate_scope_is_not_erased_by_executor_claim(scope, exclusions):
    gate = item("todo_gate", claimed_by="agent-a", excluded_agents=exclusions, **scope)
    result = summarize_user_todos_for_quota(
        summary([gate]),
        agent_identity={"agent_id": "agent-b"},
        filter_user_gate_blocks_agent=True,
    )
    assert [row["todo_id"] for row in result["gate_open_items"]] == ["todo_gate"]
    assert result["open_count"] == 1
    assert result["first_executable_items"] == []


def test_scope_precedence_keeps_other_lane_gate_diagnostic():
    gate = item("todo_gate", claimed_by="agent-b", blocks_agent="agent-a")
    result = summarize_user_todos_for_quota(
        summary([gate]),
        agent_identity={"agent_id": "agent-b"},
        filter_user_gate_blocks_agent=True,
    )
    assert result["gate_open_items"] == []
    assert result["other_agent_scoped_open_count"] == 1
    assert result["open_count"] == 0


def test_agent_execution_still_respects_claim_and_exclusion():
    rows = [
        item("todo_peer", task_class="advancement_task", claimed_by="agent-a"),
        item(
            "todo_excluded", task_class="advancement_task", excluded_agents=["agent-b"]
        ),
        item("todo_free", task_class="advancement_task"),
        item("todo_owned", task_class="advancement_task", claimed_by="agent-b"),
    ]
    result = summarize_user_todos_for_quota(
        summary(rows), agent_identity={"agent_id": "agent-b"}
    )
    assert [row["todo_id"] for row in result["first_executable_items"]] == [
        "todo_owned",
        "todo_free",
    ]
    assert result["claim_scope"]["executor_excluded_self_count"] == 1
    assert result["claim_scope"]["other_agent_claimed_open_count"] == 1


def test_executor_excluded_handoff_projects_dispatchable_and_no_peer_outcomes():
    rows = [
        item(
            "todo_dispatch",
            task_class="advancement_task",
            continuation_policy="independent_handoff",
            excluded_agents=["agent-b"],
        ),
        item(
            "todo_no_peer",
            task_class="advancement_task",
            continuation_policy="independent_handoff",
            excluded_agents=["agent-a", "agent-b"],
        ),
    ]
    result = summarize_user_todos_for_quota(
        summary(rows),
        agent_identity={
            "agent_id": "agent-b",
            "registered_agents": ["agent-a", "agent-b"],
        },
    )
    scope = result["claim_scope"]
    assert scope["executor_excluded_dispatchable_count"] == 1
    assert scope["executor_excluded_dispatchable_items"][0]["eligible_peer_ids"] == [
        "agent-a"
    ]
    assert scope["executor_excluded_no_eligible_peer_count"] == 1
    assert (
        scope["executor_excluded_no_eligible_peer_items"][0]["eligible_peer_ids"] == []
    )

    frontier = _agent_scope_no_candidate_frontier(
        agent_identity={"agent_id": "agent-b"},
        agent_todo_summary=result,
        agent_lane_next_action=None,
        work_lane_contract={
            "lane": "advancement_task",
            "must_attempt_work": True,
        },
        candidate_should_run=True,
    )
    assert frontier is not None
    assert frontier["action"] == "successor_replan_required"
    assert frontier["handoff_dispatch_required"] is True
    assert frontier["quiet_noop_allowed"] is False
    assert frontier["eligible_peer_ids"] == ["agent-a"]
    compact = compact_quota_todo_summary_for_payload(result)
    assert compact["claim_scope"]["executor_excluded_dispatchable_items"][0][
        "eligible_peer_ids"
    ] == ["agent-a"]


def test_executor_excluded_handoff_without_peer_is_nonquiet_and_not_dispatched():
    result = summarize_user_todos_for_quota(
        summary(
            [
                item(
                    "todo_no_peer",
                    task_class="advancement_task",
                    continuation_policy="independent_handoff",
                    excluded_agents=["agent-a", "agent-b"],
                )
            ]
        ),
        agent_identity={
            "agent_id": "agent-b",
            "registered_agents": ["agent-a", "agent-b"],
        },
    )
    frontier = _agent_scope_no_candidate_frontier(
        agent_identity={"agent_id": "agent-b"},
        agent_todo_summary=result,
        agent_lane_next_action=None,
        work_lane_contract={"lane": "advancement_task", "must_attempt_work": True},
        candidate_should_run=True,
    )
    assert frontier is not None
    assert frontier["quiet_noop_allowed"] is False
    assert frontier["handoff_dispatch_required"] is False
    assert frontier["handoff_dispatch_state"] == "no_eligible_peer"
    assert frontier["eligible_peer_ids"] == []


def test_quota_dispatch_survives_process_restart_and_replays_one_peer_inbox(
    tmp_path: Path,
):
    runtime = tmp_path / "runtime"
    registry = tmp_path / "registry.json"
    state = tmp_path / "state.md"
    state.write_text(
        "---\nstatus: active\n---\n# Goal\n## Objective\nDeliver a checked change.\n\n"
        "## Agent Todo\n"
        "- [ ] [P0] Implement the independent review.\n"
        "  <!-- loopx:todo todo_id=todo_handoff status=open "
        "task_class=advancement_task continuation_policy=independent_handoff "
        "excluded_agents=agent-b -->\n"
    )
    write_fixture_registry(
        project=tmp_path,
        runtime_root=runtime,
        registry_path=registry,
        goal_id="goal-handoff",
        domain="quota-handoff",
        adapter_kind="generic_project_goal_v0",
        state_file=str(state),
        registered_agents=["agent-a", "agent-b"],
        quota_allowed_slots=None,
    )
    goal = json.loads(registry.read_text())["goals"][0]
    fields = parse_active_state_todos(state.read_text(), goal=goal, item_limit=None)
    projection = build_todo_runtime_shadow_projection(
        goal_id="goal-handoff",
        todos=(
            fields["agent_todos"]["items"]
            + fields.get("user_todos", {}).get("items", [])
        ),
        handoff_mode="soft_claim",
    )
    initialize_canonical_authority(
        runtime,
        "goal-handoff",
        projection,
        state_path=state,
    )

    args = (
        "quota",
        "should-run",
        "--goal-id",
        "goal-handoff",
        "--agent-id",
        "agent-b",
        "--scan-path",
        str(tmp_path),
    )
    code, first = run_json_cli_result(
        *args,
        registry_path=registry,
        runtime_root=runtime,
    )
    assert code == 0, first
    receipt = first["agent_handoff_dispatch_receipt"]
    assert receipt["status"] == "dispatched" and receipt["replayed"] is False
    assert receipt["to_agent_id"] == "agent-a"

    code, replay = run_json_cli_result(
        *args,
        registry_path=registry,
        runtime_root=runtime,
    )
    assert code == 0, replay
    assert (
        replay["agent_handoff_dispatch_receipt"]["dispatch_id"]
        == receipt["dispatch_id"]
    )
    assert replay["agent_handoff_dispatch_receipt"]["replayed"] is True

    code, inbox = run_json_cli_result(
        "manager-inbox",
        "read",
        "--goal-id",
        "goal-handoff",
        "--agent-id",
        "agent-a",
        registry_path=registry,
        runtime_root=runtime,
    )
    assert code == 0, inbox
    assert [item["dispatch_id"] for item in inbox["items"]] == [receipt["dispatch_id"]]
    assert inbox["items"][0]["inbox_kind"] == "agent_handoff"
    assert inbox["handoff_followthrough"]

    code, claimed = run_json_cli_result(
        "todo",
        "claim",
        "--goal-id",
        "goal-handoff",
        "--todo-id",
        "todo_handoff",
        "--claimed-by",
        "agent-a",
        "--agent-id",
        "agent-a",
        "--role",
        "agent",
        "--claim-operation-id",
        f"agent-handoff-{receipt['dispatch_id'][:32]}",
        registry_path=registry,
        runtime_root=runtime,
    )
    assert code == 0, claimed
    code, acknowledged = run_json_cli_result(
        "manager-inbox",
        "acknowledge-handoff",
        "--goal-id",
        "goal-handoff",
        "--agent-id",
        "agent-a",
        "--request-id",
        receipt["dispatch_id"],
        registry_path=registry,
        runtime_root=runtime,
    )
    assert code == 0, acknowledged
    assert acknowledged["canonical_claim_verified"] is True
    assert acknowledged["status"] == "claimed"
    code, empty = run_json_cli_result(
        "manager-inbox",
        "read",
        "--goal-id",
        "goal-handoff",
        "--agent-id",
        "agent-a",
        registry_path=registry,
        runtime_root=runtime,
    )
    assert code == 0, empty
    assert empty["items"] == []


@pytest.mark.parametrize("display", ["legacy", "missing", "stale"])
@pytest.mark.parametrize("scope", ["global_gate=true", "blocks_agent=agent-b"])
def test_public_quota_keeps_gate_from_real_canonical_provider(
    tmp_path: Path, display: str, scope: str
):
    runtime, registry, state = (
        tmp_path / "runtime",
        tmp_path / "registry.json",
        tmp_path / "state.md",
    )
    history = "\n".join(
        f"- [x] [P2] Completed synthetic work {i}.\n"
        f"  <!-- loopx:todo todo_id=todo_history_{i} status=done task_class=advancement_task -->"
        for i in range(12)
    )
    state.write_text(
        "---\nstatus: active\n---\n# Goal\n## Objective\nDeliver a checked change.\n\n"
        "## Agent Todo\n" + history + "\n\n## User Todo\n"
        "- [ ] [P0] Owner approval is required.\n"
        f"  <!-- loopx:todo todo_id=todo_gate task_class=user_gate status=open claimed_by=agent-a {scope} -->\n"
    )
    write_fixture_registry(
        project=tmp_path,
        runtime_root=runtime,
        registry_path=registry,
        goal_id="goal-scope",
        domain="quota-scope",
        adapter_kind="generic_project_goal_v0",
        state_file=str(state),
        registered_agents=["agent-a", "agent-b"],
        quota_allowed_slots=None,
    )
    if display != "legacy":
        goal = json.loads(registry.read_text())["goals"][0]
        fields = parse_active_state_todos(state.read_text(), goal=goal, item_limit=None)
        projection = build_todo_runtime_shadow_projection(
            goal_id="goal-scope",
            todos=fields["agent_todos"]["items"] + fields["user_todos"]["items"],
            handoff_mode="soft_claim",
        )
        initialize_canonical_authority(
            runtime, "goal-scope", projection, state_path=state
        )
        if display == "missing":
            state.unlink()
        else:
            state.write_text("# Stale projection\n## User Todo\n- [x] Old approval.\n")
    before = state.read_bytes() if state.exists() else None
    code, packet = run_json_cli_result(
        "quota",
        "should-run",
        "--goal-id",
        "goal-scope",
        "--agent-id",
        "agent-b",
        "--scan-path",
        str(tmp_path),
        registry_path=registry,
        runtime_root=runtime,
    )
    assert code == 0, packet
    assert packet["requires_user_action"] is True
    gates = packet["user_todo_summary"]["gate_open_items"]
    assert [row["todo_id"] for row in gates] == ["todo_gate"]
    assert not packet["user_todo_summary"].get("claim_scope")
    assert (state.read_bytes() if state.exists() else None) == before
