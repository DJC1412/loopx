"""One executing bounded Turn per Turn lane.

A Turn lane is one agent working one goal. Two executing Turns for the same
lane would run two executors at once: each invokes its own host, writes its own
delivery, and spends its own quota slot, so the lane ends up with two answers to
one bounded question. LoopX therefore admits exactly one *executing* Turn per
lane and refuses the second with a typed, retryable refusal naming the holder.

The fence is two facts, because a kernel lock is only an authority inside one
machine. The executing process holds a kernel lock, so a crashed or killed Turn
releases the lane instead of leaving a stale claim no later Turn can enter. When
the runtime root is shared -- an SSH-driven executor beside a local one, for
example -- a lock the other host cannot see is not a fence, so the same Turn
also holds a durable lane lease under the runtime root: an exclusive create
naming this host and this Turn, refused while it is live, and taken over once it
expires. Previews and other non-executing decisions never take either.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import timedelta
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import socket
from typing import Any

from ...file_lock import lock_holder_path, try_exclusive_file_lock
from ..runtime.time import now_utc as runtime_now_utc
from ..runtime.time import parse_timestamp, utc_isoformat

# Typed refusal for a lane whose single executor is already busy. The reason is
# a fact about this lane, so a caller can retry it unchanged once it clears.
TURN_LANE_IN_FLIGHT = "turn_lane_in_flight"
# The operator-reachable exit: wait for the named Turn to settle, then retry.
REMEDY_WAIT_FOR_IN_FLIGHT_TURN = "wait_for_in_flight_turn"
TURN_LANE_OPERATION = "loopx_turn_lane"
TURN_LANE_DIR_NAME = ".lanes"
TURN_LANE_UNATTRIBUTED_AGENT = "unattributed"
# A durable lane lease exists because a kernel lock is not an authority across
# hosts. Its holder names the host it was taken on, so a second host sharing the
# runtime root refuses it, and it expires so a host that died cannot hold a lane
# forever.
TURN_LANE_LEASE_SCHEMA_VERSION = "turn_lane_lease_v0"
TURN_LANE_LEASE_SUFFIX = ".lease.json"
# Long enough to cover a bounded Turn, short enough that a host which died
# without releasing the lane does not block the lane for the rest of the day.
TURN_LANE_LEASE_TTL_SECONDS = 30 * 60
# Public-safe holder facts. The host is kept as a fingerprint: a refusal has to
# say "another host is running this lane" without publishing which machine it is.
TURN_LANE_LEASE_HOLDER_TEXT_FIELDS = ("agent_id", "operation", "acquired_at")
# Public-safe holder fields only: the lock record also carries a lock id, a
# policy name, and the private lock path, which never leave this process.
TURN_LANE_HOLDER_TEXT_FIELDS = ("agent_id", "operation", "acquired_at")
# A refusal taken here stops before the journal, the host, and quota, so the
# payload reports the same effect shape an executing Turn does -- all false.
TURN_LANE_NO_EFFECTS: dict[str, bool] = {
    "host_invoked": False,
    "state_written": False,
    "quota_spent": False,
    "scheduler_acknowledged": False,
}
_LANE_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
TURN_LANE_EXECUTION_PAYLOAD = Callable[..., dict[str, Any]]
TURN_LANE_TURN_RUNNER = Callable[..., dict[str, Any]]


def turn_lane_agent_id(plan: Mapping[str, Any]) -> str:
    """Return the agent whose lane this Turn executes in.

    The Turn envelope owns the agent identity. A plan without one still gets a
    lane rather than no fence at all, because two unattributed Turns on one goal
    are exactly the overlap this module exists to refuse.
    """

    envelope = plan.get("turn_envelope")
    if isinstance(envelope, Mapping):
        agent_id = str(envelope.get("agent_id") or "").strip()
        if agent_id:
            return agent_id
    return TURN_LANE_UNATTRIBUTED_AGENT


def turn_lane_target(
    *, runtime_root: Path, goal_id: str, plan: Mapping[str, Any]
) -> Path:
    """Return the lock target one lane's executing Turn holds."""

    agent_id = turn_lane_agent_id(plan)
    readable = _LANE_NAME_UNSAFE.sub("_", agent_id)[:64] or TURN_LANE_UNATTRIBUTED_AGENT
    digest = hashlib.sha256(f"{goal_id}\0{agent_id}".encode()).hexdigest()[:12]
    return (
        Path(runtime_root)
        / "goals"
        / goal_id
        / "turns"
        / TURN_LANE_DIR_NAME
        / f"{readable}-{digest}.lane"
    )


