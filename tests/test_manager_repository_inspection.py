import argparse
import json

from loopx.capabilities.manager_context import authority
from loopx.capabilities.manager_context.inspection import ManagerInspection, TOOL_NAME
from loopx.capabilities.manager_context.repository_evidence_github import (
    RepositoryEvidenceError,
)


def _readback(title="Bounded reader"):
    return {
        "artifact_revision": {
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "head_ref": "feature",
            "base_ref": "main",
            "updated_at": "2026-09-13T00:00:00Z",
        },
        "evidence": {"overview": {"title": title}},
        "coverage": {"complete": True, "next_offset": None},
        "source": {
            "provider": "fixture_read_only",
            "retrieved_at": "2026-09-13T00:00:01Z",
            "exact_head_sha": "a" * 40,
            "write_capability": False,
            "arbitrary_command_capability": False,
        },
    }


def _inspector(tmp_path, monkeypatch, reader):
    import loopx.capabilities.manager_context.repository_evidence as evidence

    profiles = {
        "research": {
            "schema_version": "agent_profile_v1",
            "agent_id": "research",
            "profile_role": "market research",
            "preferred_action_kinds": ["research_*"],
        },
        "steward": {
            "schema_version": "agent_profile_v1",
            "agent_id": "steward",
            "profile_role": "repository delivery",
            "preferred_action_kinds": ["repository_*"],
        },
    }
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "goals": [
                    {
                        "id": "alpha",
                        "repo": str(tmp_path),
                        "coordination": {
                            "registered_agents": ["research", "steward"],
                            "agent_profiles": profiles,
                        },
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(
        evidence,
        "list_goal_todos",
        lambda **_: {
            "ok": True,
            "todos": [
                {
                    "claimed_by": "research",
                    "task_repository": "git:github.com/example/research",
                },
                {
                    "claimed_by": "steward",
                    "task_repository": "git:github.com/example/loopx",
                },
            ],
        },
    )
    delegation = authority(
        tmp_path,
        registry,
        {"session_id": "manager", "channel_id": "manager"},
        {"client_turn_id": "turn", "origin": "web", "message": "Why #42?"},
    )
    records = []
    return ManagerInspection(
        context={
            "snapshot_id": "fixture",
            "goals": [{"goal_id": "alpha"}],
            "context_delegation": delegation,
        },
        registry_path=registry,
        runtime_root=tmp_path,
        owner_scope=True,
        scope_valid=lambda: True,
        record=records.append,
        repository_reader=reader,
    ), records


def _query(**extra):
    return {
        "view": "repository_artifact",
        "goal_id": "alpha",
        "repository_id": "git:github.com/example/loopx",
        "artifact_ref": "#42",
        **extra,
    }


def test_repository_artifact_reads_before_considering_handoff(monkeypatch, tmp_path):
    calls = []

    def reader(**kwargs):
        calls.append(kwargs)
        return _readback()

    tool, records = _inspector(tmp_path, monkeypatch, reader)
    result = tool.read(TOOL_NAME, _query())
    assert result["ok"] and not result["unknown"]
    assert result["evidence"]["overview"]["title"] == "Bounded reader"
    assert result["artifact"]["head_sha"] == "a" * 40
    assert result["routing"] == {
        "status": "not_needed",
        "recommended_handoff": None,
        "sole_candidate_fallback_used": False,
    }
    assert calls[0]["section"] == "overview"
    assert calls[0]["expected_head_sha"] is None
    assert records == [result]


def test_repository_artifact_projection_is_shared_by_web_and_lark(
    monkeypatch, tmp_path
):
    tool, _ = _inspector(tmp_path, monkeypatch, lambda **_: _readback("Same"))
    web = tool.read(TOOL_NAME, _query())
    tool.owner_scope = False
    lark = tool.read(TOOL_NAME, _query())
    assert web["evidence"] == lark["evidence"]


def test_failed_read_routing_requires_same_repository_responsibility(
    monkeypatch, tmp_path
):
    def unavailable(**_):
        raise RepositoryEvidenceError("provider_timeout")

    tool, _ = _inspector(tmp_path, monkeypatch, unavailable)
    profiles = tool.context["context_delegation"]["routing_profiles"]
    for profile in profiles:
        profile["preferred_action_kinds"] = (
            ["repository_*"] if profile["agent_id"] == "research" else ["other_*"]
        )
    result = tool.read(TOOL_NAME, _query())
    assert result["routing"]["status"] == "no_capability_matched_agent"
    assert result["routing"]["recommended_handoff"] is None
    assert result["routing"]["repository_responsible_agent_ids"] == ["steward"]


def test_repository_artifact_rejects_invalid_section_without_provider_read(
    monkeypatch, tmp_path
):
    calls = []
    tool, _ = _inspector(tmp_path, monkeypatch, lambda **kw: calls.append(kw))
    result = tool.read(TOOL_NAME, _query(artifact_section=[]))
    assert result == {"ok": False, "error": "invalid_arguments"}
    assert not calls


def test_repository_artifact_has_local_cli_projection(monkeypatch, tmp_path):
    from loopx.capabilities.manager_context import evidence_export
    import loopx.capabilities.manager_context.repository_evidence as evidence
    import loopx.capabilities.manager_context.repository_evidence_github as github

    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"goals": [{"id": "alpha", "repo": str(tmp_path)}]}))
    monkeypatch.setattr(
        evidence,
        "list_goal_todos",
        lambda **_: {
            "ok": True,
            "todos": [{"task_repository": "git:github.com/example/loopx"}],
        },
    )
    monkeypatch.setattr(
        github, "read_github_pull_request_evidence", lambda **_: _readback("CLI")
    )
    result = evidence_export.export_page(
        registry,
        str(tmp_path),
        argparse.Namespace(
            portfolio_goal_ids=["alpha"],
            limit=8,
            days=1,
            offset=0,
            include_stopped=False,
            manager_view="repository_artifact",
            repository_id="git:github.com/example/loopx",
            artifact_ref="#42",
            artifact_section="overview",
            expected_head_sha=None,
            source_path=None,
            source_ref="head",
            source_line_start=1,
            source_line_limit=120,
        ),
    )
    assert result["schema_version"] == "manager_evidence_page_v1"
    assert result["evidence"]["overview"]["title"] == "CLI"
