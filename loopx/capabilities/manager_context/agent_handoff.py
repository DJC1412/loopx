"""Durable same-Goal agent handoffs over the manager-context inbox."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
import shlex
from typing import Any

from ...agent_registry import registered_agent_ids_for_goal
from ...file_lock import exclusive_file_lock
from ...history import load_registry
from ...todos import list_goal_todos
from . import _hash, _read, _root, _write

HANDOFF_ENTRY_SCHEMA = "loopx_agent_handoff_inbox_entry_v0"
HANDOFF_RECEIPT_SCHEMA = "loopx_agent_handoff_dispatch_receipt_v0"
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
_TODO_RE = re.compile(r"todo_[A-Za-z0-9_-]+")


def _require_token(value: Any, label: str, *, todo: bool = False) -> str:
    normalized = str(value or "").strip()
    pattern = _TODO_RE if todo else _TOKEN_RE
    if not pattern.fullmatch(normalized):
        raise ValueError(f"invalid {label}")
    return normalized


def _goal_agents(registry_path: Path, goal_id: str) -> list[str]:
    registry = load_registry(registry_path)
    goal = next(
        (
            item
            for item in registry.get("goals", [])
            if isinstance(item, dict) and item.get("id") == goal_id
        ),
        None,
    )
    if goal is None:
        raise ValueError("handoff Goal is not registered")
    return registered_agent_ids_for_goal(goal)


def _receipt_path(runtime_root: Path, dispatch_id: str) -> Path:
    return _root(runtime_root) / "agent-handoffs" / "receipts" / f"{dispatch_id}.json"


def _entry_path(runtime_root: Path, target: dict[str, str], dispatch_id: str) -> Path:
    return (
        _root(runtime_root)
        / "agent-handoffs"
        / "entries"
        / _hash(target)
        / f"{dispatch_id}.json"
    )


def dispatch_from_quota_decision(
    runtime_root: Path,
    registry_path: Path,
    *,
    goal_id: str,
    from_agent_id: str,
    decision: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Dispatch the first typed handoff candidate to a deterministic peer."""
    frontier = decision.get("agent_scope_frontier")
    if (
        not isinstance(frontier, Mapping)
        or frontier.get("handoff_dispatch_required") is not True
    ):
        return None
    goal_id = _require_token(goal_id, "goal id")
    from_agent_id = _require_token(from_agent_id, "source agent id")
    registered = _goal_agents(registry_path, goal_id)
    if from_agent_id not in registered:
        raise ValueError("handoff source agent is not registered for the Goal")

    raw_candidates = frontier.get("executor_excluded_dispatchable_items")
    candidates = raw_candidates if isinstance(raw_candidates, list) else []
    candidate = next((item for item in candidates if isinstance(item, Mapping)), None)
    if candidate is None:
        raise ValueError("handoff dispatch frontier has no typed candidate")
    todo_id = _require_token(candidate.get("todo_id"), "handoff todo id", todo=True)
    if candidate.get("continuation_policy") != "independent_handoff":
        raise ValueError("handoff candidate must use independent_handoff")

    raw_peers = candidate.get("eligible_peer_ids")
    if not isinstance(raw_peers, list):
        raw_peers = frontier.get("eligible_peer_ids")
    peers = raw_peers if isinstance(raw_peers, list) else []
    eligible = sorted(
        {
            _require_token(peer, "eligible peer id")
            for peer in peers
            if str(peer or "").strip()
        }
        & set(registered) - {from_agent_id}
    )
    if not eligible:
        raise ValueError("handoff dispatch frontier has no eligible registered peer")
    to_agent_id = eligible[0]
    target = {"goal_id": goal_id, "agent_id": to_agent_id}
    identity = {
        "goal_id": goal_id,
        "todo_id": todo_id,
        "from_agent_id": from_agent_id,
        "to_agent_id": to_agent_id,
    }
    dispatch_id = _hash(identity)
    entry_path = _entry_path(runtime_root, target, dispatch_id)
    receipt_path = _receipt_path(runtime_root, dispatch_id)
    with exclusive_file_lock(receipt_path.with_suffix(".lock")):
        if receipt_path.exists():
            current = _read(receipt_path)
            if any(current.get(key) != value for key, value in identity.items()):
                raise ValueError("agent handoff dispatch identity conflict")
            if not entry_path.exists():
                raise ValueError("agent handoff dispatch entry is missing")
            entry = _read(entry_path)
            if any(entry.get(key) != value for key, value in identity.items()):
                raise ValueError("agent handoff entry identity conflict")
            return {
                key: current.get(key)
                for key in (
                    "schema_version",
                    "dispatch_id",
                    "goal_id",
                    "todo_id",
                    "from_agent_id",
                    "to_agent_id",
                    "status",
                )
            } | {"replayed": True}

    todo_read = list_goal_todos(
        registry_path=registry_path,
        goal_id=goal_id,
        role="agent",
        todo_id=todo_id,
        runtime_root_arg=str(runtime_root),
    )
    todos = todo_read.get("todos", [])
    canonical_todo = next((item for item in todos if isinstance(item, dict)), None)
    if (
        not isinstance(todo_read.get("authority_read"), Mapping)
        or canonical_todo is None
    ):
        raise ValueError("agent handoff requires a readable canonical Todo authority")
    if (
        canonical_todo.get("status") != "open"
        or canonical_todo.get("claimed_by")
        or canonical_todo.get("continuation_policy") != "independent_handoff"
        or from_agent_id not in (canonical_todo.get("excluded_agents") or [])
        or to_agent_id in (canonical_todo.get("excluded_agents") or [])
    ):
        raise ValueError(
            "agent handoff candidate no longer matches canonical Todo eligibility"
        )
    claim_operation_id = f"agent-handoff-{dispatch_id[:32]}"
    claim_command = shlex.join(
        [
            "loopx",
            "--registry",
            str(registry_path),
            "--runtime-root",
            str(runtime_root),
            "todo",
            "claim",
            "--goal-id",
            goal_id,
            "--todo-id",
            todo_id,
            "--claimed-by",
            to_agent_id,
            "--agent-id",
            to_agent_id,
            "--role",
            "agent",
            "--claim-operation-id",
            claim_operation_id,
        ]
    )
    acknowledge_command = shlex.join(
        [
            "loopx",
            "--registry",
            str(registry_path),
            "--runtime-root",
            str(runtime_root),
            "manager-inbox",
            "acknowledge-handoff",
            "--goal-id",
            goal_id,
            "--agent-id",
            to_agent_id,
            "--request-id",
            dispatch_id,
        ]
    )
    entry = {
        "schema_version": HANDOFF_ENTRY_SCHEMA,
        "inbox_kind": "agent_handoff",
        "dispatch_id": dispatch_id,
        **identity,
        "claim_operation_id": claim_operation_id,
        "claim_command": claim_command,
        "acknowledge_command": acknowledge_command,
        "instruction": (
            "Claim the canonical same-Goal Todo with claim_command before work, then run "
            "acknowledge_command so the sender receives durable claim readback. This handoff "
            "grants no repository, provider, trading, payment, publishing, or other protected-operation authority."
        ),
    }
    receipt = {
        "schema_version": HANDOFF_RECEIPT_SCHEMA,
        "dispatch_id": dispatch_id,
        **identity,
        "status": "dispatched",
    }
    with exclusive_file_lock(receipt_path.with_suffix(".lock")):
        replayed = receipt_path.exists()
        if replayed:
            current = _read(receipt_path)
            if any(
                current.get(key) != value
                for key, value in receipt.items()
                if key != "status"
            ):
                raise ValueError("agent handoff dispatch identity conflict")
        else:
            _write(entry_path, entry)
            _write(receipt_path, receipt)
        if not entry_path.exists() or _read(entry_path) != entry:
            raise ValueError("agent handoff dispatch readback failed")
        current = _read(receipt_path)
    return {
        key: current.get(key)
        for key in (
            "schema_version",
            "dispatch_id",
            "goal_id",
            "todo_id",
            "from_agent_id",
            "to_agent_id",
            "status",
        )
    } | {"replayed": replayed}


