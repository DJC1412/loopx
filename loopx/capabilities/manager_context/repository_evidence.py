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
from .repository_evidence_github import (
    RepositoryEvidenceError,
    RepositoryEvidenceReader,
)


SCHEMA_VERSION = "manager_repository_evidence_v1"
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
) -> tuple[list[str], bool, dict[str, list[str]]]:
    try:
        goal = _goal(registry_path, goal_id)
    except (OSError, ValueError, KeyError, TypeError):
        return [], False, {}
    if goal is None:
        return [], False, {}
    repositories: set[str] = set()
    responsible_agents: dict[str, set[str]] = {}
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
            agent_id = str(todo.get("claimed_by") or "").strip()
            if agent_id:
                responsible_agents.setdefault(repository, set()).add(agent_id)
        todo_authority_available = True
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        todo_authority_available = False
    return (
        sorted(repositories),
        todo_authority_available,
        {
            repository: sorted(agent_ids)
            for repository, agent_ids in sorted(responsible_agents.items())
        },
    )


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
    responsible_agent_ids: list[str],
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
        if agent_id not in allowed_agents or agent_id not in responsible_agent_ids:
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
            "matching_basis": (
                "agent_profile.preferred_action_kinds+todo.task_repository"
            ),
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
        "matching_basis": "agent_profile.preferred_action_kinds+todo.task_repository",
        "matched_agent_ids": matched_agents,
        "repository_responsible_agent_ids": sorted(responsible_agent_ids),
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
    section: str = "overview",
    offset: int = 0,
    limit: int = 8,
    expected_head_sha: str | None = None,
    source_path: str | None = None,
    source_ref: str = "head",
    source_line_start: int = 1,
    source_line_limit: int = 120,
    reader: RepositoryEvidenceReader | None = None,
) -> dict[str, Any]:
    """Return scoped, revision-pinned repository evidence or a typed failure."""

    if repository_id is not None:
        try:
            repository_id = normalize_repository_identity(repository_id)
        except ValueError:
            return {"ok": False, "error": "invalid_repository_identity"}
    artifact = _artifact_identity(artifact_ref, repository_id)
    if artifact is None:
        return {"ok": False, "error": "invalid_or_conflicting_pull_request_ref"}
    artifact_repository, number = artifact
    repositories, todo_authority_available, repository_responsibilities = (
        _repository_bindings(
            registry_path=registry_path,
            runtime_root=runtime_root,
            goal_id=goal_id,
        )
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
    artifact_payload = {
        "kind": "pull_request",
        "repository_id": artifact_repository,
        "number": number,
        "canonical_ref": f"{artifact_repository}#pull/{number}",
    }
    if reader is None:
        from .repository_evidence_github import read_github_pull_request_evidence

        reader = read_github_pull_request_evidence
    try:
        readback = reader(
            repository_id=artifact_repository,
            number=number,
            section=section,
            offset=offset,
            limit=limit,
            expected_head_sha=expected_head_sha,
            source_path=source_path,
            source_ref=source_ref,
            source_line_start=source_line_start,
            source_line_limit=source_line_limit,
        )
    except RepositoryEvidenceError as exc:
        reason_code = exc.reason_code
        details = exc.details
        routing = _routing(
            context_delegation=context_delegation,
            goal_id=goal_id,
            responsible_agent_ids=repository_responsibilities.get(
                artifact_repository, []
            ),
        )
        routing["handoff_policy"] = (
            "explicit_implementation_validation_or_extended_investigation_only"
        )
        return {
            "ok": True,
            "schema_version": SCHEMA_VERSION,
            "view": "repository_artifact",
            "goal_id": goal_id,
            "artifact": artifact_payload,
            "unknown": True,
            "evidence": {
                "status": "unavailable",
                "reason_code": reason_code,
                "artifact_read_status": "failed",
                "source_artifact_read": False,
                "claim_policy": "do_not_infer_artifact_facts",
                **details,
            },
            "routing": routing,
            "source": {
                "source": SCHEMA_VERSION,
                "repository_binding": ("goal_repository_or_core_todo_task_repository"),
                "external_read_attempted": True,
                "provider_contacted": reason_code
                not in {
                    "repository_provider_unsupported", "provider_not_installed",
                    "invalid_provider_request", "invalid_source_path", "source_path_required",
                },
                "external_read_performed": False,
                "arbitrary_path_read_performed": False,
                "todo_authority_available": todo_authority_available,
            },
        }
    if not isinstance(readback, dict):
        return {
            "ok": False,
            "error": "repository_provider_contract_invalid",
        }
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "view": "repository_artifact",
        "goal_id": goal_id,
        "artifact": artifact_payload | readback["artifact_revision"],
        "unknown": False,
        "evidence": {
            "status": "available",
            "artifact_read_status": "read",
            "source_artifact_read": True,
            "section": section,
            **readback["evidence"],
            "coverage": readback["coverage"],
        },
        "routing": {
            "status": "not_needed",
            "recommended_handoff": None,
            "sole_candidate_fallback_used": False,
        },
        "source": {
            "source": SCHEMA_VERSION,
            "repository_binding": "goal_repository_or_core_todo_task_repository",
            "external_read_attempted": True,
            "external_read_performed": True,
            "arbitrary_path_read_performed": False,
            "todo_authority_available": todo_authority_available,
            **readback["source"],
        },
    }