@contextmanager
def turn_lane_singleflight(
    *, runtime_root: Path, goal_id: str, plan: Mapping[str, Any]
) -> Iterator[Path | None]:
    """Hold one lane for one executing Turn.

    ``None`` means another process already owns this lane's executor, which the
    caller reports as the typed refusal instead of running a second executor.
    """

    target = turn_lane_target(runtime_root=runtime_root, goal_id=goal_id, plan=plan)
    lease_target = turn_lane_lease_target(target)
    agent_id = turn_lane_agent_id(plan)
    # The durable lease is read first: a lane another host is executing is
    # refused before this process even contends for its own kernel lock.
    if turn_lane_lease_holder(lease_target) is not None:
        yield None
        return
    with try_exclusive_file_lock(
        target,
        agent_id=agent_id,
        operation=TURN_LANE_OPERATION,
    ) as lock_path:
        if lock_path is None:
            yield None
            return
        record = turn_lane_lease_record(
            agent_id=agent_id, turn_instance_id=turn_lane_turn_instance_id(plan)
        )
        if not _claim_turn_lane_lease(lease_target, record):
            # Another host claimed the lane between the read above and this
            # process's kernel lock. The lease is the authority, so this Turn is
            # refused and its own kernel lock is released by the context manager.
            yield None
            return
        try:
            yield lock_path
        finally:
            _release_turn_lane_lease(lease_target, record)


def turn_lane_lease_target(target: Path) -> Path:
    """Return the durable lease path that sits beside one lane's kernel lock."""

    return target.with_name(f"{target.name}{TURN_LANE_LEASE_SUFFIX}")


def turn_lane_host_fingerprint() -> str:
    """Return a public-safe fingerprint of the machine holding a lane lease."""

    try:
        hostname = socket.gethostname()
    except OSError:  # pragma: no cover - a host without a name is still a host
        hostname = ""
    return hashlib.sha256(hostname.encode("utf-8")).hexdigest()[:12]


def turn_lane_turn_instance_id(plan: Mapping[str, Any]) -> str:
    """Return the Turn identity a lease names, taken from the Turn envelope."""

    envelope = plan.get("turn_envelope")
    if isinstance(envelope, Mapping):
        for field in ("turn_instance_id", "turn_id", "run_id"):
            value = str(envelope.get(field) or "").strip()
            if value:
                return value
    return ""


def turn_lane_lease_record(
    *, agent_id: str, turn_instance_id: str = ""
) -> dict[str, Any]:
    """Build this process's durable claim on one lane."""

    acquired = runtime_now_utc()
    return {
        "schema_version": TURN_LANE_LEASE_SCHEMA_VERSION,
        "agent_id": agent_id or TURN_LANE_UNATTRIBUTED_AGENT,
        "operation": TURN_LANE_OPERATION,
        "turn_instance_id": turn_instance_id,
        "acquired_at": utc_isoformat(acquired),
        "expires_at": utc_isoformat(
            acquired + timedelta(seconds=TURN_LANE_LEASE_TTL_SECONDS)
        ),
        "host_fingerprint": turn_lane_host_fingerprint(),
        "pid": os.getpid(),
    }


def _pid_is_alive(pid: int) -> bool | None:
    """Report whether a recorded holder process still exists, when knowable."""

    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; this process is simply not allowed to signal it.
        return True
    except (OSError, AttributeError, ValueError, NotImplementedError):
        # The platform cannot answer. The lease's own expiry is then the only
        # thing that clears it, which the caller already applies.
        return None
    return True


def _turn_lane_lease_is_live(record: Mapping[str, Any]) -> bool:
    """Report whether one lease record still holds its lane."""

    expires_at = record.get("expires_at")
    if not isinstance(expires_at, str) or not expires_at:
        # A record without an expiry is not a claim this module can honor: it
        # would hold the lane forever, so it is treated as already released.
        return False
    try:
        deadline = parse_timestamp(expires_at)
    except (TypeError, ValueError):
        return False
    if deadline is None or runtime_now_utc() >= deadline:
        return False
    if str(record.get("host_fingerprint") or "") != turn_lane_host_fingerprint():
        # A different machine is executing this lane. Only its own expiry can
        # clear the claim, because this process cannot observe its process.
        return True
    pid = record.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return True
    # The same machine: a holder that no longer exists is a crashed Turn, and
    # the lane is free. A live holder is still running, though the kernel lock
    # is what actually keeps two local processes apart.
    return _pid_is_alive(pid) is not False


