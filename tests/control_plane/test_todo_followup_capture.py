"""Public batch capture regressions, including real canonical stores."""
from pathlib import Path
import runpy

import pytest

from loopx.todo_followups import capture_followup_todos
from loopx.control_plane.todos.active_state_todo_parser import parse_todo_source

_SMOKE = runpy.run_path(str(Path(__file__).resolve().parents[2] / "examples/control_plane/todo-capture-followups-smoke.py"))
fixture = _SMOKE["write_fixture"]
cli = _SMOKE["run_cli"]
GOAL = _SMOKE["GOAL_ID"]


def test_capture_cli_preserves_continuation_policy(tmp_path):
    registry, state = fixture(tmp_path)
    result = cli(registry, "todo", "capture-followups", "--goal-id", GOAL,
                 "--follow-up", "Validate public follow-up capture", "--evidence", "validation://capture",
                 "--continuation-policy", "same_agent_non_delivery")
    assert result["recorded_count"] == 1
    items, _, _ = parse_todo_source(state.read_text())
    assert items["agent"][-1].get("continuation_policy") == "same_agent_non_delivery"


@pytest.mark.parametrize("provider", [None, "file", "sqlite"])
def test_capture_uses_full_text_identity(tmp_path, monkeypatch, provider):
    registry, state = fixture(tmp_path)
    if provider is not None:
        from canonical_authority_fixture import initialize_canonical_authority, isolate_sqlite_runtime
        from loopx.control_plane.coordination.runtime_shadow import build_todo_runtime_shadow_projection
        from loopx.todos import list_goal_todos
        if provider == "sqlite":
            isolate_sqlite_runtime(tmp_path, monkeypatch)
        projection = build_todo_runtime_shadow_projection(goal_id=GOAL, handoff_mode="hard_lease",
            todos=list_goal_todos(registry_path=registry, goal_id=GOAL)["todos"])
        initialize_canonical_authority(tmp_path / "runtime", GOAL, projection, state_path=state, provider=provider)
    prefix = "[P1] " + "long text " * 60
    one, two = prefix + "first invariant", prefix + "second invariant"
    args = dict(registry_path=registry, goal_id=GOAL, followups=[one, two], evidence="validation://capture")
    result = capture_followup_todos(**args)
    assert result["recorded_count"] == 2
    assert all(item["added"] for item in result["items"])
    assert one in state.read_text() and two in state.read_text()
    replay = capture_followup_todos(**args)
    assert replay["recorded_count"] == 0
    assert all(item["skipped_reason"] == "duplicate" for item in replay["items"])


def test_legacy_capture_characterization(tmp_path):
    registry, state = fixture(tmp_path)
    original = state.read_bytes()
    args = dict(registry_path=registry, goal_id=GOAL,
                followups=["", " First  successor ", "First successor", "Inspect file://raw", "Second successor", "Third successor"],
                evidence="validation://capture")
    preview = capture_followup_todos(**args, dry_run=True)
    assert preview["recorded_count"] == 2
    assert [x["skipped_reason"] for x in preview["items"]] == ["empty", None, "duplicate", "unsafe_boundary:local_absolute_path", None, "max_items_exceeded"]
    assert state.read_bytes() == original
    captured = capture_followup_todos(**args)
    assert captured["recorded_count"] == 2
    assert "claimed_by" not in state.read_text()
    with pytest.raises(ValueError, match="public-safe"):
        capture_followup_todos(**{**args, "evidence": "password=redacted-fixture"})


@pytest.fixture(params=["file", "sqlite"])
def canonical(tmp_path, monkeypatch, request):
    from canonical_authority_fixture import initialize_canonical_authority, isolate_sqlite_runtime
    from loopx.control_plane.coordination.runtime_shadow import build_todo_runtime_shadow_projection
    from loopx.control_plane.coordination.local_authority import read_canonical_todos_if_promoted
    from loopx.todos import list_goal_todos
    if request.param == "sqlite":
        isolate_sqlite_runtime(tmp_path, monkeypatch)
    registry, state = fixture(tmp_path)
    runtime = tmp_path / "runtime"
    projection = build_todo_runtime_shadow_projection(goal_id=GOAL, handoff_mode="hard_lease",
        todos=list_goal_todos(registry_path=registry, goal_id=GOAL)["todos"])
    initialize_canonical_authority(runtime, GOAL, projection, state_path=state, provider=request.param)
    def read():
        return read_canonical_todos_if_promoted(runtime_root=runtime, goal_id=GOAL, include_leases=True)
    return registry, state, runtime, read


