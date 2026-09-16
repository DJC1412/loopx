#!/usr/bin/env python3
"""Smoke-check compact path deltas survive the goal-vision read path."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loopx.control_plane.goals.goal_frontier import (  # noqa: E402
    latest_agent_vision_from_runs,
)
from loopx.control_plane.goals.goal_vision import (  # noqa: E402
    compact_goal_vision_packet,
    misplaced_goal_path_delta_field,
    normalize_goal_vision_packet,
)
from loopx.control_plane.runtime.shared_runtime_refresh_projection import (  # noqa: E402
    build_shared_runtime_projection,
)


GOAL_ID = "fixture-goal"
AGENT_ID = "fixture-agent"


def packet() -> dict[str, object]:
    return {
        "agent_id": AGENT_ID,
        "state": "vision_drift_detected",
        "vision_patch": {
            "vision_summary": "Route one bounded successor from verified evidence.",
            "acceptance_summary": "The successor falsifies or confirms the new path.",
            "replan_trigger_summary": "The prior monitor-only path made no progress.",
        },
        "path_delta": {
            "outcome": "replan",
            "prior_assumption": "The monitor lane would produce acceptance evidence.",
            "observed_reality": "Two bounded polls produced no material transition.",
            "retained": ["Keep the verified monitor target."],
            "changed": ["Create one runnable advancement successor."],
            "stopped": ["Stop treating future polling as completion evidence."],
            "unresolved_questions": ["Can the successor falsify the new path?"],
            "reentry_condition": "Resume waiting after successor evidence lands.",
            "evidence_refs": ["evidence:monitor-poll-02", "todo:successor-01"],
        },
    }


def main() -> int:
    normalized = normalize_goal_vision_packet(
        packet(), goal_id=GOAL_ID, agent_id=AGENT_ID
    )
    path_delta = normalized["path_delta"]
    assert path_delta["schema_version"] == "goal_path_delta_v0", path_delta
    assert path_delta["outcome"] == "replan", path_delta
    assert normalized["vision_budget"]["total_usage"] <= 1200, normalized

    compact = compact_goal_vision_packet(normalized)
    assert compact is not None, normalized
    assert compact["path_delta"] == path_delta, compact

    record = {
        "generated_at": "2026-07-20T00:00:00+00:00",
        "goal_id": GOAL_ID,
        "classification": "autonomous_replan_recorded",
        "agent_id": AGENT_ID,
        "agent_vision": normalized,
        "state": {"sha256_16": "0123456789abcdef", "frontmatter": {}},
    }
    shared, _ = build_shared_runtime_projection(record=record)
    assert shared["agent_vision"]["path_delta"] == path_delta, shared

    latest = latest_agent_vision_from_runs(
        [shared], goal_id=GOAL_ID, agent_id=AGENT_ID
    )
    assert latest is not None, shared
    assert latest["path_delta"] == path_delta, latest

    invalid = packet()
    invalid["path_delta"] = {"outcome": "replan"}
    try:
        normalize_goal_vision_packet(invalid, goal_id=GOAL_ID, agent_id=AGENT_ID)
    except ValueError as exc:
        assert "requires prior_assumption and observed_reality" in str(exc), exc
    else:
        raise AssertionError("incomplete path delta should fail")

    missing_disposition = packet()
    missing_disposition["path_delta"] = {
        "outcome": "wait",
        "prior_assumption": "A dependency would become available.",
        "observed_reality": "The dependency remains unavailable.",
    }
    try:
        normalize_goal_vision_packet(
            missing_disposition, goal_id=GOAL_ID, agent_id=AGENT_ID
        )
    except ValueError as exc:
        assert "requires at least one retained, changed, or stopped item" in str(exc), exc
    else:
        raise AssertionError("path delta without a disposition should fail")

    over_item_limit = packet()
    over_item_limit["path_delta"]["retained"] = ["item"] * 4
    try:
        normalize_goal_vision_packet(
            over_item_limit, goal_id=GOAL_ID, agent_id=AGENT_ID
        )
    except ValueError as exc:
        assert "path_delta.retained has 4 items; limit is 3" in str(exc), exc
    else:
        raise AssertionError("over-item path delta should fail")

    # The delta is nested under `path_delta`; `goal_path_delta_v0` is only its
    # schema_version. A packet that nests it under the schema name would drop
    # the delta silently, so the write path has to name the accepted key.
    assert misplaced_goal_path_delta_field(packet()) is None
    assert misplaced_goal_path_delta_field({"path_delta": {"outcome": "wait"}}) is None
    assert misplaced_goal_path_delta_field("not-a-packet") is None
    misfiled = dict(packet())
    misfiled["goal_path_delta_v0"] = misfiled.pop("path_delta")
    assert misplaced_goal_path_delta_field(misfiled) == (
        "goal_path_delta_v0",
        "goal_path_delta_v0",
    ), misfiled
    # A read-path compaction of the misfiled packet loses the delta, which is
    # exactly the silent drop the write-path guard exists to prevent.
    misfiled_compact = compact_goal_vision_packet(misfiled)
    assert "path_delta" not in (misfiled_compact or {}), misfiled_compact
    # Placing both keys is not a misfiling: the read path still finds the delta.
    both_keys = dict(packet())
    both_keys["goal_path_delta_v0"] = {"schema_version": "goal_path_delta_v0"}
    assert misplaced_goal_path_delta_field(both_keys) is None
    # One shared key is not enough to call an unrelated object a path delta.
    assert (
        misplaced_goal_path_delta_field({"telemetry": {"outcome": "ok"}}) is None
    )
    assert misplaced_goal_path_delta_field(
        {"telemetry": {"outcome": "ok", "evidence_refs": ["evidence:x"]}}
    ) == ("telemetry", "goal_path_delta_v0")

    print("goal-vision-path-delta-smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