def turn_lane_lease_readback(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the public-safe identity of a durable lease holder."""

    projection: dict[str, Any] = {}
    for field in TURN_LANE_LEASE_HOLDER_TEXT_FIELDS:
        value = record.get(field)
        if isinstance(value, str) and value:
            projection[field] = value
    pid = record.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool):
        projection["pid"] = pid
    return projection


def turn_lane_lease_holder(target: Path) -> dict[str, Any] | None:
    """Return the readback of the holder of one lane, or ``None`` if it is free."""

    record = _read_turn_lane_lease(target)
    if record is None or not _turn_lane_lease_is_live(record):
        return None
    return turn_lane_lease_readback(record)


def _read_turn_lane_lease(target: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _claim_turn_lane_lease(target: Path, record: Mapping[str, Any]) -> bool:
    """Take one lane's durable lease, replacing a stale record.

    The exclusive create is the claim: two hosts that both find the lane free
    cannot both succeed, so exactly one of them executes. A record that already
    exists is replaced only when it no longer holds the lane (expired, or left
    by a crashed process on this host).
    """

    try:
        handle = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        existing = _read_turn_lane_lease(target)
        if existing is not None and _turn_lane_lease_is_live(existing):
            return False
        _write_turn_lane_lease(target, record)
        return True
    except OSError:
        return False
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(dict(record), stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return True


def _write_turn_lane_lease(target: Path, record: Mapping[str, Any]) -> None:
    """Replace one lane's lease atomically."""

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temp_path.write_text(
        json.dumps(dict(record), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(target)


def _release_turn_lane_lease(target: Path, record: Mapping[str, Any]) -> None:
    """Release this process's lease, leaving any other holder's record alone."""

    existing = _read_turn_lane_lease(target)
    if existing is None:
        return
    if str(existing.get("turn_instance_id") or "") != str(
        record.get("turn_instance_id") or ""
    ) or existing.get("pid") != record.get("pid"):
        return
    try:
        target.unlink()
    except OSError:  # pragma: no cover - already released by a peer
        return


def turn_lane_holder_readback(target: Path) -> dict[str, Any]:
    """Return the public-safe identity of the Turn holding one lane, else ``{}``.

    Only names, a timestamp, and a process id are projected: the holder record's
    private lock path and lock id stay out, so a refusal can say who is running
    without publishing where this machine keeps its runtime state.
    """

    try:
        record = json.loads(lock_holder_path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(record, Mapping):
        return {}
    projection: dict[str, Any] = {}
    for field in TURN_LANE_HOLDER_TEXT_FIELDS:
        value = record.get(field)
        if isinstance(value, str) and value:
            projection[field] = value
    pid = record.get("pid")
    if isinstance(pid, int):
        projection["pid"] = pid
    return projection


def turn_lane_in_flight_record(
    plan: Mapping[str, Any], *, holder: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the fail-closed result record for a lane already executing a Turn.

    The planned host is named as readback, never as an invocation: this refusal
    stops before the journal, the host, and quota, so it must not claim a host
    identity it did not build.
    """

    planned_host = plan.get("host") if isinstance(plan.get("host"), dict) else {}
    return {
        "status": "unavailable",
        "host": {
            "executable": "not_invoked",
            "kind": str(planned_host.get("kind") or ""),
        },
        "reason": TURN_LANE_IN_FLIGHT,
        "remediation": [REMEDY_WAIT_FOR_IN_FLIGHT_TURN],
        **({"in_flight": dict(holder)} if holder else {}),
    }


def turn_lane_in_flight_projection(journal: Mapping[str, Any]) -> dict[str, Any]:
    """Return the ``in_flight`` holder entry of a lane refusal, or nothing.

    The holder identity is this module's own readback, so the entry is projected
    here and the executor only spreads it into the execution payload.
    """

    holder = journal.get("in_flight")
    return {"in_flight": dict(holder)} if isinstance(holder, Mapping) else {}


def single_executor_per_turn_lane(
    execution_payload: TURN_LANE_EXECUTION_PAYLOAD,
) -> Callable[[TURN_LANE_TURN_RUNNER], TURN_LANE_TURN_RUNNER]:
    """Admit one executing Turn per lane and refuse the second with a typed packet.

    A non-executing decision (``execute=False``) takes no fence: it invokes no
    host and spends nothing, so it can always answer. The fence is held across
    the whole executing section, which is why it wraps the entry rather than one
    phase inside it. The refusal is rendered by the caller's own payload builder
    so a lane refusal and a host refusal keep exactly one payload shape.
    """

    def decorate(execute_turn: TURN_LANE_TURN_RUNNER) -> TURN_LANE_TURN_RUNNER:
        @wraps(execute_turn)
        def single_lane_turn(
            plan: Mapping[str, Any], *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            runtime_root = kwargs.get("runtime_root")
            goal_id = str(kwargs.get("goal_id") or "")
            if not kwargs.get("execute") or runtime_root is None or not goal_id:
                return execute_turn(plan, *args, **kwargs)
            root = Path(runtime_root)
            target = turn_lane_target(runtime_root=root, goal_id=goal_id, plan=plan)
            with turn_lane_singleflight(
                runtime_root=root, goal_id=goal_id, plan=plan
            ) as held:
                if held is not None:
                    return execute_turn(plan, *args, **kwargs)
                # Either this machine's kernel lock or another host's durable
                # lease refused the Turn, and the durable lease is the only one
                # a caller on a shared runtime root can actually wait for.
                holder = turn_lane_lease_holder(
                    turn_lane_lease_target(target)
                ) or turn_lane_holder_readback(target)
                return execution_payload(
                    plan,
                    turn_lane_in_flight_record(plan, holder=holder),
                    execute=True,
                    replayed=False,
                    effects=TURN_LANE_NO_EFFECTS,
                )

        return single_lane_turn

    return decorate
