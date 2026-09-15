from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from loopx.control_plane.heartbeat import automation_upgrade as upgrade
from loopx.control_plane.quota.app_automation_observation import (
    observe_lane_app_automation,
)
from loopx.control_plane.quota.should_run import build_quota_should_run
from loopx.control_plane.scheduler.execution_context import (
    scheduler_execution_context_for_runtime_profile,
)
from loopx.control_plane.testing.quota_fixtures import (
    quota_status_payload,
    quota_todo_item,
)


APP_CONTEXT = scheduler_execution_context_for_runtime_profile("codex_app_heartbeat")
CLI_CONTEXT = scheduler_execution_context_for_runtime_profile("codex_cli")
GOAL_ID = "fixture-goal"
AGENT_ID = "agent-a"
THREAD_ID = "thread-a"
FROZEN_BODY = f"Advance `{GOAL_ID}` from registry. --agent-id {AGENT_ID}"


def _host_with_frozen_body(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "host"
    path = home / "automations/watch/automation.toml"
    path.parent.mkdir(parents=True)
    path.write_text('version = 1\nid = "watch"\nname = "Fixture watch"\nkind = "heartbeat"\n'
                    'status = "ACTIVE"\ntarget_thread_id = "' + THREAD_ID + '"\n'
                    'rrule = "FREQ=MINUTELY;INTERVAL=3"\n'
                    'prompt = ' + json.dumps(FROZEN_BODY) + "\n", encoding="utf-8")
    database = home / "sqlite/codex-dev.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE automations (id TEXT PRIMARY KEY, kind TEXT, prompt TEXT,"
                           " status TEXT, target_thread_id TEXT, rrule TEXT, model TEXT,"
                           " updated_at INTEGER, next_run_at INTEGER)")
        connection.execute("INSERT INTO automations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("watch", "heartbeat", FROZEN_BODY, "ACTIVE", THREAD_ID,
             "FREQ=MINUTELY;INTERVAL=3", "fixture-model", 123, 456))
    registry = tmp_path / "registry.json"
    state = tmp_path / "STATE.md"
    state.write_text("# Fixture\n", encoding="utf-8")
    registry.write_text(json.dumps({"goals": [{"id": GOAL_ID, "repo": str(tmp_path),
        "state_file": str(state), "registered_agents": [AGENT_ID]}]}), encoding="utf-8")
    return home, registry


def _lane_payload(tmp_path: Path, registry: Path) -> dict:
    """One runnable lane whose host context the packet has to observe."""

    return {
        **quota_status_payload(
            goal_id=GOAL_ID,
            status="active",
            agent_todo_items=[
                quota_todo_item(
                    todo_id="todo_current001",
                    index=1,
                    priority="P1",
                    title="Advance the reviewed slice.",
                    claimed_by=AGENT_ID,
                )
            ],
            recommended_action="Advance the reviewed slice.",
            coordination={"agent_model": "peer_v1", "registered_agents": [AGENT_ID]},
            claim_scope_agent_id=AGENT_ID,
        ),
        "registry": str(registry),
        "runtime_root": str(tmp_path / "runtime"),
    }


def test_app_lane_projects_the_installed_prompt_binding(tmp_path, monkeypatch):
    home, registry = _host_with_frozen_body(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)
    packet = build_quota_should_run(
        _lane_payload(tmp_path, registry),
        goal_id=GOAL_ID,
        agent_id=AGENT_ID,
        scheduler_execution_context=APP_CONTEXT,
    )
    hint = packet["scheduler_hint"]
    binding = hint["app_automation"]["prompt_binding"]
    assert binding["status"] == "adoption_required"
    assert binding["automation_id"] == "watch"
    assert binding["host_action"] == upgrade.PROMPT_BINDING_ADOPT_ACTION
    assert binding["host_action_contract"] == upgrade.PROMPT_BINDING_HOST_ACTION_CONTRACT
    assert binding["api_update_request"]["arguments"]["prompt"].startswith(
        "LoopX managed heartbeat bootstrap v2\n"
    )
    # The legacy Codex App projection carries the same observed automation.
    assert hint["codex_app"]["prompt_binding"] == binding
    assert hint["app_automation"]["no_spend_for_cadence_change"] is True


def test_non_app_hosts_never_observe_a_prompt_binding(tmp_path, monkeypatch):
    home, registry = _host_with_frozen_body(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)
    packet = build_quota_should_run(
        _lane_payload(tmp_path, registry),
        goal_id=GOAL_ID,
        agent_id=AGENT_ID,
        scheduler_execution_context=CLI_CONTEXT,
    )
    assert "prompt_binding" not in json.dumps(packet["scheduler_hint"])


def test_lane_without_an_installed_automation_observes_nothing(tmp_path):
    observation = observe_lane_app_automation(
        {"registry": str(tmp_path / "registry.json"), "runtime_root": str(tmp_path)},
        goal_id=GOAL_ID,
        agent_id=AGENT_ID,
        surface="codex_app",
    )
    assert observation.prompt_binding is None
    assert observation.scheduler_state is None