def test_canonical_cli_batch_retry_and_missing_display(canonical):
    registry, state, _, read = canonical
    initial = read()
    args = ("todo", "capture-followups", "--goal-id", GOAL, "--follow-up", "First canonical task",
        "--follow-up", "Second canonical task", "--evidence", "validation://capture-cli",
        "--continuation-policy", "same_agent_non_delivery", "--capture-operation-id", "capture-cli")
    state.unlink()
    preview = cli(registry, *args, "--dry-run")
    assert preview["status"] == "planned" and preview["recorded_count"] == 2
    assert read() == initial and not state.exists()
    applied = cli(registry, *args)
    assert applied["status"] == "applied", applied
    assert applied["projection_delivery"] == "delivered", applied
    assert applied["recorded_count"] == 2
    first = read()
    assert len(first["todos"]) == len(initial["todos"]) + 2
    assert first["leases"] == initial["leases"]
    added = [x for x in first["todos"] if x["text"] in {"First canonical task", "Second canonical task"}]
    assert len(added) == 2 and all(x.get("claimed_by") is None for x in added)
    assert all(x["continuation_policy"] == "same_agent_non_delivery" for x in added)
    later = capture_followup_todos(registry_path=registry, goal_id=GOAL,
        followups=["A later task"], evidence="validation://later")
    assert later["recorded_count"] == 1
    latest = read()
    state.unlink()
    replay = cli(registry, *args)
    assert replay["status"] == "replayed" and replay["changed"] is False
    assert replay["original_receipt"] == applied["original_receipt"]
    assert replay["recorded_count"] == 2  # Historical accepted batch, not new insertions.
    assert read() == latest
    assert "A later task" in state.read_text()  # Display drains latest, not receipt head.
    mismatch = cli(registry, *args, "--follow-up", "Changed request", check=False)
    assert mismatch["ok"] is False and mismatch["error_code"] == "coordination_operation_identity_mismatch"
    assert read() == latest


def test_canonical_projection_failure_is_recoverable(canonical, monkeypatch):
    from loopx.control_plane.todos import provider_projection
    registry, state, _, read = canonical
    original = state.read_bytes()
    render = provider_projection.project_current_canonical_todos
    def fail(**_kwargs):
        raise OSError("synthetic display unavailable")
    monkeypatch.setattr(provider_projection, "project_current_canonical_todos", fail)
    args = dict(registry_path=registry, goal_id=GOAL, followups=["Committed before rendering"],
        evidence="validation://capture", capture_operation_id="pending-display")
    applied = capture_followup_todos(**args)
    assert applied["ok"] and applied["projection_delivery"] == "pending"
    after = read()
    assert any(x["text"] == "Committed before rendering" for x in after["todos"])
    assert state.read_bytes() == original
    monkeypatch.setattr(provider_projection, "project_current_canonical_todos", render)
    replay = capture_followup_todos(**args)
    assert replay["status"] == "replayed" and replay["projection_delivery"] == "delivered"
    assert read() == after


def test_canonical_provider_outage_never_writes_legacy(canonical, monkeypatch):
    from loopx.control_plane.todos import provider_followups
    from loopx.control_plane.coordination.local_authority import LocalCoordinationAuthorityUnavailable
    registry, state, _, read = canonical
    before, original = read(), state.read_bytes()
    monkeypatch.setattr(provider_followups, "effect_runtime_result", lambda *_args: {
        "status": "unavailable", "reason_code": "synthetic_outage", "reason": "provider unavailable"})
    with pytest.raises(LocalCoordinationAuthorityUnavailable) as error:
        capture_followup_todos(registry_path=registry, goal_id=GOAL,
            followups=["Must not fall back"], evidence="validation://capture", capture_operation_id="retry-this")
    assert error.value.payload["capture_operation_id"] == "retry-this"
    assert state.read_bytes() == original and read() == before


def test_legacy_rejects_durable_operation_identity(tmp_path):
    registry, state = fixture(tmp_path)
    before = state.read_bytes()
    with pytest.raises(ValueError, match="requires promoted"):
        capture_followup_todos(registry_path=registry, goal_id=GOAL,
            followups=["Requires canonical history"], evidence="validation://capture", capture_operation_id="durable")
    assert state.read_bytes() == before


@pytest.mark.parametrize("metadata", [{"required_capabilities": ["valid", "bad/token"]},
    {"required_write_scopes": ["src/**", "../escape"]}, {"continuation_policy": "removed-policy"}])
def test_legacy_invalid_metadata_is_atomic(tmp_path, metadata):
    registry, state = fixture(tmp_path)
    before = state.read_bytes()
    with pytest.raises(ValueError):
        capture_followup_todos(registry_path=registry, goal_id=GOAL,
            followups=["First task", "Second task"], evidence="validation://capture", **metadata)
    assert state.read_bytes() == before


def test_preview_refuses_missing_promoted_provider(tmp_path):
    from loopx.control_plane.coordination.legacy_writer_fence import legacy_coordination_writer_fence_path
    registry, state = fixture(tmp_path)
    fence = legacy_coordination_writer_fence_path(runtime_root=tmp_path / "runtime", goal_id=GOAL)
    fence.parent.mkdir(parents=True)
    fence.write_text("{invalid")
    before = state.read_bytes()
    result = cli(registry, "todo", "capture-followups", "--goal-id", GOAL, "--follow-up", "No fake preview",
        "--evidence", "validation://capture", "--dry-run", check=False)
    assert result["ok"] is False and result["legacy_fallback_used"] is False
    assert state.read_bytes() == before
