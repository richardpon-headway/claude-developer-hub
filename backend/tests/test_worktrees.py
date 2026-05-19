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
    assert r.json() == []


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


def test_setup_step_failure_marks_failed(_isolate: dict[str, Path]) -> None:
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
        assert r.json()["status"] == "failed"
        r2 = client.get("/api/worktree/myapp/feature")
    detail = r2.json()
    log = detail["log"]
    assert any("first-step-ok" in line for line in log)
    assert any("setup step 1 failed" in line for line in log)
    assert not any("should-not-run" in line for line in log)


def test_missing_branch_marks_failed(_isolate: dict[str, Path]) -> None:
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    with TestClient(app) as client:
        r = client.post("/api/worktree", json={"repo": "myapp", "branch": "nope-not-real"})
        assert r.status_code == 200
        assert r.json()["status"] == "failed"
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


def test_recreate_409_when_status_not_stale(_isolate: dict[str, Path]) -> None:
    """Recreate is intentionally limited to stale rows — for a ready or
    failed row we'd be destroying on-disk state the user might want
    to investigate or stash."""
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
