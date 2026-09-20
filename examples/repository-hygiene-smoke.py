#!/usr/bin/env python3
"""Thin repository-hygiene smoke for LoopX's own public checkout."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from loopx.contract import scan_public_boundary  # noqa: E402


REQUIRED_TRACKED_FILES = (
    "LICENSE",
    "CONTRIBUTING.md",
)
SECURITY_FILES = ("SECURITY.md", ".github/SECURITY.md")
ISSUE_TEMPLATE_DIR = ".github/ISSUE_TEMPLATE/"
PR_TEMPLATE = ".github/PULL_REQUEST_TEMPLATE.md"
RELEASE_TIMELINE = REPO_ROOT / "docs" / "product" / "release-readiness.md"
FIRST_PUBLIC_RELEASE = (0, 1, 3)
VERSION_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

CANONICAL_REPO = "loopx-project/loopx"
PRE_TRANSFER_REPO_URL = "github.com/huangruiteng/loopx"
# Surfaces that hand an address to a user, a host or another tool at run time.
LIVE_CODE_PREFIXES = ("loopx/", "scripts/", ".github/workflows/")
LIVE_PACKAGE_MARKERS = ("/src/", "/package.json", "/examples/request.json", "/smoke/")
DOCUMENT_SUFFIXES = (".md", ".html", ".txt")
# Prose may cite the address an event happened under; a code or config default
# may not, because it is what a shipped release keeps handing out.
HISTORICAL_CITATION_RE = re.compile(
    r"^/(pull|issues|commit|releases/download|archive)/[0-9A-Za-z]"
)
# The provider's project-disambiguation list and its smoke are the declared
# exceptions: both must keep matching archived pages that cite the old address.
DISAMBIGUATION_SOURCES = (
    "packages/loopx-community-discussion/src/loopx_community_discussion/normalize.py",
    "packages/loopx-community-discussion/smoke/community_discussion_smoke.py",
)
DISAMBIGUATION_TERMS_SOURCE = DISAMBIGUATION_SOURCES[0]
# The packaged chat bundle is a build product, so a stale address inside it is
# fixed by rebuilding rather than by hand-editing minified output.
GENERATED_ASSET_PREFIXES = ("loopx/web/chat/assets/",)


def _is_checked_surface(name: str) -> bool:
    if name in DISAMBIGUATION_SOURCES:
        return False
    if name.startswith(GENERATED_ASSET_PREFIXES):
        return False
    if name.startswith(LIVE_CODE_PREFIXES):
        return True
    return name.startswith("packages/") and any(
        marker in name for marker in LIVE_PACKAGE_MARKERS
    )


def _stale_live_pointer(text: str) -> str | None:
    """Return the first old-address use that is a live pointer, not a citation."""

    prose = text.endswith(DOCUMENT_SUFFIXES) or "/README" in text
    for match in re.finditer(re.escape(PRE_TRANSFER_REPO_URL), text):
        tail = text[match.end() : match.end() + 32]
        if prose and HISTORICAL_CITATION_RE.match(tail):
            continue
        return tail.split("\n", 1)[0][:40]
    return None


def tracked_files() -> set[str]:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "repository-hygiene-smoke requires a git worktree: "
            f"{completed.stderr.strip() or 'git ls-files failed'}"
        )
    return {line for line in completed.stdout.split("\0") if line}


def validate_required_tracked_files(files: set[str]) -> None:
    missing = [name for name in REQUIRED_TRACKED_FILES if name not in files]
    if missing:
        raise AssertionError(f"missing tracked repository-hygiene files: {sorted(missing)}")
    if not any(name in files for name in SECURITY_FILES):
        raise AssertionError(
            "missing tracked security policy; expected one of "
            + ", ".join(SECURITY_FILES)
        )
    if PR_TEMPLATE not in files:
        raise AssertionError(f"missing tracked pull-request template: {PR_TEMPLATE}")
    issue_templates = [
        name
        for name in files
        if name.startswith(ISSUE_TEMPLATE_DIR)
        and name != f"{ISSUE_TEMPLATE_DIR}config.yml"
    ]
    if not issue_templates:
        raise AssertionError(f"missing tracked issue templates under {ISSUE_TEMPLATE_DIR}")


def validate_public_private_boundary() -> None:
    boundary = scan_public_boundary([REPO_ROOT], registry={})
    hits = list(boundary.get("hits") or [])
    if hits:
        detail = "\n".join(hits)
        raise AssertionError(f"public/private boundary violations:\n{detail}")
    if not boundary.get("ok"):
        raise AssertionError(
            f"public/private boundary scan failed: {boundary.get('ok')}"
        )


def release_tags() -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "tag", "--list", "--sort=version:refname", "v*"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"cannot list git tags: {completed.stderr.strip()}")
    tags: list[str] = []
    for tag in completed.stdout.splitlines():
        match = VERSION_TAG_RE.match(tag.strip())
        if not match:
            continue
        version = tuple(int(part) for part in match.groups())
        if version >= FIRST_PUBLIC_RELEASE:
            tags.append(tag.strip())
    return tags



def validate_canonical_repository_pointer() -> None:
    """Fail when a live surface still hands out the pre-transfer address.

    The organization migration left GitHub redirecting the old URL, so nothing
    fails loudly: a shipped first-run link, a projected documentation pointer or
    a code default could keep naming the previous owner indefinitely. Prose may
    still cite the address an event happened under, and the disambiguation list
    must match both, so those are the declared exceptions rather than a widening
    allowlist.
    """
    offenders: list[str] = []
    for name in sorted(tracked_files()):
        if not _is_checked_surface(name):
            continue
        stale = _stale_live_pointer(
            (REPO_ROOT / name).read_text(encoding="utf-8", errors="replace")
        )
        if stale is not None:
            offenders.append(f"{name} ({stale})")
    if offenders:
        raise AssertionError(
            f"live surfaces must name the canonical {CANONICAL_REPO}; "
            f"{PRE_TRANSFER_REPO_URL} still appears in: {offenders}"
        )
    terms = (REPO_ROOT / DISAMBIGUATION_TERMS_SOURCE).read_text(encoding="utf-8")
    for address in (CANONICAL_REPO, PRE_TRANSFER_REPO_URL.removeprefix("github.com/")):
        if f"github.com/{address}" not in terms:
            raise AssertionError(
                f"project disambiguation terms dropped {address}; current and archived "
                "pages must both classify as this project"
            )


def validate_release_timeline() -> None:
    if not RELEASE_TIMELINE.is_file():
        raise AssertionError(f"missing release timeline: {RELEASE_TIMELINE.relative_to(REPO_ROOT)}")
    timeline = RELEASE_TIMELINE.read_text(encoding="utf-8")
    tags = release_tags()
    if not tags:
        if "on 20" not in timeline:
            raise AssertionError("release timeline has no dated version entries")
        return
    missing = [tag for tag in tags if f"`{tag}`" not in timeline]
    if missing:
        raise AssertionError(
            "release timeline is missing version entries: "
            + ", ".join(missing)
        )


def main() -> int:
    files = tracked_files()
    validate_required_tracked_files(files)
    validate_public_private_boundary()
    validate_canonical_repository_pointer()
    validate_release_timeline()
    print("repository-hygiene-smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