def pending_handoffs(
    runtime_root: Path, goal_id: str, agent_id: str
) -> list[dict[str, Any]]:
    target = {"goal_id": goal_id, "agent_id": agent_id}
    folder = _root(runtime_root) / "agent-handoffs" / "entries" / _hash(target)
    items: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json")):
        item = _read(path)
        if (
            item.get("schema_version") != HANDOFF_ENTRY_SCHEMA
            or item.get("goal_id") != goal_id
            or item.get("to_agent_id") != agent_id
        ):
            raise ValueError("agent handoff inbox scope mismatch")
        receipt = _read(_receipt_path(runtime_root, str(item.get("dispatch_id") or "")))
        if (
            receipt.get("schema_version") != HANDOFF_RECEIPT_SCHEMA
            or receipt.get("dispatch_id") != item.get("dispatch_id")
            or receipt.get("goal_id") != goal_id
            or receipt.get("to_agent_id") != agent_id
        ):
            raise ValueError("agent handoff receipt scope mismatch")
        if receipt.get("status") in {"claimed", "completed", "claim_conflict"}:
            continue
        items.append(item)
        if len(items) == 21:
            break
    return items


def record_handoff_reads(runtime_root: Path, items: list[dict[str, Any]]) -> None:
    """Record inbox visibility without treating delivery as a Todo claim."""
    for item in items:
        if item.get("inbox_kind") != "agent_handoff":
            continue
        dispatch_id = str(item.get("dispatch_id") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", dispatch_id):
            raise ValueError("invalid handoff dispatch id")
        receipt_path = _receipt_path(runtime_root, dispatch_id)
        with exclusive_file_lock(receipt_path.with_suffix(".lock")):
            receipt = _read(receipt_path)
            if (
                receipt.get("schema_version") != HANDOFF_RECEIPT_SCHEMA
                or receipt.get("dispatch_id") != dispatch_id
                or receipt.get("goal_id") != item.get("goal_id")
                or receipt.get("to_agent_id") != item.get("to_agent_id")
            ):
                raise ValueError("agent handoff receipt scope mismatch")
            if receipt.get("status") == "dispatched":
                _write(receipt_path, {**receipt, "status": "read"})


def acknowledge_claim(
    runtime_root: Path,
    registry_path: Path,
    *,
    goal_id: str,
    agent_id: str,
    dispatch_id: str,
) -> dict[str, Any]:
    goal_id = _require_token(goal_id, "goal id")
    agent_id = _require_token(agent_id, "agent id")
    if not re.fullmatch(r"[a-f0-9]{64}", dispatch_id):
        raise ValueError("invalid handoff dispatch id")
    target = {"goal_id": goal_id, "agent_id": agent_id}
    entry = _read(_entry_path(runtime_root, target, dispatch_id))
    if entry.get("goal_id") != goal_id or entry.get("to_agent_id") != agent_id:
        raise ValueError("agent handoff inbox scope mismatch")
    todos = list_goal_todos(
        registry_path=registry_path,
        goal_id=goal_id,
        role="agent",
        todo_id=str(entry.get("todo_id") or ""),
        runtime_root_arg=str(runtime_root),
    ).get("todos", [])
    todo = next((item for item in todos if isinstance(item, dict)), None)
    if todo is None or not todo.get("claimed_by"):
        raise ValueError(
            "canonical Todo must be claimed by the handoff recipient first"
        )
    receipt_path = _receipt_path(runtime_root, dispatch_id)
    with exclusive_file_lock(receipt_path.with_suffix(".lock")):
        receipt = _read(receipt_path)
        if (
            receipt.get("schema_version") != HANDOFF_RECEIPT_SCHEMA
            or receipt.get("dispatch_id") != dispatch_id
            or receipt.get("goal_id") != goal_id
            or receipt.get("to_agent_id") != agent_id
        ):
            raise ValueError("agent handoff receipt scope mismatch")
        if todo.get("claimed_by") != agent_id:
            conflict = {
                **receipt,
                "status": "claim_conflict",
                "observed_claimed_by": todo.get("claimed_by"),
            }
            replayed = receipt.get("status") == "claim_conflict"
            if not replayed:
                _write(receipt_path, conflict)
            return {
                "ok": True,
                "schema_version": HANDOFF_RECEIPT_SCHEMA,
                "dispatch_id": dispatch_id,
                "goal_id": goal_id,
                "todo_id": entry["todo_id"],
                "from_agent_id": entry["from_agent_id"],
                "to_agent_id": agent_id,
                "status": "claim_conflict",
                "canonical_claim_verified": False,
                "observed_claimed_by": todo.get("claimed_by"),
                "reroute_required": True,
                "replayed": replayed,
            }
        replayed = receipt.get("status") == "claimed"
        if not replayed:
            receipt = {**receipt, "status": "claimed"}
            _write(receipt_path, receipt)
    return {
        "ok": True,
        "schema_version": HANDOFF_RECEIPT_SCHEMA,
        "dispatch_id": dispatch_id,
        "goal_id": goal_id,
        "todo_id": entry["todo_id"],
        "from_agent_id": entry["from_agent_id"],
        "to_agent_id": agent_id,
        "status": "claimed",
        "canonical_claim_verified": True,
        "replayed": replayed,
    }
