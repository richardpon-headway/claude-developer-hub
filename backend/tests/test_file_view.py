"""Tests for ``GET /api/worktree/{repo}/{name}/file`` (plan-46).

Unlike the rest of the suite, these tests exercise real ``git``
subprocess calls inside ``tmp_path``-scoped repos. Mocking the git
output would defeat the point — the diff merge logic is the whole
contribution.

Each test:
  1. Calls ``_init_repo_with_feature_branch`` to build a tiny repo on
     disk with a ``main`` branch, a ``feature`` branch, and an
     ``origin`` remote that points at ``main`` (so ``resolve_base_ref``
     finds ``origin/main`` like it would in real life).
  2. Edits files on the feature branch (committed and/or working-tree).
  3. Seeds a worktree row pointing at the repo's checkout.
  4. Hits the endpoint and asserts on the response shape.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from tests.fixtures.config import write_repo_config
from tests.fixtures.worktree import seed_worktree


def _git(wt_path: Path, *args: str) -> str:
    """Shell ``git -C <wt_path> <args>``. Returns stdout; raises on
    non-zero exit."""
    res = subprocess.run(
        ["git", "-C", str(wt_path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout


def _init_repo_with_feature_branch(
    base_dir: Path,
    *,
    main_files: dict[str, str],
    feature_files: dict[str, str] | None = None,
    working_tree_files: dict[str, str] | None = None,
    untracked_files: dict[str, str] | None = None,
    branch_name: str = "feature",
) -> Path:
    """Build a repo + working checkout in ``base_dir/wt`` with:

    - ``main`` branch holding ``main_files`` (one initial commit).
    - ``branch_name`` checked out, with ``feature_files`` committed on
      top (or no extra commit when ``feature_files`` is None).
    - Optional ``working_tree_files`` applied to the checkout WITHOUT
      committing — simulates the user's in-progress edits.
    - Optional ``untracked_files`` written to disk but never tracked.
    - An ``origin`` remote pointing at a bare clone so
      ``origin/main`` resolves like it would in real life.

    Returns the checkout path (used for ``wt_path`` in tests).
    """
    repo = base_dir / "repo.git"
    wt = base_dir / "wt"
    wt.mkdir(parents=True)

    # Init the checkout as a regular repo (not from a bare clone) so we
    # can commit into main here, then push to a bare repo we make the
    # "origin" remote. This is the simplest way to give resolve_base_ref
    # a real ``origin/main`` ref to find.
    _git(wt, "init", "-q", "-b", "main")
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")

    for path, body in main_files.items():
        full = wt / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(body)
        _git(wt, "add", path)
    _git(wt, "commit", "-q", "-m", "main initial")

    # Bare clone for ``origin``.
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(wt), str(repo)],
        check=True,
        capture_output=True,
    )
    _git(wt, "remote", "add", "origin", str(repo))
    _git(wt, "fetch", "-q", "origin")
    _git(wt, "branch", "--set-upstream-to=origin/main", "main")

    # Branch off and apply feature commits.
    _git(wt, "checkout", "-q", "-b", branch_name)
    if feature_files:
        for path, body in feature_files.items():
            full = wt / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(body)
            _git(wt, "add", path)
        _git(wt, "commit", "-q", "-m", "feature commit")

    # Apply working-tree-only edits.
    if working_tree_files:
        for path, body in working_tree_files.items():
            full = wt / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(body)

    # Untracked files: write but don't ``git add``.
    if untracked_files:
        for path, body in untracked_files.items():
            full = wt / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(body)

    return wt


def _seed_and_configure(
    _isolate: dict[str, Path],
    wt_path: Path,
    *,
    repo: str = "myapp",
    name: str = "feature",
    branch: str = "feature",
) -> None:
    """Common seed: insert a worktree row pointing at ``wt_path``, and
    write a repo config so ``load_config`` finds ``default_branch=main``.
    """
    seed_worktree(
        _isolate["db_path"], repo, name, path=wt_path, branch=branch, mkdir=False
    )
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        wt_path,
        name=repo,
        default_branch="main",
    )


# --- error paths -----------------------------------------------------------


def test_file_view_400_when_path_query_missing(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    """``?path=`` is required. Empty string → 400."""
    wt = _init_repo_with_feature_branch(tmp_path, main_files={"foo.py": "x = 1\n"})
    _seed_and_configure(_isolate, wt)
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/file?path=")
    assert r.status_code == 400


def test_file_view_404_when_worktree_missing(_isolate: dict[str, Path]) -> None:
    with TestClient(app) as client:
        r = client.get("/api/worktree/unknown/missing/file?path=foo.py")
    assert r.status_code == 404


def test_file_view_400_when_worktree_path_missing_on_disk(
    _isolate: dict[str, Path],
) -> None:
    """Worktree row points at a path that no longer exists on disk."""
    seed_worktree(
        _isolate["db_path"], "myapp", "feature",
        path=_isolate["dev_root"] / "ghost",
        mkdir=False,
    )
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/file?path=foo.py")
    assert r.status_code == 400


def test_file_view_400_when_path_traverses_parent(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    wt = _init_repo_with_feature_branch(tmp_path, main_files={"foo.py": "x = 1\n"})
    _seed_and_configure(_isolate, wt)
    with TestClient(app) as client:
        r = client.get(
            "/api/worktree/myapp/feature/file?path=../../../etc/passwd"
        )
    assert r.status_code == 400
    assert "worktree root" in r.json()["detail"]


def test_file_view_400_when_path_is_absolute(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    wt = _init_repo_with_feature_branch(tmp_path, main_files={"foo.py": "x = 1\n"})
    _seed_and_configure(_isolate, wt)
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/file?path=/etc/passwd")
    assert r.status_code == 400


def test_file_view_400_when_symlink_escapes_worktree(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    wt = _init_repo_with_feature_branch(tmp_path, main_files={"foo.py": "x\n"})
    _seed_and_configure(_isolate, wt)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    os.symlink(outside, wt / "leak")
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/file?path=leak")
    assert r.status_code == 400


# --- diff classification ---------------------------------------------------


def test_file_view_unchanged_file_returns_empty_hunks(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    """Feature branch == main, no working-tree edits → no hunks."""
    wt = _init_repo_with_feature_branch(
        tmp_path,
        main_files={"foo.py": "x = 1\n"},
    )
    _seed_and_configure(_isolate, wt)
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/file?path=foo.py")
    assert r.status_code == 200
    body = r.json()
    assert body["hunks"] == []
    assert body["is_binary"] is False
    assert body["is_missing"] is False
    assert body["on_disk_content"] == "x = 1\n"
    assert body["file_in_pr_diff"] is False


def test_file_view_committed_only_changes(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    """Feature branch added a line; no working-tree edits. Should
    produce committed_add hunk(s) only."""
    wt = _init_repo_with_feature_branch(
        tmp_path,
        main_files={"foo.py": "x = 1\n"},
        feature_files={"foo.py": "x = 1\ny = 2\n"},
    )
    _seed_and_configure(_isolate, wt)
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/file?path=foo.py")
    assert r.status_code == 200
    body = r.json()
    assert body["file_in_pr_diff"] is True
    assert len(body["hunks"]) >= 1
    kinds = {ln["kind"] for h in body["hunks"] for ln in h["lines"]}
    assert "committed_add" in kinds
    # No uncommitted classification when working tree matches HEAD.
    assert "uncommitted_add" not in kinds
    assert "uncommitted_remove" not in kinds


def test_file_view_uncommitted_only_changes(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    """Feature branch == main (no commits), working tree has edits.
    Should produce uncommitted hunks only."""
    wt = _init_repo_with_feature_branch(
        tmp_path,
        main_files={"foo.py": "x = 1\n"},
        working_tree_files={"foo.py": "x = 1\ny = 2\n"},
    )
    _seed_and_configure(_isolate, wt)
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/file?path=foo.py")
    assert r.status_code == 200
    body = r.json()
    kinds = {ln["kind"] for h in body["hunks"] for ln in h["lines"]}
    assert "uncommitted_add" in kinds
    assert "committed_add" not in kinds


def test_file_view_both_committed_and_uncommitted(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    """Branch commits one change; working tree adds another. Response
    contains both classifications."""
    wt = _init_repo_with_feature_branch(
        tmp_path,
        main_files={"foo.py": "x = 1\n"},
        feature_files={"foo.py": "x = 1\ny = 2\n"},
        working_tree_files={"foo.py": "x = 1\ny = 2\nz = 3\n"},
    )
    _seed_and_configure(_isolate, wt)
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/file?path=foo.py")
    assert r.status_code == 200
    body = r.json()
    kinds = {ln["kind"] for h in body["hunks"] for ln in h["lines"]}
    assert "committed_add" in kinds
    assert "uncommitted_add" in kinds


def test_file_view_untracked_file_renders_as_all_uncommitted_add(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    """Untracked file → no committed diff, but every line is an
    uncommitted-add since it's brand new on disk."""
    wt = _init_repo_with_feature_branch(
        tmp_path,
        main_files={"foo.py": "x = 1\n"},
        untracked_files={"new.py": "a = 1\nb = 2\n"},
    )
    _seed_and_configure(_isolate, wt)
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/file?path=new.py")
    assert r.status_code == 200
    body = r.json()
    assert body["on_disk_content"] == "a = 1\nb = 2\n"
    kinds = [ln["kind"] for h in body["hunks"] for ln in h["lines"]]
    assert kinds and all(k == "uncommitted_add" for k in kinds)


