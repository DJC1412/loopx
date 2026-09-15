#!/usr/bin/env python3
"""Prove the managed executor readback and the fail-closed start it drives."""

from __future__ import annotations

import contextlib
import importlib.abc
import io
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from loopx.cli import main as cli_main  # noqa: E402
from loopx.control_plane.turn_driver import executor as turn_executor  # noqa: E402
from loopx.control_plane.turn_driver.host_binding import (  # noqa: E402
    DSH_RUNTIME_UNAVAILABLE,
    EXECUTOR_KIND_INDIVIDUAL,
    EXECUTOR_KIND_MANAGED,
)


GOAL_ID = "loopx-turn-managed-executor-fixture"
AGENT_ID = "codex-managed-executor-fixture"
TODO_ID = "todo_managedexec01"
CREDENTIAL_ENV = "DEEPSEEK_API_KEY"


def _write_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    project = root / "project"
    runtime = root / "runtime"
    workspace = root / "workspace"
    runtime.mkdir(parents=True)
    workspace.mkdir(parents=True)

    state = project / ".codex" / "goals" / GOAL_ID / "ACTIVE_GOAL_STATE.md"
    state.parent.mkdir(parents=True)
    state.write_text(
        "\n".join(
            [
                "---",
                "status: active",
                "updated_at: 2026-01-01T00:00:00+00:00",
                "---",
                "",
                "# LoopX Managed Executor Fixture",
                "",
                "## Next Action",
                "",
                "Run one bounded managed Turn on the planned executor only.",
                "",
                "## Agent Todo",
                "",
                "- [ ] [P1] Run one bounded managed Turn without leaving the planned executor.",
                (
                    f"  <!-- loopx:todo todo_id={TODO_ID} status=open "
                    "task_class=advancement_task action_kind=implement "
                    f"claimed_by={AGENT_ID} priority=P1 -->"
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )
    registry = project / ".loopx" / "registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "common_runtime_root": str(runtime),
                "goals": [
                    {
                        "id": GOAL_ID,
                        "domain": "loopx-turn-public-fixture",
                        "status": "active",
                        "repo": str(project),
                        "state_file": str(state.relative_to(project)),
                        "adapter": {"kind": "fixture_v0", "status": "connected-delivery"},
                        "quota": {"compute": 10.0, "window_hours": 24},
                        "coordination": {
                            "agent_model": "peer_v1",
                            "registered_agents": [AGENT_ID],
                            "agent_profiles": {
                                AGENT_ID: {
                                    "schema_version": "agent_profile_v1",
                                    "profile_role": "fixture",
                                    "scope": "public qualification",
                                }
                            },
                            "write_scope": ["docs/**"],
                        },
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return project, runtime, workspace, registry


class _UnavailableHarnessRuntime(importlib.abc.MetaPathFinder):
    """Make the DeepSeek Harness runtime unimportable for one bounded call."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "deepseek_harness" or fullname.startswith("deepseek_harness."):
            raise ModuleNotFoundError(fullname)
        return None


@contextlib.contextmanager
def _harness_runtime_unavailable() -> Iterator[None]:
    finder = _UnavailableHarnessRuntime()
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)


@contextlib.contextmanager
def _operator_credential(value: str | None) -> Iterator[None]:
    previous = os.environ.get(CREDENTIAL_ENV)
    if value is None:
        os.environ.pop(CREDENTIAL_ENV, None)
    else:
        os.environ[CREDENTIAL_ENV] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(CREDENTIAL_ENV, None)
        else:
            os.environ[CREDENTIAL_ENV] = previous


def _run_cli(argv: list[str]) -> tuple[int, dict[str, Any]]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = cli_main(argv)
    return exit_code, json.loads(output.getvalue())


def _plan_command(registry: Path, runtime: Path, project: Path) -> list[str]:
    return [
        "--registry",
        str(registry),
        "--runtime-root",
        str(runtime),
        "--format",
        "json",
        "turn",
        "plan",
        "--goal-id",
        GOAL_ID,
        "--agent-id",
        AGENT_ID,
        "--scan-root",
        str(project),
    ]


def _run_once_command(registry: Path, runtime: Path, project: Path, workspace: Path) -> list[str]:
    return [
        "--registry",
        str(registry),
        "--runtime-root",
        str(runtime),
        "--format",
        "json",
        "turn",
        "run-once",
        "--goal-id",
        GOAL_ID,
        "--agent-id",
        AGENT_ID,
        "--turn-instance-id",
        "managed-executor-fail-closed",
        "--project",
        str(workspace),
        "--scan-root",
        str(project),
        "--no-global-sync",
        "--execute",
    ]


def _expect_managed_binding(payload: dict[str, Any]) -> dict[str, Any]:
    binding = payload["managed_executor"]
    assert binding["schema_version"] == "managed_executor_binding_v0", binding
    assert binding["executor"] == payload["host"]["kind"], binding
    assert binding["executor_kind"] == EXECUTOR_KIND_MANAGED, binding
    assert binding["credential_env"] == CREDENTIAL_ENV, binding
    assert isinstance(binding["available"], bool), binding
    assert (binding["unavailable_reason"] is None) is binding["available"], binding
    return binding


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="loopx-turn-managed-executor-") as directory:
        root = Path(directory)
        project, runtime, workspace, registry = _write_fixture(root)

        # 1. No operator credential: the default stays an individual CLI host and
        #    the readback makes no launch claim for it.
        with _operator_credential(None):
            exit_code, payload = _run_cli(_plan_command(registry, runtime, project))
        assert exit_code == 0, payload
        assert payload["host"]["kind"] == "codex-cli", payload
        individual = payload["managed_executor"]
        assert individual["executor_kind"] == EXECUTOR_KIND_INDIVIDUAL, individual
        assert individual["available"] is None, individual
        assert individual["unavailable_reason"] is None, individual

        # 2. A configured credential selects the managed executor, and the
        #    readback names the operator environment it is bound to.
        with _operator_credential("sk-fixture-operator"):
            exit_code, payload = _run_cli(_plan_command(registry, runtime, project))
        assert exit_code == 0, payload
        assert payload["host"]["kind"] == "dsh", payload
        _expect_managed_binding(payload)

        # 3. With the runtime genuinely missing, the same plan reports an
        #    unavailable managed executor instead of promising a launch.
        with _operator_credential("sk-fixture-operator"):
            with _harness_runtime_unavailable():
                exit_code, payload = _run_cli(_plan_command(registry, runtime, project))
        assert exit_code == 0, payload
        assert payload["host"]["kind"] == "dsh", payload
        unavailable = _expect_managed_binding(payload)
        assert unavailable["available"] is False, unavailable
        assert unavailable["unavailable_reason"] == DSH_RUNTIME_UNAVAILABLE, unavailable

        # 4. Executing that Turn fails closed: typed status, no host invocation,
        #    no journal, and no quota slot spend.
        with _operator_credential("sk-fixture-operator"):
            with _harness_runtime_unavailable():
                exit_code, refusal = _run_cli(
                    _run_once_command(registry, runtime, project, workspace)
                )
        assert exit_code == 1, refusal
        assert refusal["ok"] is False, refusal
        assert refusal["status"] == "unavailable", refusal
        assert refusal["reason"] == DSH_RUNTIME_UNAVAILABLE, refusal
        assert refusal["effects"] == {
            "host_invoked": False,
            "state_written": False,
            "quota_spent": False,
            "scheduler_acknowledged": False,
        }, refusal
        assert refusal["quota_slot_spend_count"] == 0, refusal
        journal = turn_executor.turn_journal_path(
            runtime,
            goal_id=GOAL_ID,
            turn_key=str(refusal["resume_turn_key"]),
        )
        assert journal.exists() is False, journal

    print("managed executor binding smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
