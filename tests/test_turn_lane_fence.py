"""One executing bounded Turn per Turn lane.

The fence is a process-level lock plus a durable lane lease, so these tests hold
the lane the same way a running Turn does and assert what the second Turn is
told -- locally, and from another host that shares the runtime root and cannot
see this machine's lock. Admission and release after a settled Turn are covered
end to end by the public dsh smokes, which run one Turn and then its replay
through the same entry.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import timedelta

from loopx.control_plane.turn_driver.executor import run_loopx_turn_once
from loopx.control_plane.turn_driver.lane_fence import (
    REMEDY_WAIT_FOR_IN_FLIGHT_TURN,
    TURN_LANE_IN_FLIGHT,
    TURN_LANE_LEASE_SCHEMA_VERSION,
    TURN_LANE_OPERATION,
    turn_lane_holder_readback,
    turn_lane_host_fingerprint,
    turn_lane_lease_record,
    turn_lane_lease_target,
    turn_lane_singleflight,
    turn_lane_target,
)
from loopx.control_plane.runtime.time import now_utc, utc_isoformat

GOAL_ID = "lane-fence-goal"
AGENT_ID = "lane-fence-agent"


def _plan(*, agent_id: str = AGENT_ID) -> dict:
    return {
        "host": {"kind": "dsh", "execution_mode": "isolated-headless"},
        "route": {"kind": "ready_for_host", "would_invoke_host": True},
        "turn_envelope": {"agent_id": agent_id, "goal_id": GOAL_ID},
        "transaction": {"turn_key": "sha256:" + "1" * 64},
    }


def _execute(tmp_path: Path, *, execute: bool = True, plan: dict | None = None) -> dict:
    return run_loopx_turn_once(
        plan or _plan(),
        host_argv=["python3", "-c", "raise SystemExit(0)"],
        project=tmp_path,
        runtime_root=tmp_path / "runtime",
        goal_id=GOAL_ID,
        timeout_seconds=1.0,
        execute=execute,
    )


def test_a_second_executing_turn_refuses_while_one_holds_the_lane(
    tmp_path: Path,
) -> None:
    with turn_lane_singleflight(
        runtime_root=tmp_path / "runtime", goal_id=GOAL_ID, plan=_plan()
    ) as held:
        assert held is not None
        payload = _execute(tmp_path)

    assert payload["ok"] is False
    assert payload["status"] == "unavailable"
    assert payload["reason"] == TURN_LANE_IN_FLIGHT
    assert payload["remediation"] == [REMEDY_WAIT_FOR_IN_FLIGHT_TURN]
    assert payload["effects"] == {
        "host_invoked": False,
        "state_written": False,
        "quota_spent": False,
        "scheduler_acknowledged": False,
    }
    assert payload["quota_slot_spend_count"] == 0
    # The refusal names the holder, so an operator can tell what to wait for.
    assert payload["in_flight"]["agent_id"] == AGENT_ID
    assert payload["in_flight"]["operation"] == TURN_LANE_OPERATION
    assert isinstance(payload["in_flight"]["pid"], int)
    assert payload["in_flight"]["acquired_at"]
    # A refusal is a readback, not an invocation of the planned host.
    assert payload["host"] == {"executable": "not_invoked", "kind": "dsh"}


def test_a_preview_never_takes_the_lane(tmp_path: Path) -> None:
    with turn_lane_singleflight(
        runtime_root=tmp_path / "runtime", goal_id=GOAL_ID, plan=_plan()
    ) as held:
        assert held is not None
        payload = _execute(tmp_path, execute=False)

    assert payload["ok"] is True
    assert payload["status"] == "preview"


def test_the_lane_is_goal_and_agent_scoped(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    same_lane = turn_lane_target(runtime_root=root, goal_id=GOAL_ID, plan=_plan())
    other_agent = turn_lane_target(
        runtime_root=root, goal_id=GOAL_ID, plan=_plan(agent_id="another-agent")
    )
    other_goal = turn_lane_target(
        runtime_root=root, goal_id="another-goal", plan=_plan()
    )

    assert same_lane == turn_lane_target(
        runtime_root=root, goal_id=GOAL_ID, plan=_plan()
    )
    assert len({same_lane, other_agent, other_goal}) == 3
    assert AGENT_ID in same_lane.name
    # A plan without an agent identity still gets a lane instead of no fence.
    unattributed = turn_lane_target(
        runtime_root=root, goal_id=GOAL_ID, plan={"turn_envelope": {}}
    )
    assert unattributed not in {same_lane, other_agent, other_goal}


def test_the_holder_readback_stays_public_safe(tmp_path: Path) -> None:
    target = turn_lane_target(
        runtime_root=tmp_path / "runtime", goal_id=GOAL_ID, plan=_plan()
    )
    with turn_lane_singleflight(
        runtime_root=tmp_path / "runtime", goal_id=GOAL_ID, plan=_plan()
    ):
        holder = turn_lane_holder_readback(target)

    assert holder["agent_id"] == AGENT_ID
    assert holder["operation"] == TURN_LANE_OPERATION
    assert isinstance(holder["pid"], int)
    assert set(holder) == {"agent_id", "operation", "pid", "acquired_at"}
    # The private lock identity and the runtime path never leave the process.
    assert str(tmp_path) not in str(holder)
    assert turn_lane_holder_readback(tmp_path / "absent.lane") == {}


def _write_lease(
    tmp_path: Path,
    *,
    host_fingerprint: str,
    expires_in_seconds: int,
    pid: int | None = None,
) -> Path:
    """Write a durable lane lease the way another host would have left it."""

    target = turn_lane_lease_target(
        turn_lane_target(
            runtime_root=tmp_path / "runtime", goal_id=GOAL_ID, plan=_plan()
        )
    )
    acquired = now_utc()
    record = {
        "schema_version": TURN_LANE_LEASE_SCHEMA_VERSION,
        "agent_id": AGENT_ID,
        "operation": TURN_LANE_OPERATION,
        "turn_instance_id": "another-host-turn",
        "acquired_at": utc_isoformat(acquired),
        "expires_at": utc_isoformat(acquired + timedelta(seconds=expires_in_seconds)),
        "host_fingerprint": host_fingerprint,
        "pid": os.getpid() if pid is None else pid,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def test_a_second_host_sharing_the_runtime_root_is_refused(tmp_path: Path) -> None:
    """A kernel lock cannot fence a host that cannot see it.

    The runtime root is shared -- an SSH-driven executor beside this machine is
    the case that motivated the durable lease -- so the lane's holder is read
    from the runtime root and refused before the journal, the host and quota.
    """

    _write_lease(tmp_path, host_fingerprint="a-different-machine", expires_in_seconds=600)

    payload = _execute(tmp_path)

    assert payload["ok"] is False
    assert payload["status"] == "unavailable"
    assert payload["reason"] == TURN_LANE_IN_FLIGHT
    assert payload["remediation"] == [REMEDY_WAIT_FOR_IN_FLIGHT_TURN]
    # The refusal is a readback of the lease, not an invocation.
    assert payload["host"] == {"executable": "not_invoked", "kind": "dsh"}
    assert payload["effects"] == {
        "host_invoked": False,
        "state_written": False,
        "quota_spent": False,
        "scheduler_acknowledged": False,
    }
    assert payload["quota_slot_spend_count"] == 0
    assert payload["in_flight"]["agent_id"] == AGENT_ID
    assert payload["in_flight"]["operation"] == TURN_LANE_OPERATION
    assert payload["in_flight"]["acquired_at"]
    # Nothing about the other machine leaves the process: no host name, no path.
    assert "a-different-machine" not in json.dumps(payload)
    assert str(tmp_path) not in json.dumps(payload)


def test_an_expired_lease_from_another_host_does_not_hold_the_lane(
    tmp_path: Path,
) -> None:
    """A host that died mid-Turn must not hold its lane forever."""

    lease_target = _write_lease(
        tmp_path, host_fingerprint="a-different-machine", expires_in_seconds=-60
    )

    with turn_lane_singleflight(
        runtime_root=tmp_path / "runtime", goal_id=GOAL_ID, plan=_plan()
    ) as held:
        assert held is not None
        # The stale record was replaced by this Turn's own claim, so the lane is
        # held by a live lease rather than by the host that never released it.
        stored = json.loads(lease_target.read_text(encoding="utf-8"))
        assert stored["host_fingerprint"] == turn_lane_host_fingerprint()
        assert stored["expires_at"] > stored["acquired_at"]

    assert not lease_target.exists()


def test_a_crashed_holder_on_this_host_does_not_hold_the_lane(tmp_path: Path) -> None:
    """A holder on this machine that no longer exists is a finished fence."""

    _write_lease(
        tmp_path,
        host_fingerprint=turn_lane_host_fingerprint(),
        expires_in_seconds=600,
        pid=999_999,
    )

    with turn_lane_singleflight(
        runtime_root=tmp_path / "runtime", goal_id=GOAL_ID, plan=_plan()
    ) as held:
        assert held is not None


def test_a_held_lane_writes_the_lease_the_next_turn_reads(tmp_path: Path) -> None:
    """The holder writes the durable fact the second host decides from."""

    with turn_lane_singleflight(
        runtime_root=tmp_path / "runtime", goal_id=GOAL_ID, plan=_plan()
    ) as held:
        assert held is not None
        record = turn_lane_lease_record(agent_id=AGENT_ID, turn_instance_id="turn-1")
        assert record["schema_version"] == TURN_LANE_LEASE_SCHEMA_VERSION
        lease_target = turn_lane_lease_target(
            turn_lane_target(
                runtime_root=tmp_path / "runtime", goal_id=GOAL_ID, plan=_plan()
            )
        )
        assert lease_target.exists()
        stored = json.loads(lease_target.read_text(encoding="utf-8"))
        assert stored["host_fingerprint"] == turn_lane_host_fingerprint()
        assert stored["pid"] == os.getpid()
        assert stored["agent_id"] == AGENT_ID

    # A settled Turn leaves no claim behind for the next Turn or host.
    assert not lease_target.exists()
    with turn_lane_singleflight(
        runtime_root=tmp_path / "runtime", goal_id=GOAL_ID, plan=_plan()
    ) as again:
        assert again is not None
