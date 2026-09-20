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
OLD_ADDRESS_RE = re.compile(
    r'github\.com/huangruiteng/loopx((?:/[^\s"<>)\],]*)?)'
)
# A surface is live by where it is, never by what else its text happens to
# contain: it hands an address to a user, a host or another tool at run time,
# or it is the command someone copies.
LIVE_SURFACE_PREFIXES = ("loopx/", "scripts/", ".github/workflows/", "packages/")
# A built bundle is regenerated, not edited, so its baked-in address is fixed by
# the release that rebuilds it. This is the tracked-build-output cost #4677 names.
GENERATED_ASSET_PREFIXES = ("loopx/web/chat/assets/",)
# Where the pre-transfer address is the reviewed-correct content, by path and by
# use: this project's own disambiguation terms must keep matching the archived
# address, and prose may cite the pull request an event happened under.
REVIEWED_ADDRESS_EXCEPTIONS: dict[str, frozenset[str]] = {
    "packages/loopx-community-discussion/src/loopx_community_discussion/normalize.py":
        frozenset({"repository", "issue"}),
    "packages/loopx-community-discussion/smoke/community_discussion_smoke.py":
        frozenset({"issue"}),
    "loopx/capabilities/issue_fix/README.md": frozenset({"pull"}),
    "loopx/capabilities/issue_fix/README.zh-CN.md": frozenset({"pull"}),
    "packages/loopx-codex-provider-routing/RUNBOOK.md": frozenset({"pull"}),
}
DISAMBIGUATION_TERMS_SOURCE = (
    "packages/loopx-community-discussion/src/loopx_community_discussion/normalize.py"
)


def _is_live_surface(name: str) -> bool:
    if name.startswith(GENERATED_ASSET_PREFIXES):
        return False
    return name.startswith(LIVE_SURFACE_PREFIXES)


def _address_use(raw_path: str) -> str:
    """Classify one old-address occurrence by the path that follows it.

    Only the occurrence itself decides the use, so unrelated text in the same
    file cannot turn an install command into a citation or the reverse.
    """

    segments = [part for part in raw_path.strip("/").split("/") if part]
    if not segments:
        return "repository"
    lead = segments[0]
    if lead == "issues":
        return "issue" if len(segments) > 1 and segments[1].isdigit() else "issue_form"
    if lead == "releases":
        return "release_asset"
    if lead == "discussions":
        return "discussion"
    if lead == "tree":
        return "main_pointer" if len(segments) > 1 and segments[1] == "main" else "branch"
    if lead.startswith("."):
        return "repository"
    return {"pull": "pull", "commit": "commit", "blob": "main_pointer"}.get(
        lead, lead
    )


# A use is either a live pointer this project must own or a dated citation that
# may keep the address the event happened under.
LIVE_ADDRESS_USES = frozenset(
    {"repository", "issue_form", "discussion", "release_asset", "main_pointer", "branch"})


def stale_address_uses(name: str, text: str) -> list[str]:
    """Return the old-address uses in a live surface that were never reviewed."""

    tolerated = REVIEWED_ADDRESS_EXCEPTIONS.get(name, frozenset())
    return [
        use
        for use in (
            _address_use(match.group(1) or "")
            for match in OLD_ADDRESS_RE.finditer(text)
        )
        if use not in tolerated
    ]


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
    """Fail when a live surface hands out the pre-transfer repository address.

    GitHub's redirect made the migration silent: a first-run link, a projected
    documentation pointer, an install command or a provider's own relevance
    terms could keep naming the previous owner while everything still resolved.
    Exceptions are per path and per use, so a reviewed citation cannot be
    reclassified by unrelated text in the same file, and a reviewed file cannot
    hide an install command.
    """
    offenders: list[str] = []
    for name in sorted(tracked_files()):
        if not _is_live_surface(name):
            continue
        stale = stale_address_uses(
            name, (REPO_ROOT / name).read_text(encoding="utf-8", errors="replace")
        )
        if stale:
            offenders.append(f"{name} ({', '.join(sorted(set(stale)))})")
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
    _validate_stale_address_classifier()


def _validate_stale_address_classifier() -> None:
    """Prove the classifier keys on path and use, not on surrounding prose."""

    install = (
        "curl -L https://github.com/huangruiteng/loopx/releases/download/"
        "pkg-v1/pkg.tgz -o pkg.tgz\n"
    )
    if stale_address_uses("packages/dsh-loopx-plugin/README.md", install) != [
        "release_asset"
    ]:
        raise AssertionError(
            "an install command under a package README must be named as a live "
            "pre-transfer address"
        )
    cited = (
        "See the README notes at #12 (https://github.com/huangruiteng/loopx/pull/12)\n"
    )
    if stale_address_uses("packages/loopx-codex-provider-routing/RUNBOOK.md", cited):
        raise AssertionError(
            "a reviewed pull-request citation must stay tolerated even where the "
            "same file mentions a README"
        )
    if stale_address_uses("loopx/configuration_catalog.py", cited) != ["pull"]:
        raise AssertionError(
            "a reviewed exception for one path must not tolerate the same shape "
            "elsewhere: a pull citation in a product module is still a live address"
        )
    pointer = "https://github.com/huangruiteng/loopx/blob/main/docs/x.md\n"
    if stale_address_uses("packages/loopx-community-discussion/README.md", pointer) != [
        "main_pointer"
    ]:
        raise AssertionError("a documentation pointer must be named as a live address")
    if _is_live_surface("loopx/web/chat/assets/index-abc123.js"):
        raise AssertionError(
            "a generated bundle is outside the guard: its address is fixed by the "
            "release that rebuilds it, not by hand-editing minified output"
        )
    if not _is_live_surface("packages/dsh-loopx-plugin/README.md"):
        raise AssertionError(
            "an install command under a package README is a live surface"
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
