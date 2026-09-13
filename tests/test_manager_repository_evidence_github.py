import base64
import json
import subprocess

import pytest

from loopx.capabilities.manager_context.repository_evidence_github import (
    RepositoryEvidenceError,
    read_github_pull_request_evidence,
)


HEAD = "a" * 40
BASE = "b" * 40
REPOSITORY = "git:github.com/example/project"


def metadata(*, head=HEAD):
    return {
        "number": 42,
        "title": "Read repository evidence",
        "body": "Why this change exists.",
        "state": "open",
        "draft": False,
        "user": {"login": "author"},
        "head": {"sha": head, "ref": "feature"},
        "base": {"sha": BASE, "ref": "main"},
        "updated_at": "2026-09-13T01:02:03Z",
        "changed_files": 2,
        "additions": 8,
        "deletions": 3,
        "commits": 1,
        "comments": 2,
        "review_comments": 1,
        "labels": [{"name": "manager"}],
    }


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, tuple):
            return subprocess.CompletedProcess(
                argv, response[0], stdout=response[1], stderr=response[2]
            )
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(response), stderr=""
        )


def read(runner, **overrides):
    arguments = {
        "repository_id": REPOSITORY,
        "number": 42,
        "section": "overview",
        "offset": 0,
        "limit": 8,
        "expected_head_sha": None,
        "source_path": None,
        "source_ref": "head",
        "source_line_start": 1,
        "source_line_limit": 120,
        "runner": runner,
    }
    arguments.update(overrides)
    return read_github_pull_request_evidence(**arguments)


def test_overview_uses_one_fixed_read_only_endpoint_and_pins_revision():
    runner = FakeRunner([metadata()])
    result = read(runner)
    assert result["artifact_revision"] == {
        "head_sha": HEAD,
        "base_sha": BASE,
        "head_ref": "feature",
        "base_ref": "main",
        "updated_at": "2026-09-13T01:02:03Z",
    }
    assert result["evidence"]["overview"]["body"] == "Why this change exists."
    assert result["coverage"] == {"complete": True, "next_offset": None}
    assert runner.calls[0][0] == [
        "gh",
        "api",
        "--method",
        "GET",
        "repos/example/project/pulls/42",
    ]
    assert result["source"]["write_capability"] is False
    assert result["source"]["arbitrary_command_capability"] is False


def test_diff_is_head_guarded_paginated_and_discloses_patch_truncation():
    first_page = [
        {
            "filename": f"src/file-{index}.py",
            "status": "modified",
            "sha": f"{index:040x}",
            "additions": 1,
            "deletions": 0,
            "changes": 1,
            "patch": "+" + ("x" * 5000),
        }
        for index in range(100)
    ]
    second_page = [
        {
            "filename": "src/final.py",
            "status": "added",
            "sha": "c" * 40,
            "additions": 1,
            "deletions": 0,
            "changes": 1,
            "patch": "+done",
        }
    ]
    runner = FakeRunner(
        [metadata(), first_page, metadata(), metadata(), second_page, metadata()]
    )
    first = read(
        runner,
        section="diff",
        expected_head_sha=HEAD,
        offset=98,
        limit=5,
    )
    assert [row["path"] for row in first["evidence"]["rows"]] == [
        "src/file-98.py",
        "src/file-99.py",
    ]
    assert first["coverage"]["next_offset"] == 100
    assert first["coverage"]["complete"] is False
    assert first["coverage"]["content_complete"] is False
    assert first["evidence"]["rows"][0]["patch_coverage"]["complete"] is False

    second = read(
        runner,
        section="diff",
        expected_head_sha=HEAD,
        offset=100,
        limit=5,
    )
    assert second["coverage"] | {"content_complete": True} == {
        "offset": 100,
        "included": 1,
        "matched": 101,
        "complete": True,
        "next_offset": None,
        "provider_page_size": 100,
        "content_complete": True,
    }
    assert "page=2" in runner.calls[4][0]


def test_deeper_read_requires_and_rechecks_exact_head():
    missing = FakeRunner([metadata()])
    with pytest.raises(RepositoryEvidenceError) as exc:
        read(missing, section="files")
    assert exc.value.reason_code == "expected_head_sha_required"
    assert exc.value.details["current_head_sha"] == HEAD

    changed = FakeRunner([metadata(head="c" * 40)])
    with pytest.raises(RepositoryEvidenceError) as exc:
        read(changed, section="reviews", expected_head_sha=HEAD)
    assert exc.value.reason_code == "artifact_head_changed"
    assert exc.value.details == {
        "expected_head_sha": HEAD,
        "current_head_sha": "c" * 40,
    }


