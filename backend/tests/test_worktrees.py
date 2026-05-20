"""Tests for the worktree CRUD slice (model, service, /api/worktree)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.models.worktree import derive_worktree_name, extract_ticket
from tests.fixtures.config import write_repo_config
from tests.fixtures.worktree import init_git_repo, seed_worktree

# --- fixtures ------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Wrapper that always seeds the ``feature`` branch this file's
    tests need for ``git worktree add``."""
    init_git_repo(path, branches=["feature"])


# --- pure helpers --------------------------------------------------------


def test_derive_worktree_name_basic() -> None:
    assert derive_worktree_name("main") == "main"
    assert derive_worktree_name("cleanup-old-foo") == "cleanup_old_foo"


def test_derive_worktree_name_strips_prefix() -> None:
    assert derive_worktree_name("alice/cleanup-foo", "alice/") == "cleanup_foo"


def test_derive_worktree_name_preserves_ticket() -> None:
    out = derive_worktree_name(
        "alice/TICKET-77_login-flow-fix",
        branch_prefix="alice/",
        ticket_pattern=r"[A-Z]+-\d+",
    )
    assert out == "TICKET-77_login_flow_fix"


def test_extract_ticket() -> None:
    assert extract_ticket("alice/PROJ-12_x", r"[A-Z]+-\d+") == "PROJ-12"
    assert extract_ticket("alice/foo", r"[A-Z]+-\d+") is None
    assert extract_ticket("alice/foo", None) is None


# --- /api/worktree -------------------------------------------------------


def test_list_worktrees_initially_empty() -> None:
    with TestClient(app) as client:
        r = client.get("/api/worktrees")
    assert r.status_code == 200
    body = r.json()
    assert body["worktrees"] == []
    assert "user_login" in body


def test_create_unknown_repo_400(_isolate: dict[str, Path]) -> None:
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        _isolate["dev_root"] / "ignored",
        name="registered",
    )
    with TestClient(app) as client:
        r = client.post("/api/worktree", json={"repo": "not-registered", "branch": "main"})
    assert r.status_code == 400
    assert "unknown repo" in r.json()["detail"]


