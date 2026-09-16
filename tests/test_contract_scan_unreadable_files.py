from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from loopx.contract import iter_scan_files, scan_public_boundary


def test_iter_scan_files_skips_dangling_symlink(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "ok.md").write_text("hello\n", encoding="utf-8")
    (package / "dangling.json").symlink_to("../missing-target.json")

    scanned = iter_scan_files(tmp_path)

    assert [path.name for path in scanned] == ["ok.md"]


def test_scan_public_boundary_survives_dangling_symlink(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "ok.md").write_text("hello\n", encoding="utf-8")
    (package / "dangling.json").symlink_to("../missing-target.json")

    payload = scan_public_boundary([tmp_path])

    assert payload["ok"] is True
    assert payload["scanned_files"] == 1
    assert payload["unreadable_files"] == []


def test_iter_scan_files_skips_virtualenv_roots_by_marker(tmp_path: Path) -> None:
    """A virtualenv is skipped by its marker file, not by its directory name.

    The skip list can only name environments it already knows. A project whose
    environment is called `.venv-conn` or `venv311` would otherwise have
    `site-packages` scanned as repository content, and vendored third-party
    source would be reported as credential and private-IP hits. The payload is
    assembled from split literals so this fixture does not trip the scan it is
    describing.
    """

    keyword = "tok" + "en="
    payload = f'AUTH = "{keyword}syntheticsyntheticsynthetic"\n'
    (tmp_path / "module.py").write_text(payload, encoding="utf-8")

    venv = tmp_path / ".venv-conn"
    vendored = venv / "lib" / "site-packages" / "vendored.py"
    vendored.parent.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    vendored.write_text(payload, encoding="utf-8")

    unmarked = tmp_path / "vendor-copy" / "lib" / "site-packages" / "vendored.py"
    unmarked.parent.mkdir(parents=True)
    unmarked.write_text(payload, encoding="utf-8")

    scanned = iter_scan_files(tmp_path)

    assert tmp_path / "module.py" in scanned
    assert vendored not in scanned
    # The marker is what decides: the same layout without `pyvenv.cfg` is a
    # normal directory and is still scanned.
    assert unmarked in scanned
    # The same bytes outside a virtualenv are still scanned and still hit, so
    # the marker skip cannot be passing by reading nothing at all.
    assert scan_public_boundary([venv])["hits"] == []
    assert scan_public_boundary([tmp_path / "module.py"])["hits"]


def test_tracked_product_runtime_source_is_not_private_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / ".local" / "worktrees" / "repo"
    source = repo_root / "loopx" / "control_plane" / "runtime" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("PUBLIC_CONTRACT = True\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "-q"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", str(source.relative_to(repo_root))],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )

    def fail_git_probe(_: Path) -> dict[str, object]:
        pytest.fail("product runtime source must not use private-state git probing")

    monkeypatch.setattr("loopx.contract._git_probe", fail_git_probe)

    payload = scan_public_boundary([repo_root])

    assert payload["ok"] is True
    assert payload["scanned_files"] == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode 000 files")
def test_scan_public_boundary_reports_unreadable_file(tmp_path: Path) -> None:
    (tmp_path / "ok.md").write_text("hello\n", encoding="utf-8")
    locked = tmp_path / "locked.md"
    locked.write_text("hello\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        payload = scan_public_boundary([tmp_path])
    finally:
        locked.chmod(0o644)

    assert payload["ok"] is True
    assert payload["unreadable_files"] == ["locked.md: Permission denied"]