# --- file status flags -----------------------------------------------------


def test_file_view_binary_file_returns_is_binary(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    wt = _init_repo_with_feature_branch(tmp_path, main_files={"foo.py": "x\n"})
    _seed_and_configure(_isolate, wt)
    (wt / "blob.bin").write_bytes(b"\x00\x01\x02PNG\x00\x00")
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/file?path=blob.bin")
    assert r.status_code == 200
    body = r.json()
    assert body["is_binary"] is True
    assert body["on_disk_content"] is None
    assert body["hunks"] == []


def test_file_view_large_file_returns_is_large(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    wt = _init_repo_with_feature_branch(tmp_path, main_files={"foo.py": "x\n"})
    _seed_and_configure(_isolate, wt)
    # 1.5 MB of repeated content.
    (wt / "big.txt").write_text("0123456789" * 150_000)
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/file?path=big.txt")
    assert r.status_code == 200
    body = r.json()
    assert body["is_large"] is True
    assert body["on_disk_content"] is None


def test_file_view_large_file_with_load_anyway_returns_content(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    wt = _init_repo_with_feature_branch(tmp_path, main_files={"foo.py": "x\n"})
    _seed_and_configure(_isolate, wt)
    payload = "0123456789" * 150_000
    (wt / "big.txt").write_text(payload)
    with TestClient(app) as client:
        r = client.get(
            "/api/worktree/myapp/feature/file?path=big.txt&load_anyway=true"
        )
    assert r.status_code == 200
    body = r.json()
    assert body["is_large"] is True  # flag still set so the UI can warn
    assert body["on_disk_content"] == payload


def test_file_view_missing_file_returns_is_missing(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    wt = _init_repo_with_feature_branch(tmp_path, main_files={"foo.py": "x\n"})
    _seed_and_configure(_isolate, wt)
    with TestClient(app) as client:
        r = client.get(
            "/api/worktree/myapp/feature/file?path=nonexistent.py"
        )
    assert r.status_code == 200
    body = r.json()
    assert body["is_missing"] is True
    assert body["on_disk_content"] is None
    assert body["hunks"] == []


def test_file_view_lockfile_sets_generated_flag(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    wt = _init_repo_with_feature_branch(
        tmp_path,
        main_files={"pnpm-lock.yaml": "lockfileVersion: 6.0\n"},
    )
    _seed_and_configure(_isolate, wt)
    with TestClient(app) as client:
        r = client.get(
            "/api/worktree/myapp/feature/file?path=pnpm-lock.yaml"
        )
    assert r.status_code == 200
    assert r.json()["is_generated_or_lockfile"] is True


def test_file_view_regular_file_does_not_set_generated_flag(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    wt = _init_repo_with_feature_branch(tmp_path, main_files={"foo.py": "x\n"})
    _seed_and_configure(_isolate, wt)
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/file?path=foo.py")
    assert r.status_code == 200
    assert r.json()["is_generated_or_lockfile"] is False


def test_file_view_rename_returns_rename_from(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    """Move a file in the branch; the renamed path should report the
    original via ``rename_from``."""
    wt = _init_repo_with_feature_branch(
        tmp_path, main_files={"old_name.py": "x = 1\ny = 2\nz = 3\n"}
    )
    _seed_and_configure(_isolate, wt)
    # Rename + commit on the feature branch.
    _git(wt, "mv", "old_name.py", "new_name.py")
    _git(wt, "commit", "-q", "-m", "rename")
    with TestClient(app) as client:
        r = client.get(
            "/api/worktree/myapp/feature/file?path=new_name.py"
        )
    assert r.status_code == 200
    assert r.json()["rename_from"] == "old_name.py"


# --- branch + PR context ---------------------------------------------------


def test_file_view_branch_matches_pr(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    """Worktree on feature branch, row.branch == 'feature' → match."""
    wt = _init_repo_with_feature_branch(tmp_path, main_files={"foo.py": "x\n"})
    _seed_and_configure(_isolate, wt)
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/file?path=foo.py")
    body = r.json()
    assert body["workspace_branch"] == "feature"
    assert body["pr_branch"] == "feature"
    assert body["branch_matches_pr"] is True


def test_file_view_branch_mismatch_when_worktree_on_other_branch(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    """Row says 'feature', but worktree got checked out back to main.
    Banner should fire."""
    wt = _init_repo_with_feature_branch(tmp_path, main_files={"foo.py": "x\n"})
    _seed_and_configure(_isolate, wt)
    _git(wt, "checkout", "-q", "main")
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/file?path=foo.py")
    body = r.json()
    assert body["workspace_branch"] == "main"
    assert body["pr_branch"] == "feature"
    assert body["branch_matches_pr"] is False


def test_file_view_file_in_pr_diff_flag(
    _isolate: dict[str, Path], tmp_path: Path
) -> None:
    """Branch touched ``foo.py``; ``bar.py`` exists in both branches
    unchanged. Flag flips per file."""
    wt = _init_repo_with_feature_branch(
        tmp_path,
        main_files={"foo.py": "x = 1\n", "bar.py": "y = 2\n"},
        feature_files={"foo.py": "x = 1\nz = 3\n"},
    )
    _seed_and_configure(_isolate, wt)
    with TestClient(app) as client:
        r1 = client.get("/api/worktree/myapp/feature/file?path=foo.py")
        r2 = client.get("/api/worktree/myapp/feature/file?path=bar.py")
    assert r1.json()["file_in_pr_diff"] is True
    assert r2.json()["file_in_pr_diff"] is False


# --- unified-diff parser unit tests ----------------------------------------


def test_parse_unified_diff_handles_basic_hunk() -> None:
    """The parser turns a standard ``@@ -a,b +c,d @@`` body into
    GitDiffHunk + GitDiffLine entries with correct lineno tracking."""
    from app.services.git_cli import parse_unified_diff

    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,3 +1,4 @@\n"
        " unchanged_a\n"
        "-removed_line\n"
        "+added_line_1\n"
        "+added_line_2\n"
        " unchanged_b\n"
    )
    hunks = parse_unified_diff(diff)
    assert len(hunks) == 1
    h = hunks[0]
    assert h.old_start == 1 and h.new_start == 1
    kinds = [ln.kind for ln in h.lines]
    assert kinds == ["context", "remove", "add", "add", "context"]
    # Adds carry new_lineno; removes carry old_lineno.
    add_linenos = [ln.new_lineno for ln in h.lines if ln.kind == "add"]
    assert add_linenos == [2, 3]
    remove_lineno = [ln.old_lineno for ln in h.lines if ln.kind == "remove"][0]
    assert remove_lineno == 2


def test_parse_unified_diff_handles_multiple_hunks() -> None:
    from app.services.git_cli import parse_unified_diff

    diff = (
        "@@ -1,0 +1,1 @@\n"
        "+x\n"
        "@@ -10,0 +11,1 @@\n"
        "+y\n"
    )
    hunks = parse_unified_diff(diff)
    assert len(hunks) == 2
    assert hunks[0].new_start == 1
    assert hunks[1].new_start == 11
