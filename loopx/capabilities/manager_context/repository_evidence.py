"""Bound explicit repository artifacts to scoped Goals and typed handoff routing."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

from ...history import load_registry
from ...repository_identity import (
    normalize_repository_identity,
    resolve_project_identity,
)
from ...todos import list_goal_todos


SCHEMA_VERSION = "manager_repository_evidence_v0"
ROUTING_ACTION_KIND = "repository_evidence"
_PR_PATH = re.compile(
    r"^/(?P<repository>[A-Za-z0-9._~+/-]+)/pull/(?P<number>[1-9][0-9]*)/?$"
)
_SHORT_PR = re.compile(r"^#?(?P<number>[1-9][0-9]*)$")


def _goal(registry_path: Path, goal_id: str) -> dict[str, Any] | None:
    registry = load_registry(registry_path)
    return next(
        (
            row
            for row in registry.get("goals", [])
            if isinstance(row, dict) and str(row.get("id") or "") == goal_id
        ),
        None,
    )


def _todo_repository(value: Any) -> str | None:
    try:
        identity = normalize_repository_identity(str(value or ""))
    except ValueError:
        return None
    return identity if identity.startswith("git:") else None


def _repository_bindings(
    *,
    registry_path: Path,
    runtime_root: Path,
    goal_id: str,
) -> tuple[list[str], bool]:
    try:
        goal = _goal(registry_path, goal_id)
    except (OSError, ValueError, KeyError, TypeError):
        return [], False
    if goal is None:
        return [], False
    repositories: set[str] = set()
    project = str(goal.get("repo") or "").strip()
    if project:
        try:
            identity = resolve_project_identity(
                project,
                loopx_project_id=goal_id,
            )
            if identity.startswith("git:"):
                repositories.add(identity)
        except ValueError:
            pass

    try:
        result = list_goal_todos(
            registry_path=registry_path,
            runtime_root_arg=str(runtime_root),
            goal_id=goal_id,
        )
        if result.get("ok") is not True or result.get("state_event_projection_warning"):
            raise ValueError("Todo authority unavailable or conflicting")
        for todo in result.get("todos", []):
            if not isinstance(todo, dict):
                continue
            repository = _todo_repository(todo.get("task_repository"))
            if repository is None:
                continue
            repositories.add(repository)
        todo_authority_available = True
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        todo_authority_available = False
    return sorted(repositories), todo_authority_available


def _artifact_identity(
    artifact_ref: str,
    repository_id: str | None,
) -> tuple[str | None, int] | None:
    short = _SHORT_PR.fullmatch(artifact_ref)
    if short:
        return repository_id, int(short.group("number"))
    parsed = urlsplit(artifact_ref)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    path = _PR_PATH.fullmatch(parsed.path)
    if path is None:
        return None
    try:
        artifact_repository = normalize_repository_identity(
            f"https://{parsed.netloc}/{path.group('repository')}"
        )
    except ValueError:
        return None
    if repository_id is not None and artifact_repository != repository_id:
        return None
    return artifact_repository, int(path.group("number"))


def _routing(
    *,
    context_delegation: Any,
    goal_id: str,
) -> dict[str, Any]:
    delegation = context_delegation if isinstance(context_delegation, dict) else {}
    targets = {
        (str(row.get("goal_id") or ""), str(row.get("agent_id") or ""))
        for row in delegation.get("targets", [])
        if isinstance(row, dict)
    }
    allowed_agents = {
        agent_id for target_goal, agent_id in targets if target_goal == goal_id
    }
    matched_agents: list[str] = []
    for profile in delegation.get("routing_profiles", []):
        if not isinstance(profile, dict) or profile.get("goal_id") != goal_id:
            continue
        agent_id = str(profile.get("agent_id") or "")
        if agent_id not in allowed_agents:
            continue
        avoided = profile.get("avoid_action_kinds")
        if isinstance(avoided, list) and any(
            fnmatchcase(ROUTING_ACTION_KIND, str(pattern)) for pattern in avoided
        ):
            continue
        preferred = profile.get("preferred_action_kinds")
        if isinstance(preferred, list) and any(
            fnmatchcase(ROUTING_ACTION_KIND, str(pattern)) for pattern in preferred
        ):
            matched_agents.append(agent_id)
    matched_agents = sorted(set(matched_agents))
    if len(matched_agents) == 1:
        return {
            "status": "matched",
            "action_kind": ROUTING_ACTION_KIND,
            "matching_basis": "agent_profile.preferred_action_kinds",
            "recommended_handoff": {
                "goal_id": goal_id,
                "agent_id": matched_agents[0],
            },
        }
    return {
        "status": (
            "ambiguous_capability_match"
            if len(matched_agents) > 1
            else "no_capability_matched_agent"
        ),
        "action_kind": ROUTING_ACTION_KIND,
        "matching_basis": "agent_profile.preferred_action_kinds",
        "matched_agent_ids": matched_agents,
        "recommended_handoff": None,
        "sole_candidate_fallback_used": False,
    }


def inspect_repository_artifact(
    *,
    registry_path: Path,
    runtime_root: Path,
    goal_id: str,
    artifact_ref: str,
    repository_id: str | None,
    context_delegation: Any,
) -> dict[str, Any]:
    """Return reviewed Core evidence or a typed, capability-routed evidence gap.

    This v0 slice deliberately performs no network or arbitrary checkout read. It
    establishes the stable artifact/repository/routing contract so an unavailable
    artifact is never converted into an unsupported factual answer.
    """

    if repository_id is not None:
        try:
            repository_id = normalize_repository_identity(repository_id)
        except ValueError:
            return {"ok": False, "error": "invalid_repository_identity"}
    artifact = _artifact_identity(artifact_ref, repository_id)
    if artifact is None:
        return {"ok": False, "error": "invalid_or_conflicting_pull_request_ref"}
    artifact_repository, number = artifact
    repositories, todo_authority_available = _repository_bindings(
        registry_path=registry_path,
        runtime_root=runtime_root,
        goal_id=goal_id,
    )
    if artifact_repository is None:
        if len(repositories) == 1:
            artifact_repository = repositories[0]
        else:
            return {
                "ok": True,
                "schema_version": SCHEMA_VERSION,
                "view": "repository_artifact",
                "goal_id": goal_id,
                "unknown": True,
                "evidence": {
                    "status": "unavailable",
                    "reason_code": "repository_identity_required",
                    "artifact_read_status": "not_read",
                },
                "available_repository_ids": repositories,
                "routing": {
                    "status": "repository_identity_required",
                    "recommended_handoff": None,
                    "sole_candidate_fallback_used": False,
                },
                "source": {
                    "source": SCHEMA_VERSION,
                    "external_read_performed": False,
                    "arbitrary_path_read_performed": False,
                    "todo_authority_available": todo_authority_available,
                },
            }
    if artifact_repository not in repositories:
        return {
            "ok": False,
            "error": "repository_outside_available_goal_scope",
            "available_repository_ids": repositories,
        }
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "view": "repository_artifact",
        "goal_id": goal_id,
        "artifact": {
            "kind": "pull_request",
            "repository_id": artifact_repository,
            "number": number,
            "canonical_ref": f"{artifact_repository}#pull/{number}",
        },
        "unknown": True,
        "evidence": {
            "status": "unavailable",
            "reason_code": "repository_artifact_not_available_in_core",
            "artifact_read_status": "not_read",
            "reviewed_artifact": False,
            "claim_policy": "do_not_infer_artifact_facts",
        },
        "routing": _routing(
            context_delegation=context_delegation,
            goal_id=goal_id,
        ),
        "source": {
            "source": SCHEMA_VERSION,
            "repository_binding": "goal_repository_or_core_todo_task_repository",
            "external_read_performed": False,
            "arbitrary_path_read_performed": False,
            "todo_authority_available": todo_authority_available,
        },
    }