def test_source_file_is_repository_relative_and_commit_pinned():
    content = base64.b64encode(b"one\ntwo\nthree\nfour\n").decode()
    runner = FakeRunner(
        [
            metadata(),
            {"type": "file", "encoding": "base64", "content": content},
            metadata(),
        ]
    )
    result = read(
        runner,
        section="source_file",
        expected_head_sha=HEAD,
        source_path="src/example.py",
        source_line_start=2,
        source_line_limit=2,
    )
    assert result["evidence"]["source_file"] == {
        "path": "src/example.py",
        "revision": HEAD,
        "text": "two\nthree",
        "line_start": 2,
        "line_end": 3,
    }
    assert result["coverage"]["next_line_start"] == 4
    assert runner.calls[1][0][:5] == [
        "gh",
        "api",
        "--method",
        "GET",
        "repos/example/project/contents/src/example.py",
    ]
    assert f"ref={HEAD}" in runner.calls[1][0]


def test_source_path_cannot_escape_repository_or_trigger_a_path_read():
    runner = FakeRunner([])
    with pytest.raises(RepositoryEvidenceError) as exc:
        read(
            runner,
            section="source_file",
            expected_head_sha=HEAD,
            source_path="../private.txt",
        )
    assert exc.value.reason_code == "invalid_source_path"
    assert not runner.calls


def test_missing_commit_pinned_source_is_distinct_from_missing_pull_request():
    runner = FakeRunner([metadata(), (1, "", "HTTP 404: Not Found")])
    with pytest.raises(RepositoryEvidenceError) as exc:
        read(
            runner,
            section="source_file",
            expected_head_sha=HEAD,
            source_path="src/missing.py",
        )
    assert exc.value.reason_code == "source_file_not_found"


def test_head_change_during_deep_read_discards_mixed_evidence():
    runner = FakeRunner(
        [
            metadata(),
            [],
            metadata(head="c" * 40),
        ]
    )
    with pytest.raises(RepositoryEvidenceError) as exc:
        read(runner, section="files", expected_head_sha=HEAD)
    assert exc.value.reason_code == "artifact_head_changed"
    assert exc.value.details == {
        "expected_head_sha": HEAD,
        "current_head_sha": "c" * 40,
    }


@pytest.mark.parametrize(
    ("section", "payload", "endpoint"),
    [
        (
            "reviews",
            [{"id": 1, "user": {"login": "reviewer"}, "state": "APPROVED"}],
            "repos/example/project/pulls/42/reviews",
        ),
        (
            "issue_comments",
            [{"id": 2, "user": {"login": "commenter"}, "body": "note"}],
            "repos/example/project/issues/42/comments",
        ),
        (
            "review_comments",
            [{"id": 3, "path": "src/a.py", "line": 7, "body": "nit"}],
            "repos/example/project/pulls/42/comments",
        ),
        (
            "checks",
            {"total_count": 1, "check_runs": [{"id": 4, "name": "test"}]},
            f"repos/example/project/commits/{HEAD}/check-runs",
        ),
    ],
)
def test_semantic_sections_use_fixed_endpoints(section, payload, endpoint):
    runner = FakeRunner([metadata(), payload, metadata()])
    result = read(runner, section=section, expected_head_sha=HEAD)
    assert result["evidence"]["rows"]
    assert runner.calls[1][0][4] == endpoint
    assert runner.calls[1][0][5:] == ["-f", "per_page=100", "-f", "page=1"]


def test_large_single_source_line_advances_with_explicit_truncation():
    content = base64.b64encode(("x" * 20_000).encode()).decode()
    runner = FakeRunner(
        [
            metadata(),
            {"type": "file", "encoding": "base64", "content": content},
            metadata(),
        ]
    )
    result = read(
        runner,
        section="source_file",
        expected_head_sha=HEAD,
        source_path="src/long.txt",
        source_line_limit=1,
    )
    assert len(result["evidence"]["source_file"]["text"]) == 16_000
    assert result["coverage"]["content_truncated_within_page"] is True
    assert result["coverage"]["included_lines"] == 1
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["next_line_start"] is None
    assert result["coverage"]["reason_code"] == "source_line_too_long"


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        (
            "HTTP 403: Resource not accessible by integration",
            "provider_permission_denied",
        ),
        ("HTTP 404: Not Found", "artifact_not_found"),
        ("API rate limit exceeded", "provider_rate_limited"),
        ("could not resolve host: api.github.com", "provider_network_unavailable"),
        ("authenticate with gh auth login", "provider_credentials_unavailable"),
    ],
)
def test_provider_failures_are_typed_without_echoing_stderr(stderr, reason):
    runner = FakeRunner([(1, "", stderr)])
    with pytest.raises(RepositoryEvidenceError) as exc:
        read(runner)
    assert exc.value.reason_code == reason
    assert not exc.value.details
