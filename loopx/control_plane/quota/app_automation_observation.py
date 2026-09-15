"""Observe this lane's Codex App automation for one turn contract.

The turn packet has to describe the automation the host actually runs, not what
the last update intended: the cadence state lives under the runtime root, and a
frozen installed body keeps applying the policy of the day it was installed.
Both reads are bounded and fail-open, so a missing host store degrades to "not
observed" instead of failing the turn.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from ..scheduler.state import load_app_automation_scheduler_state
from ..todos.contract import normalize_todo_claimed_by


@dataclass(frozen=True)
class LaneAppAutomationObservation:
    """The lane's App automation as the host has it, with unobserved parts None."""

    scheduler_state: dict[str, Any] | None
    prompt_binding: dict[str, Any] | None


def observe_lane_app_automation(
    status_payload: dict[str, Any],
    *,
    goal_id: str,
    agent_id: str | None,
    surface: str,
) -> LaneAppAutomationObservation:
    """Read the lane's cadence state and installed prompt body once per turn."""

    safe_agent_id = normalize_todo_claimed_by(agent_id)
    raw_runtime_root = status_payload.get("runtime_root")
    return LaneAppAutomationObservation(
        scheduler_state=(
            load_app_automation_scheduler_state(
                Path(str(raw_runtime_root)).expanduser(),
                goal_id=goal_id,
                agent_id=safe_agent_id,
                surface=surface,
            )
            if raw_runtime_root and safe_agent_id
            else None
        ),
        prompt_binding=_installed_prompt_binding(
            status_payload,
            goal_id=goal_id,
            agent_id=safe_agent_id,
        ),
    )


def _installed_prompt_binding(
    status_payload: dict[str, Any],
    *,
    goal_id: str,
    agent_id: str | None,
) -> dict[str, Any] | None:
    """Report only bindings the host has to act on, and never fail the turn."""

    registry = str(status_payload.get("registry") or "").strip()
    if not agent_id or not registry:
        return None
    from ..heartbeat.automation_upgrade import installed_prompt_binding

    try:
        binding = installed_prompt_binding(
            registry=Path(registry).expanduser(),
            goal_id=goal_id,
            agent_id=agent_id,
            thread_id=str(os.environ.get("CODEX_THREAD_ID") or "").strip(),
        )
    except (OSError, ValueError):
        return None
    return binding if binding.get("status") not in {"absent", "unavailable"} else None
