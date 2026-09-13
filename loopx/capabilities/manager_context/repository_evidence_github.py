"""Bounded, read-only GitHub evidence for the manager repository view."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import PurePosixPath
import re
import subprocess
from typing import Any
from urllib.parse import quote


PROVIDER_ID = "github_cli_read_only_v0"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_PAGE_SIZE = 100
_MAX_PROVIDER_BYTES = 5_000_000
_MAX_TEXT_PAGE_CHARS = 16_000


class RepositoryEvidenceError(RuntimeError):
    """A public-safe, typed repository read failure."""

    def __init__(self, reason_code: str, **details: Any) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.details = details


Runner = Callable[..., subprocess.CompletedProcess[str]]
RepositoryEvidenceReader = Callable[..., dict[str, Any]]
_SECTIONS = {
    "overview",
    "files",
    "diff",
    "reviews",
    "issue_comments",
    "review_comments",
    "checks",
    "source_file",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _github_slug(repository_id: str) -> str:
    prefix = "git:github.com/"
    if not repository_id.startswith(prefix):
        raise RepositoryEvidenceError("repository_provider_unsupported")
    slug = repository_id[len(prefix) :]
    if len(slug.split("/")) != 2:
        raise RepositoryEvidenceError("repository_provider_unsupported")
    return slug


def _failure_code(stderr: str) -> str:
    text = stderr.casefold()
    if "rate limit" in text or "secondary rate" in text:
        return "provider_rate_limited"
    if any(
        marker in text
        for marker in (
            "authentication",
            "authenticate",
            "not logged",
            "gh auth login",
            "bad credentials",
        )
    ):
        return "provider_credentials_unavailable"
    if any(
        marker in text
        for marker in (
            "resource not accessible",
            "forbidden",
            "http 403",
            "permission",
        )
    ):
        return "provider_permission_denied"
    if any(
        marker in text
        for marker in (
            "could not resolve host",
            "connection refused",
            "network is unreachable",
            "tls handshake timeout",
        )
    ):
        return "provider_network_unavailable"
    if any(
        marker in text
        for marker in (
            "http 404",
            "not found",
            "could not resolve to a pullrequest",
        )
    ):
        return "artifact_not_found"
    return "provider_read_failed"


def _run_json(
    endpoint: str,
    *,
    fields: Mapping[str, str] | None = None,
    timeout_seconds: int,
    runner: Runner,
) -> Any:
    argv = ["gh", "api", "--method", "GET", endpoint]
    for key, value in (fields or {}).items():
        argv.extend(["-f", f"{key}={value}"])
    try:
        completed = runner(
            argv,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        raise RepositoryEvidenceError("provider_not_installed") from None
    except subprocess.TimeoutExpired:
        raise RepositoryEvidenceError("provider_timeout") from None
    except OSError:
        raise RepositoryEvidenceError("provider_unavailable") from None
    if completed.returncode != 0:
        raise RepositoryEvidenceError(_failure_code(completed.stderr))
    if len(completed.stdout.encode("utf-8")) > _MAX_PROVIDER_BYTES:
        raise RepositoryEvidenceError("provider_response_too_large")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise RepositoryEvidenceError("provider_malformed_response") from None


def _metadata(
    slug: str,
    number: int,
    *,
    timeout_seconds: int,
    runner: Runner,
) -> dict[str, Any]:
    payload = _run_json(
        f"repos/{slug}/pulls/{number}",
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    if not isinstance(payload, dict) or payload.get("number") != number:
        raise RepositoryEvidenceError("provider_malformed_response")
    head = payload.get("head")
    base = payload.get("base")
    head_sha = str(head.get("sha") if isinstance(head, dict) else "")
    base_sha = str(base.get("sha") if isinstance(base, dict) else "")
    if not _SHA.fullmatch(head_sha) or not _SHA.fullmatch(base_sha):
        raise RepositoryEvidenceError("provider_malformed_response")
    return payload


def _bounded_text(value: Any, *, limit: int) -> tuple[str, bool, int]:
    text = str(value or "")
    return text[:limit], len(text) > limit, len(text)


def _page(
    endpoint: str,
    *,
    offset: int,
    limit: int,
    timeout_seconds: int,
    runner: Runner,
    list_key: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    page_number = offset // _PAGE_SIZE + 1
    local_offset = offset % _PAGE_SIZE
    payload = _run_json(
        endpoint,
        fields={"per_page": str(_PAGE_SIZE), "page": str(page_number)},
        timeout_seconds=timeout_seconds,
        runner=runner,
    )
    matched: int | None = None
    if list_key is None:
        raw_rows = payload
    else:
        if not isinstance(payload, dict):
            raise RepositoryEvidenceError("provider_malformed_response")
        raw_rows = payload.get(list_key)
        total = payload.get("total_count")
        if type(total) is int and total >= 0:
            matched = total
    if not isinstance(raw_rows, list) or any(
        not isinstance(row, dict) for row in raw_rows
    ):
        raise RepositoryEvidenceError("provider_malformed_response")
    rows = raw_rows[local_offset : local_offset + limit]
    source_page_complete = len(raw_rows) < _PAGE_SIZE
    consumed = local_offset + len(rows)
    complete = source_page_complete and consumed >= len(raw_rows)
    if matched is None and source_page_complete:
        matched = (page_number - 1) * _PAGE_SIZE + len(raw_rows)
    next_offset = None if complete else offset + len(rows)
    if not rows and not complete:
        next_offset = page_number * _PAGE_SIZE
    return rows, {
        "offset": offset,
        "included": len(rows),
        "matched": matched,
        "complete": complete,
        "next_offset": next_offset,
        "provider_page_size": _PAGE_SIZE,
    }


def _actor(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return str(value.get("login") or "") or None


def _overview(payload: Mapping[str, Any]) -> dict[str, Any]:
    body, truncated, total_chars = _bounded_text(payload.get("body"), limit=12_000)
    labels = [
        str(row.get("name") or "")
        for row in payload.get("labels", [])
        if isinstance(row, dict) and row.get("name")
    ][:50]
    return {
        "title": str(payload.get("title") or ""),
        "url": payload.get("html_url"),
        "body": body,
        "body_coverage": {
            "complete": not truncated,
            "included_chars": len(body),
            "total_chars": total_chars,
        },
        "state": str(payload.get("state") or "").upper(),
        "draft": payload.get("draft") is True,
        "author": _actor(payload.get("user")),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
        "merged_at": payload.get("merged_at"),
        "mergeable": payload.get("mergeable"),
        "mergeable_state": payload.get("mergeable_state"),
        "changed_files": payload.get("changed_files"),
        "additions": payload.get("additions"),
        "deletions": payload.get("deletions"),
        "commit_count": payload.get("commits"),
        "issue_comment_count": payload.get("comments"),
        "review_comment_count": payload.get("review_comments"),
        "labels": labels,
    }


def _list_evidence(
    *,
    slug: str,
    number: int,
    head_sha: str,
    section: str,
    offset: int,
    limit: int,
    timeout_seconds: int,
    runner: Runner,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    endpoint: str
    list_key: str | None = None
    if section in {"files", "diff"}:
        endpoint = f"repos/{slug}/pulls/{number}/files"
    elif section == "reviews":
        endpoint = f"repos/{slug}/pulls/{number}/reviews"
    elif section == "issue_comments":
        endpoint = f"repos/{slug}/issues/{number}/comments"
    elif section == "review_comments":
        endpoint = f"repos/{slug}/pulls/{number}/comments"
    elif section == "checks":
        endpoint = f"repos/{slug}/commits/{head_sha}/check-runs"
        list_key = "check_runs"
    else:
        raise RepositoryEvidenceError("artifact_section_unsupported")
    raw_rows, coverage = _page(
        endpoint,
        offset=offset,
        limit=limit,
        timeout_seconds=timeout_seconds,
        runner=runner,
        list_key=list_key,
    )
    text_limit = min(4_000, max(800, _MAX_TEXT_PAGE_CHARS // max(limit, 1)))
    rows: list[dict[str, Any]] = []
    for row in raw_rows:
        if section in {"files", "diff"}:
            item = {
                "path": row.get("filename"),
                "status": row.get("status"),
                "sha": row.get("sha"),
                "additions": row.get("additions"),
                "deletions": row.get("deletions"),
                "changes": row.get("changes"),
            }
            if section == "diff":
                patch = row.get("patch")
                if isinstance(patch, str):
                    text, truncated, total_chars = _bounded_text(
                        patch, limit=text_limit
                    )
                    item.update(
                        patch=text,
                        patch_coverage={
                            "available": True,
                            "complete": not truncated,
                            "included_chars": len(text),
                            "total_chars": total_chars,
                        },
                    )
                else:
                    item["patch_coverage"] = {
                        "available": False,
                        "complete": False,
                        "reason_code": "provider_patch_unavailable",
                    }
            rows.append(item)
        elif section == "reviews":
            body, truncated, total_chars = _bounded_text(
                row.get("body"), limit=text_limit
            )
            rows.append(
                {
                    "id": row.get("id"),
                    "author": _actor(row.get("user")),
                    "state": row.get("state"),
                    "submitted_at": row.get("submitted_at"),
                    "commit_sha": row.get("commit_id"),
                    "body": body,
                    "body_coverage": {
                        "complete": not truncated,
                        "included_chars": len(body),
                        "total_chars": total_chars,
                    },
                }
            )
        elif section in {"issue_comments", "review_comments"}:
            body, truncated, total_chars = _bounded_text(
                row.get("body"), limit=text_limit
            )
            item = {
                "id": row.get("id"),
                "author": _actor(row.get("user")),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "body": body,
                "body_coverage": {
                    "complete": not truncated,
                    "included_chars": len(body),
                    "total_chars": total_chars,
                },
            }
            if section == "review_comments":
                item.update(
                    path=row.get("path"),
                    line=row.get("line"),
                    side=row.get("side"),
                    commit_sha=row.get("commit_id"),
                    original_commit_sha=row.get("original_commit_id"),
                )
            rows.append(item)
        else:
            rows.append(
                {
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "status": row.get("status"),
                    "conclusion": row.get("conclusion"),
                    "started_at": row.get("started_at"),
                    "completed_at": row.get("completed_at"),
                    "details_url": row.get("details_url"),
                    "head_sha": row.get("head_sha"),
                }
            )
    if section == "diff":
        coverage["content_complete"] = all(
            row.get("patch_coverage", {}).get("complete") is True for row in rows
        )
    return rows, coverage


def _safe_source_path(value: str) -> str:
    if (
        not value
        or len(value) > 500
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise RepositoryEvidenceError("invalid_source_path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RepositoryEvidenceError("invalid_source_path")
    return "/".join(path.parts)


def _source_file(
    *,
    slug: str,
    source_path: str,
    revision: str,
    line_start: int,
    line_limit: int,
    timeout_seconds: int,
    runner: Runner,
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = source_path
    encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
    try:
        payload = _run_json(
            f"repos/{slug}/contents/{encoded_path}",
            fields={"ref": revision},
            timeout_seconds=timeout_seconds,
            runner=runner,
        )
    except RepositoryEvidenceError as exc:
        if exc.reason_code == "artifact_not_found":
            raise RepositoryEvidenceError("source_file_not_found") from None
        raise
    if not isinstance(payload, dict) or payload.get("type") != "file":
        raise RepositoryEvidenceError("source_file_not_found")
    if payload.get("encoding") != "base64" or not isinstance(
        payload.get("content"), str
    ):
        raise RepositoryEvidenceError("source_file_too_large_or_unsupported")
    try:
        encoded = re.sub(r"\s+", "", payload["content"])
        raw = base64.b64decode(encoded, validate=True)
        text = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise RepositoryEvidenceError("source_file_not_utf8") from None
    lines = text.splitlines()
    start_index = line_start - 1
    selected = lines[start_index : start_index + line_limit]
    joined = "\n".join(selected)
    page_truncated = len(joined) > _MAX_TEXT_PAGE_CHARS
    joined = joined[:_MAX_TEXT_PAGE_CHARS]
    consumed_lines = len(selected) if not page_truncated else 0
    if page_truncated:
        running = 0
        consumed_lines = 0
        for line in selected:
            added = len(line) + (1 if consumed_lines else 0)
            if running + added > _MAX_TEXT_PAGE_CHARS:
                break
            running += added
            consumed_lines += 1
        if not consumed_lines and selected:
            consumed_lines = 1
            selected[0] = selected[0][:_MAX_TEXT_PAGE_CHARS]
        joined = "\n".join(selected[:consumed_lines])
    next_line_start = line_start + consumed_lines
    complete = not page_truncated and next_line_start > len(lines)
    coverage: dict[str, Any] = {
        "complete": complete,
        "included_lines": consumed_lines,
        "total_lines": len(lines),
        "next_line_start": (None if complete or page_truncated else next_line_start),
        "content_truncated_within_page": page_truncated,
    }
    if page_truncated:
        coverage["reason_code"] = "source_line_too_long"
    return {
        "path": path,
        "revision": revision,
        "text": joined,
        "line_start": line_start,
        "line_end": line_start + consumed_lines - 1 if consumed_lines else None,
    }, coverage


def read_github_pull_request_evidence(
    *,
    repository_id: str,
    number: int,
    section: str,
    offset: int,
    limit: int,
    expected_head_sha: str | None,
    source_path: str | None,
    source_ref: str,
    source_line_start: int,
    source_line_limit: int,
    timeout_seconds: int = 15,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Read one semantic PR surface without accepting arbitrary commands."""

    if (
        not isinstance(repository_id, str)
        or type(number) is not int
        or number < 1
        or not isinstance(section, str)
        or section not in _SECTIONS
        or type(offset) is not int
        or not 0 <= offset <= 10_000
        or type(limit) is not int
        or not 1 <= limit <= 12
        or not isinstance(source_ref, str)
        or source_ref not in {"head", "base"}
        or type(source_line_start) is not int
        or not 1 <= source_line_start <= 1_000_000
        or type(source_line_limit) is not int
        or not 1 <= source_line_limit <= 200
        or (
            expected_head_sha is not None
            and (
                not isinstance(expected_head_sha, str)
                or not _SHA.fullmatch(expected_head_sha)
            )
        )
        or (section != "source_file" and source_path is not None)
    ):
        raise RepositoryEvidenceError("invalid_provider_request")
    slug = _github_slug(repository_id)
    safe_source_path = None
    if section == "source_file":
        if source_path is None:
            raise RepositoryEvidenceError("source_path_required")
        safe_source_path = _safe_source_path(source_path)
    metadata = _metadata(slug, number, timeout_seconds=timeout_seconds, runner=runner)
    head = metadata["head"]
    base = metadata["base"]
    head_sha = str(head["sha"])
    base_sha = str(base["sha"])
    if expected_head_sha is not None and expected_head_sha != head_sha:
        raise RepositoryEvidenceError(
            "artifact_head_changed",
            expected_head_sha=expected_head_sha,
            current_head_sha=head_sha,
        )
    if section != "overview" and expected_head_sha is None:
        raise RepositoryEvidenceError(
            "expected_head_sha_required", current_head_sha=head_sha
        )
    evidence: dict[str, Any]
    coverage: dict[str, Any]
    if section == "overview":
        evidence = {"overview": _overview(metadata)}
        coverage = {"complete": True, "next_offset": None}
    elif section == "source_file":
        assert safe_source_path is not None
        revision = head_sha if source_ref == "head" else base_sha
        source, coverage = _source_file(
            slug=slug,
            source_path=safe_source_path,
            revision=revision,
            line_start=source_line_start,
            line_limit=source_line_limit,
            timeout_seconds=timeout_seconds,
            runner=runner,
        )
        evidence = {"source_file": source}
    else:
        rows, coverage = _list_evidence(
            slug=slug,
            number=number,
            head_sha=head_sha,
            section=section,
            offset=offset,
            limit=limit,
            timeout_seconds=timeout_seconds,
            runner=runner,
        )
        evidence = {"rows": rows}
    if section != "overview":
        verified = _metadata(
            slug, number, timeout_seconds=timeout_seconds, runner=runner
        )
        current_head_sha = str(verified["head"]["sha"])
        if current_head_sha != head_sha:
            raise RepositoryEvidenceError(
                "artifact_head_changed",
                expected_head_sha=head_sha,
                current_head_sha=current_head_sha,
            )
    return {
        "evidence": evidence,
        "coverage": coverage,
        "artifact_revision": {
            "head_sha": head_sha,
            "base_sha": base_sha,
            "head_ref": head.get("ref"),
            "base_ref": base.get("ref"),
            "updated_at": metadata.get("updated_at"),
        },
        "source": {
            "provider": PROVIDER_ID,
            "retrieved_at": _now(),
            "repository": slug,
            "pull_request_number": number,
            "section": section,
            "exact_head_sha": head_sha,
            "pagination_basis": (
                "exact_pr_head"
                if section in {"files", "diff", "checks", "source_file"}
                else "provider_created_order_at_retrieval"
            ),
            "raw_provider_payload_captured": False,
            "write_capability": False,
            "arbitrary_command_capability": False,
        },
    }