def test_create_happy_path(_isolate: dict[str, Path]) -> None:
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repo_path,
        setup_steps=[{"cmd": "echo setup-ran", "cwd": ""}],
    )

    with TestClient(app) as client:
        r = client.post("/api/worktree", json={"repo": "myapp", "branch": "feature"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready"
        assert body["name"] == "feature"
        assert body["branch"] == "feature"
        assert Path(body["path"]).exists()

        r2 = client.get("/api/worktree/myapp/feature")
    detail = r2.json()
    assert detail["row"]["status"] == "ready"
    assert any("setup-ran" in line for line in detail["log"])


def test_create_duplicate_409(_isolate: dict[str, Path]) -> None:
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    with TestClient(app) as client:
        r1 = client.post("/api/worktree", json={"repo": "myapp", "branch": "feature"})
        assert r1.status_code == 200
        assert r1.json()["status"] == "ready"
        r2 = client.post("/api/worktree", json={"repo": "myapp", "branch": "feature"})
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"]


def test_setup_step_failure_marks_code_on_disk(_isolate: dict[str, Path]) -> None:
    """Setup-step failure after `git worktree add` succeeded routes
    to `code_on_disk`, not `failed`. The user keeps access to iTerm2 /
    Cursor on a usable worktree."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repo_path,
        setup_steps=[
            {"cmd": "echo first-step-ok", "cwd": ""},
            {"cmd": "false", "cwd": ""},  # exit 1
            {"cmd": "echo should-not-run", "cwd": ""},
        ],
    )

    with TestClient(app) as client:
        r = client.post("/api/worktree", json={"repo": "myapp", "branch": "feature"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "code_on_disk"
        # And the on-disk path actually exists — that's the whole
        # premise of the new status.
        assert Path(body["path"]).is_dir()
        r2 = client.get("/api/worktree/myapp/feature")
    detail = r2.json()
    log = detail["log"]
    assert any("first-step-ok" in line for line in log)
    assert any("setup step 1 failed" in line for line in log)
    assert not any("should-not-run" in line for line in log)


def test_missing_branch_marks_failed(_isolate: dict[str, Path]) -> None:
    """Pre-worktree-add failure (branch doesn't exist) → still
    `failed`. There's no usable code on disk."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    with TestClient(app) as client:
        r = client.post("/api/worktree", json={"repo": "myapp", "branch": "nope-not-real"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "failed"
        assert not Path(body["path"]).is_dir()
        r2 = client.get("/api/worktree/myapp/nope_not_real")
    detail = r2.json()
    assert any("not found locally or on origin" in line for line in detail["log"])


def test_get_worktree_404() -> None:
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/missing")
    assert r.status_code == 404


# --- /api/worktree/{repo}/{name}/recreate --------------------------------


def test_recreate_404_when_worktree_missing(_isolate: dict[str, Path]) -> None:
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], _isolate["dev_root"])
    with TestClient(app) as client:
        r = client.post("/api/worktree/myapp/nope/recreate")
    assert r.status_code == 404


def test_recreate_409_when_status_is_ready(_isolate: dict[str, Path]) -> None:
    """Recreate refuses ready rows — they have on-disk state the user
    may not want destroyed without thinking. Only stale + code_on_disk
    are accepted."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    with TestClient(app) as client:
        r1 = client.post(
            "/api/worktree", json={"repo": "myapp", "branch": "feature"}
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "ready"
        r2 = client.post("/api/worktree/myapp/feature/recreate")
    assert r2.status_code == 409
    assert "stale" in r2.json()["detail"]


def test_recreate_allows_code_on_disk(_isolate: dict[str, Path]) -> None:
    """Recreate accepts code_on_disk rows — the user knows setup didn't
    finish and explicitly wants to wipe and retry."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repo_path,
        setup_steps=[{"cmd": "false", "cwd": ""}],  # guaranteed fail
    )

    with TestClient(app) as client:
        r1 = client.post(
            "/api/worktree", json={"repo": "myapp", "branch": "feature"}
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "code_on_disk"
        old_created_at = r1.json()["created_at"]
        # Recreate should be accepted (and will fail setup again,
        # since we didn't fix the failing step — but that's the
        # user's problem, not the endpoint's).
        r2 = client.post("/api/worktree/myapp/feature/recreate")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "code_on_disk"  # still fails setup
    assert body["created_at"] != old_created_at  # fresh row


def test_recreate_still_rejects_failed(_isolate: dict[str, Path]) -> None:
    """Recreate is not validated for genuinely-failed rows (no code on
    disk). Keep the rejection until that path is exercised."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    with TestClient(app) as client:
        r1 = client.post(
            "/api/worktree",
            json={"repo": "myapp", "branch": "nope-not-real"},
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "failed"
        r2 = client.post("/api/worktree/myapp/nope_not_real/recreate")
    assert r2.status_code == 409
    assert "code_on_disk" in r2.json()["detail"]


def test_recreate_stale_row_drops_and_reinserts(_isolate: dict[str, Path]) -> None:
    """End-to-end: create a worktree, mark it stale in the DB to
    simulate "user deleted the directory outside CDH and ran Sync",
    then click Recreate. The row should be replaced with a fresh
    ready row pointing at the same branch."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    with TestClient(app) as client:
        r1 = client.post(
            "/api/worktree", json={"repo": "myapp", "branch": "feature"}
        )
        assert r1.status_code == 200, r1.text
        old_path = r1.json()["path"]
        old_created_at = r1.json()["created_at"]

        # Simulate: user `rm -rf`d the on-disk directory WITHOUT
        # running `git worktree prune`. Git still tracks the (now-
        # broken) worktree as prunable. The recreate endpoint must
        # prune git's tracking itself before `git worktree add`
        # can succeed against the same path.
        import shutil

        shutil.rmtree(old_path)
        # NOTE: deliberately NOT running `git worktree prune` here —
        # the endpoint should handle the un-pruned case.
        conn = db.open_db(_isolate["db_path"])
        try:
            conn.execute(
                "UPDATE worktree SET status='stale' WHERE repo=? AND name=?",
                ("myapp", "feature"),
            )
            conn.commit()
        finally:
            conn.close()

        # Click Recreate — should re-run create_worktree against the
        # same branch and return a fresh ready row.
        r2 = client.post("/api/worktree/myapp/feature/recreate")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "ready"
    assert body["branch"] == "feature"
    assert body["name"] == "feature"
    assert body["created_at"] != old_created_at  # fresh insert
    assert Path(body["path"]).exists()


# --- /api/worktree/{repo}/{name}/pr-url ----------------------------------


def test_pr_url_uses_cached_values(_isolate: dict[str, Path]) -> None:
    wt_path = _isolate["dev_root"] / "myapp_wt"
    seed_worktree(
        _isolate["db_path"],
        "myapp",
        "feature",
        path=wt_path,
        branch="feature",
        pr_number=42,
        pr_repo="acme/myapp",
    )
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/pr-url")
    assert r.status_code == 200
    assert r.json() == {"url": "https://github.com/acme/myapp/pull/42"}


def test_pr_url_404_when_worktree_missing() -> None:
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/nope/pr-url")
    assert r.status_code == 404


def test_pr_url_lazy_lookup_and_cache(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """First call: no cache → shell `gh`, return URL, write cache.
    Second call: cache hit, no `gh` invocation."""
    wt_path = _isolate["dev_root"] / "myapp_wt"
    seed_worktree(
        _isolate["db_path"], "myapp", "feature", path=wt_path, branch="feature"
    )

    call_count = {"n": 0}

    async def fake_gh_pr_view(cwd: Path) -> dict:
        call_count["n"] += 1
        return {
            "number": 7,
            "url": "https://github.com/acme/myapp/pull/7",
            "headRepository": {"name": "myapp"},
            "headRepositoryOwner": {"login": "acme"},
        }

    from app.routes import worktrees as routes_module

    monkeypatch.setattr(routes_module, "_gh_pr_view", fake_gh_pr_view)

    with TestClient(app) as client:
        r1 = client.get("/api/worktree/myapp/feature/pr-url")
        assert r1.status_code == 200
        assert r1.json() == {"url": "https://github.com/acme/myapp/pull/7"}
        assert call_count["n"] == 1

        r2 = client.get("/api/worktree/myapp/feature/pr-url")
        assert r2.status_code == 200
        assert r2.json() == {"url": "https://github.com/acme/myapp/pull/7"}
        # Cache hit — `gh` should NOT have been re-invoked.
        assert call_count["n"] == 1


def test_pr_url_404_when_no_pr(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    wt_path = _isolate["dev_root"] / "myapp_wt"
    seed_worktree(
        _isolate["db_path"], "myapp", "feature", path=wt_path, branch="feature"
    )

    async def fake_gh_pr_view(cwd: Path) -> None:
        return None

    from app.routes import worktrees as routes_module

    monkeypatch.setattr(routes_module, "_gh_pr_view", fake_gh_pr_view)

    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/pr-url")
    assert r.status_code == 404
    assert "no open PR" in r.json()["detail"]


def test_pr_url_400_when_worktree_path_missing(_isolate: dict[str, Path]) -> None:
    seed_worktree(
        _isolate["db_path"],
        "myapp",
        "feature",
        path=_isolate["dev_root"] / "does-not-exist",
        branch="feature",
        mkdir=False,
    )
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/pr-url")
    assert r.status_code == 400
    assert "missing on disk" in r.json()["detail"]


# --- /api/worktree/{repo}/{name}/open-cursor -----------------------------


def _stub_cursor_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stderr: bytes = b"",
    raise_filenotfound: bool = False,
) -> dict:
    """Replace ``asyncio.create_subprocess_exec`` with a fake that
    captures the argv it was called with and returns a process whose
    ``communicate()`` yields ``(b"", stderr)`` + ``returncode``.

    Returns a dict the test can inspect after the call (``["argv"]``)."""
    from unittest.mock import AsyncMock

    captured: dict = {"argv": None}

    class FakeProc:
        def __init__(self) -> None:
            self.returncode = returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"", stderr)

    async def fake_exec(*args: object, **kwargs: object) -> FakeProc:
        # Only react to actual `cursor …` invocations; background
        # pollers (inbox / pr_state) and the pr-files endpoint also
        # call ``asyncio.create_subprocess_exec`` and would otherwise
        # clobber the captured argv.
        if not args or args[0] != "cursor":
            return FakeProc()
        if raise_filenotfound:
            raise FileNotFoundError("[Errno 2] No such file: 'cursor'")
        captured["argv"] = list(args)
        return FakeProc()

    monkeypatch.setattr(
        "asyncio.create_subprocess_exec", AsyncMock(side_effect=fake_exec)
    )
    return captured


def test_open_cursor_404_when_worktree_missing(_isolate: dict[str, Path]) -> None:
    with TestClient(app) as client:
        r = client.post("/api/worktree/myapp/nope/open-cursor")
    assert r.status_code == 404
    assert "worktree not found" in r.json()["detail"]


def test_open_cursor_400_when_path_missing_on_disk(
    _isolate: dict[str, Path],
) -> None:
    seed_worktree(
        _isolate["db_path"],
        "myapp",
        "feature",
        path=_isolate["dev_root"] / "ghost",
        mkdir=False,
    )
    with TestClient(app) as client:
        r = client.post("/api/worktree/myapp/feature/open-cursor")
    assert r.status_code == 400
    assert "missing on disk" in r.json()["detail"]


def test_open_cursor_503_when_cursor_not_on_path(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cursor` binary missing from PATH — Python raises FileNotFoundError
    before exec. Endpoint must return 503 with install instructions."""
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    _stub_cursor_subprocess(monkeypatch, raise_filenotfound=True)

    with TestClient(app) as client:
        r = client.post("/api/worktree/myapp/feature/open-cursor")
    assert r.status_code == 503
    assert "Cursor CLI not on PATH" in r.json()["detail"]
    assert "cursor.com" in r.json()["detail"]


def test_open_cursor_503_when_subprocess_reports_missing(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some shells return non-zero with 'command not found' in stderr
    rather than raising FileNotFoundError. Endpoint must still 503."""
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    _stub_cursor_subprocess(
        monkeypatch, returncode=127, stderr=b"cursor: command not found"
    )

    with TestClient(app) as client:
        r = client.post("/api/worktree/myapp/feature/open-cursor")
    assert r.status_code == 503
    assert "Cursor CLI not on PATH" in r.json()["detail"]


def test_open_cursor_502_when_cursor_errors(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    _stub_cursor_subprocess(
        monkeypatch, returncode=1, stderr=b"cursor: something went wrong"
    )

    with TestClient(app) as client:
        r = client.post("/api/worktree/myapp/feature/open-cursor")
    assert r.status_code == 502
    assert "cursor exited 1" in r.json()["detail"]
    assert "something went wrong" in r.json()["detail"]


def test_open_cursor_happy_path_invokes_subprocess(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    captured = _stub_cursor_subprocess(monkeypatch)

    with TestClient(app) as client:
        r = client.post("/api/worktree/myapp/feature/open-cursor")
    assert r.status_code == 200
    assert r.json() == {"opened": True}
    # First two positional args are ("cursor", "<wt_path>").
    assert captured["argv"][:2] == ["cursor", str(wt_path)]


def test_open_cursor_with_file_invokes_cursor_with_workspace_and_file(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-file open must pass both the worktree folder AND the file
    so Cursor loads the workspace (project root, language-server
    settings) before bringing the file into focus. Without the
    folder, pyright / pylance can't resolve any imports."""
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    (wt_path / "src").mkdir()
    (wt_path / "src" / "foo.py").write_text("# foo\n")
    captured = _stub_cursor_subprocess(monkeypatch)

    with TestClient(app) as client:
        r = client.post(
            "/api/worktree/myapp/feature/open-cursor",
            json={"file": "src/foo.py"},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"opened": True}
    assert captured["argv"][:3] == [
        "cursor",
        str(wt_path),
        str(wt_path / "src" / "foo.py"),
    ]


def test_open_cursor_rejects_parent_traversal(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    _stub_cursor_subprocess(monkeypatch)

    with TestClient(app) as client:
        r = client.post(
            "/api/worktree/myapp/feature/open-cursor",
            json={"file": "../../../etc/passwd"},
        )
    assert r.status_code == 400
    assert "worktree root" in r.json()["detail"]


def test_open_cursor_rejects_absolute_file_path(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    _stub_cursor_subprocess(monkeypatch)

    with TestClient(app) as client:
        r = client.post(
            "/api/worktree/myapp/feature/open-cursor",
            json={"file": "/etc/passwd"},
        )
    assert r.status_code == 400
    assert "worktree root" in r.json()["detail"]


def test_open_cursor_rejects_nonexistent_file(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    _stub_cursor_subprocess(monkeypatch)

    with TestClient(app) as client:
        r = client.post(
            "/api/worktree/myapp/feature/open-cursor",
            json={"file": "does-not-exist.py"},
        )
    assert r.status_code == 400
    assert "does not exist" in r.json()["detail"]


def test_open_cursor_rejects_symlink_escaping_worktree(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A symlink inside the worktree that resolves to a path outside the
    worktree must be rejected — `resolve().relative_to()` catches the
    escape after symlink resolution."""
    import os

    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    # Make a target outside the worktree, then a symlink inside pointing at it.
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    os.symlink(outside, wt_path / "escape.txt")
    _stub_cursor_subprocess(monkeypatch)

    with TestClient(app) as client:
        r = client.post(
            "/api/worktree/myapp/feature/open-cursor",
            json={"file": "escape.txt"},
        )
    assert r.status_code == 400
    assert "worktree root" in r.json()["detail"]


# --- /api/worktree/{repo}/{name}/pr-files --------------------------------


def _stub_git_diff_numstat(
    monkeypatch: pytest.MonkeyPatch,
    *,
    by_ref: dict[str, tuple[int, bytes]] | None = None,
    default: tuple[int, bytes] = (0, b""),
) -> dict:
    """Replace ``asyncio.create_subprocess_exec`` (as imported via the
    worktrees route module) so each invocation matches one of the
    expected ``git diff --numstat <ref>...HEAD`` shapes and returns a
    canned ``(returncode, stdout)``.

    ``by_ref`` lets a test return different results per ref (e.g., to
    simulate "origin/main missing, falls back to main"). The argv's
    ref segment is the 8th token: ``git -C <wt> diff --numstat
    --no-renames <ref>...HEAD``. ``default`` is used for any ref not
    in ``by_ref``.

    Returns ``{"calls": [argv, ...]}`` for inspection.
    """
    from unittest.mock import AsyncMock

    captured: dict = {"calls": []}

    class FakeProc:
        def __init__(self, rc: int, stdout: bytes) -> None:
            self.returncode = rc
            self._stdout = stdout

        async def communicate(self) -> tuple[bytes, bytes]:
            return (self._stdout, b"")

    async def fake_exec(*args: object, **kwargs: object) -> FakeProc:
        captured["calls"].append(list(args))
        # The ref...HEAD token is the last positional before the kwargs.
        ref_token = ""
        for a in args:
            if isinstance(a, str) and a.endswith("...HEAD"):
                ref_token = a[: -len("...HEAD")]
                break
        rc, stdout = (by_ref or {}).get(ref_token, default)
        return FakeProc(rc, stdout)

    monkeypatch.setattr(
        "asyncio.create_subprocess_exec", AsyncMock(side_effect=fake_exec)
    )
    return captured


def test_pr_files_404_when_worktree_missing(_isolate: dict[str, Path]) -> None:
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/nope/pr-files")
    assert r.status_code == 404


def test_pr_files_400_when_path_missing_on_disk(
    _isolate: dict[str, Path],
) -> None:
    seed_worktree(
        _isolate["db_path"],
        "myapp",
        "feature",
        path=_isolate["dev_root"] / "ghost",
        mkdir=False,
    )
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/pr-files")
    assert r.status_code == 400
    assert "missing on disk" in r.json()["detail"]


def test_pr_files_parses_numstat_output(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: git diff --numstat returns one line per changed
    file, tab-separated <adds>\\t<dels>\\t<path>."""
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    out = b"12\t3\tsrc/foo.py\n0\t5\tsrc/bar.py\n"
    _stub_git_diff_numstat(monkeypatch, default=(0, out))

    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/pr-files")
    assert r.status_code == 200
    body = r.json()
    assert len(body["files"]) == 2
    assert body["files"][0]["path"] == "src/foo.py"
    assert body["files"][0]["additions"] == 12
    assert body["files"][0]["deletions"] == 3
    assert body["files"][1]["additions"] == 0
    assert body["files"][1]["deletions"] == 5


def test_pr_files_empty_when_git_returns_empty(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branch at HEAD of base — git diff returns nothing, endpoint
    returns an empty list (not 5xx)."""
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    _stub_git_diff_numstat(monkeypatch, default=(0, b""))

    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/pr-files")
    assert r.status_code == 200
    assert r.json() == {"files": []}


def test_pr_files_empty_when_git_fails_both_refs(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither origin/<branch> nor bare <branch> ref resolves — both
    git calls exit non-zero. Endpoint returns empty rather than 5xx
    so the section just renders nothing."""
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    _stub_git_diff_numstat(monkeypatch, default=(128, b""))

    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/pr-files")
    assert r.status_code == 200
    assert r.json() == {"files": []}


def test_pr_files_falls_back_to_local_ref_when_origin_missing(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``origin/main`` ref doesn't exist locally → first git call
    exits non-zero. Endpoint retries with bare ``main`` and uses
    its output."""
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    captured = _stub_git_diff_numstat(
        monkeypatch,
        by_ref={
            "origin/main": (128, b""),
            "main": (0, b"5\t2\tsrc/x.py\n"),
        },
    )

    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/pr-files")
    assert r.status_code == 200
    assert len(r.json()["files"]) == 1
    # Both refs were tried, in order.
    refs_seen = [
        token
        for call in captured["calls"]
        for token in call
        if isinstance(token, str) and token.endswith("...HEAD")
    ]
    assert refs_seen == ["origin/main...HEAD", "main...HEAD"]


def test_pr_files_treats_binary_files_as_zero_changes(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """git diff --numstat reports binary files with `-`/`-` instead of
    line counts. Endpoint must not crash; render as 0/0."""
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    _stub_git_diff_numstat(
        monkeypatch, default=(0, b"-\t-\tassets/logo.png\n3\t1\tsrc/x.py\n")
    )

    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/pr-files")
    assert r.status_code == 200
    body = r.json()
    assert body["files"][0]["path"] == "assets/logo.png"
    assert body["files"][0]["additions"] == 0
    assert body["files"][0]["deletions"] == 0
    assert body["files"][1]["additions"] == 3
    assert body["files"][1]["deletions"] == 1


def test_pr_files_uses_repo_default_branch_from_config(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repo config's ``default_branch`` drives the ref used. A repo
    with default_branch='develop' should produce ``origin/develop``
    as the first ref tried."""
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        _isolate["dev_root"] / "myapp",
        default_branch="develop",
    )
    captured = _stub_git_diff_numstat(monkeypatch, default=(0, b""))

    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/pr-files")
    assert r.status_code == 200
    refs_seen = [
        token
        for call in captured["calls"]
        for token in call
        if isinstance(token, str) and token.endswith("...HEAD")
    ]
    assert refs_seen[0] == "origin/develop...HEAD"


def test_pr_files_defaults_to_main_when_repo_not_in_config(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worktree row's repo isn't in the config (e.g., config got out
    of sync). Fall back to 'main' as the base branch."""
    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    # Intentionally no write_repo_config call.
    captured = _stub_git_diff_numstat(monkeypatch, default=(0, b""))

    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/pr-files")
    assert r.status_code == 200
    refs_seen = [
        token
        for call in captured["calls"]
        for token in call
        if isinstance(token, str) and token.endswith("...HEAD")
    ]
    assert refs_seen[0] == "origin/main...HEAD"


def test_pr_files_computes_github_diff_anchor(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each file's `github_diff_anchor` is sha256(path).hexdigest() —
    independent of the data source (was unit-tested when files came
    from gh; verifying it survives the local-git move)."""
    import hashlib

    wt_path = _isolate["dev_root"] / "wt"
    seed_worktree(_isolate["db_path"], "myapp", "feature", path=wt_path)
    _stub_git_diff_numstat(monkeypatch, default=(0, b"1\t0\tsrc/foo.py\n"))

    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature/pr-files")
    expected = hashlib.sha256(b"src/foo.py").hexdigest()
    assert r.json()["files"][0]["github_diff_anchor"] == expected


# --- PUT /api/worktree/{repo}/{name}/notes -------------------------------


def test_update_notes_404_when_worktree_missing(_isolate: dict[str, Path]) -> None:
    with TestClient(app) as client:
        r = client.put(
            "/api/worktree/missing/x/notes",
            json={"notes": "hello"},
        )
    assert r.status_code == 404


def test_update_notes_persists_to_db(_isolate: dict[str, Path]) -> None:
    import sqlite3

    seed_worktree(_isolate["db_path"], "myapp", "feature")
    with TestClient(app) as client:
        r = client.put(
            "/api/worktree/myapp/feature/notes",
            json={"notes": "blocking PROJ-218, keep open"},
        )
    assert r.status_code == 200
    assert r.json() == {"notes": "blocking PROJ-218, keep open"}

    conn = sqlite3.connect(_isolate["db_path"])
    try:
        row = conn.execute(
            "SELECT notes FROM worktree WHERE repo='myapp' AND name='feature'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("blocking PROJ-218, keep open",)


def test_update_notes_overwrites_existing(_isolate: dict[str, Path]) -> None:
    seed_worktree(_isolate["db_path"], "myapp", "feature")
    with TestClient(app) as client:
        client.put("/api/worktree/myapp/feature/notes", json={"notes": "v1"})
        r = client.put(
            "/api/worktree/myapp/feature/notes",
            json={"notes": "v2 — replaces v1"},
        )
    assert r.status_code == 200
    assert r.json()["notes"] == "v2 — replaces v1"


def test_update_notes_empty_string_clears(_isolate: dict[str, Path]) -> None:
    """Empty string is a valid value — the user clears a note by
    deleting all its text. The endpoint should accept it (not 422),
    persist it (read path can see ""), and not coerce to NULL."""
    import sqlite3

    seed_worktree(_isolate["db_path"], "myapp", "feature")
    with TestClient(app) as client:
        client.put("/api/worktree/myapp/feature/notes", json={"notes": "x"})
        r = client.put("/api/worktree/myapp/feature/notes", json={"notes": ""})
    assert r.status_code == 200

    conn = sqlite3.connect(_isolate["db_path"])
    try:
        row = conn.execute(
            "SELECT notes FROM worktree WHERE repo='myapp' AND name='feature'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("",)


def test_update_notes_rejects_oversize_payload(_isolate: dict[str, Path]) -> None:
    """Soft guard against a runaway paste — 10k char ceiling."""
    seed_worktree(_isolate["db_path"], "myapp", "feature")
    with TestClient(app) as client:
        r = client.put(
            "/api/worktree/myapp/feature/notes",
            json={"notes": "a" * 10_001},
        )
    assert r.status_code == 422


def test_get_worktree_includes_notes_field(_isolate: dict[str, Path]) -> None:
    seed_worktree(_isolate["db_path"], "myapp", "feature")
    with TestClient(app) as client:
        client.put("/api/worktree/myapp/feature/notes", json={"notes": "remember this"})
        r = client.get("/api/worktree/myapp/feature")
    assert r.status_code == 200
    assert r.json()["row"]["notes"] == "remember this"


def test_list_worktrees_includes_notes_field(_isolate: dict[str, Path]) -> None:
    seed_worktree(_isolate["db_path"], "myapp", "feature")
    seed_worktree(_isolate["db_path"], "myapp", "other")
    with TestClient(app) as client:
        client.put("/api/worktree/myapp/feature/notes", json={"notes": "first"})
        r = client.get("/api/worktrees")
    assert r.status_code == 200
    rows = {row["name"]: row for row in r.json()["worktrees"]}
    assert rows["feature"]["notes"] == "first"
    # Untouched rows still serialize with notes=None.
    assert rows["other"]["notes"] is None
