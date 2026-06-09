"""Tests for the worktree CRUD slice (model, service, /api/worktree).

Create-flow tests exercise ``wt_svc.create_worktree`` directly via
``asyncio.run(...)``. The bare ``POST /api/worktree`` endpoint was
removed once pull-down funnelled through ``pull_down.perform_pull_down``
and ``recreate`` covered the retry path; the route exposed no
production behavior that needed HTTP framing.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.models.worktree import derive_worktree_name, extract_ticket
from app.services import worktree as wt_svc
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


def test_derive_worktree_name_prepends_inferred_ticket() -> None:
    """A ticket inferred from PR metadata (not in the branch) is
    prepended so the folder name matches the in-branch case."""
    out = derive_worktree_name(
        "pci-recoup-lifecycle-statuses",
        ticket_pattern=r"[A-Z]+-\d+",
        ticket="COR-272",
    )
    assert out == "COR-272_pci_recoup_lifecycle_statuses"


def test_derive_worktree_name_no_double_prefix_when_ticket_in_branch() -> None:
    """When the ticket already lives in the branch, passing the same
    ticket is a no-op — no double-prefix."""
    out = derive_worktree_name(
        "alice/TICKET-77_login-flow-fix",
        branch_prefix="alice/",
        ticket_pattern=r"[A-Z]+-\d+",
        ticket="TICKET-77",
    )
    assert out == "TICKET-77_login_flow_fix"


def test_derive_worktree_name_ticket_none_matches_no_ticket() -> None:
    """``ticket=None`` reproduces the no-ticket signature exactly."""
    assert (
        derive_worktree_name("cleanup-old-foo", ticket=None)
        == derive_worktree_name("cleanup-old-foo")
        == "cleanup_old_foo"
    )


def test_derive_worktree_name_prepended_ticket_keeps_internal_hyphen() -> None:
    """The prepended ticket keeps its own hyphen while the tail is
    underscored."""
    out = derive_worktree_name("some-branch", ticket="COR-272")
    assert out == "COR-272_some_branch"


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


def test_create_unknown_repo_raises(_isolate: dict[str, Path]) -> None:
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        _isolate["dev_root"] / "ignored",
        name="registered",
    )
    with pytest.raises(wt_svc.WorktreeCreationError) as exc_info:
        asyncio.run(wt_svc.create_worktree("not-registered", "main"))
    assert "unknown repo" in str(exc_info.value)


def test_create_happy_path(_isolate: dict[str, Path]) -> None:
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repo_path,
        setup_steps=[{"cmd": "echo setup-ran", "cwd": ""}],
    )

    row = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    assert row.status == "ready"
    assert row.name == "feature"
    assert row.branch == "feature"
    assert Path(row.path).exists()

    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature")
    detail = r.json()
    assert detail["row"]["status"] == "ready"
    assert any("setup-ran" in line for line in detail["log"])


def test_create_worktree_ticket_override(_isolate: dict[str, Path]) -> None:
    """An explicit ``ticket_override`` (a ticket inferred from PR
    metadata) drives both the folder name and the stored ticket, even
    when the branch itself carries no ticket."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repo_path,
        ticket_pattern=r"[A-Z]+-\d+",
    )

    row = asyncio.run(
        wt_svc.create_and_wait("myapp", "feature", ticket_override="COR-9")
    )
    assert row.status == "ready"
    assert row.name == "COR-9_feature"
    assert row.ticket == "COR-9"
    assert row.branch == "feature"


def test_create_worktree_no_override_is_branch_only(
    _isolate: dict[str, Path],
) -> None:
    """With no override the ticket/name come from the branch alone —
    behavior unchanged from before."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repo_path,
        ticket_pattern=r"[A-Z]+-\d+",
    )

    row = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    assert row.name == "feature"
    assert row.ticket is None


# --- pull-down ticket inference (branch → title → body → commits) --------


def _run_pull_down(
    _isolate: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    gh_payload: dict,
) -> wt_svc.WorktreeRow:
    """Drive ``perform_pull_down`` against an isolated repo with a
    stubbed ``gh pr view`` payload, then return the resulting worktree
    row. The repo has a ticket-less ``feature`` branch so the ticket can
    only come from the stubbed PR metadata."""
    from app.services import pull_down

    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repo_path,
        name="myapp",
        github_repo="acme/myapp",
        ticket_pattern=r"[A-Z]+-\d+",
    )

    async def fake_run_gh_json(args: list, **kwargs: object) -> dict:
        return gh_payload

    monkeypatch.setattr(pull_down, "run_gh_json", fake_run_gh_json)

    async def _drive() -> None:
        await pull_down.perform_pull_down("acme/myapp", 42)
        await wt_svc.wait_for_setup_complete()

    asyncio.run(_drive())
    rows = wt_svc.list_worktrees_sync()
    assert len(rows) == 1
    return rows[0]


def test_pull_down_ticket_from_title(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _run_pull_down(
        _isolate,
        monkeypatch,
        {
            "headRefName": "feature",
            "isCrossRepository": False,
            "title": "[COR-272] Add remediation statuses",
            "body": "no ticket here",
            "commits": [],
        },
    )
    assert row.ticket == "COR-272"
    assert row.name == "COR-272_feature"


def test_pull_down_ticket_from_body_when_title_clean(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _run_pull_down(
        _isolate,
        monkeypatch,
        {
            "headRefName": "feature",
            "isCrossRepository": False,
            "title": "Add remediation statuses",
            "body": "Implements COR-272 per the spec.",
            "commits": [],
        },
    )
    assert row.ticket == "COR-272"
    assert row.name == "COR-272_feature"


def test_pull_down_ticket_from_commits_last_resort(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _run_pull_down(
        _isolate,
        monkeypatch,
        {
            "headRefName": "feature",
            "isCrossRepository": False,
            "title": "Add remediation statuses",
            "body": "no ticket",
            "commits": [
                {"messageHeadline": "wip", "messageBody": ""},
                {"messageHeadline": "COR-272 finalize", "messageBody": "details"},
            ],
        },
    )
    assert row.ticket == "COR-272"
    assert row.name == "COR-272_feature"


def test_pull_down_no_ticket_anywhere(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _run_pull_down(
        _isolate,
        monkeypatch,
        {
            "headRefName": "feature",
            "isCrossRepository": False,
            "title": "Add remediation statuses",
            "body": "nothing here",
            "commits": [{"messageHeadline": "wip", "messageBody": ""}],
        },
    )
    assert row.ticket is None
    assert row.name == "feature"


def test_pull_down_branch_ticket_wins_over_metadata(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ticket in the branch takes precedence and the folder name is
    branch-derived (no metadata override, no double-prefix)."""
    repo_path = _isolate["dev_root"] / "myapp"
    init_git_repo(repo_path, branches=["COR-100-fix"])
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repo_path,
        name="myapp",
        github_repo="acme/myapp",
        ticket_pattern=r"[A-Z]+-\d+",
    )

    from app.services import pull_down

    async def fake_run_gh_json(args: list, **kwargs: object) -> dict:
        return {
            "headRefName": "COR-100-fix",
            "isCrossRepository": False,
            "title": "[COR-272] different ticket in title",
            "body": "",
            "commits": [],
        }

    monkeypatch.setattr(pull_down, "run_gh_json", fake_run_gh_json)

    async def _drive() -> None:
        await pull_down.perform_pull_down("acme/myapp", 42)
        await wt_svc.wait_for_setup_complete()

    asyncio.run(_drive())
    rows = wt_svc.list_worktrees_sync()
    assert len(rows) == 1
    assert rows[0].ticket == "COR-100"
    assert rows[0].name == "COR-100_fix"


def test_create_returns_existing_row_on_duplicate(
    _isolate: dict[str, Path],
) -> None:
    """Second call for the same (repo, branch) is a no-op — returns
    the existing ready row instead of raising. The strict-mode 409
    contract was retired with the bare-POST endpoint; pull-down is
    a user-clicked action and a second click must be idempotent."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    first = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    assert first.status == "ready"
    second = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    assert second.status == "ready"
    assert second.created_at == first.created_at  # same row, not reinserted
    assert second.path == first.path


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

    row = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    assert row.status == "code_on_disk"
    # And the on-disk path actually exists — that's the whole
    # premise of the new status.
    assert Path(row.path).is_dir()

    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature")
    log = r.json()["log"]
    assert any("first-step-ok" in line for line in log)
    assert any("setup step 1 failed" in line for line in log)
    assert not any("should-not-run" in line for line in log)


def test_setup_step_resolves_mise_shim_over_system_path(
    _isolate: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A fake binary placed in ``$MISE_DATA_DIR/shims`` wins over the
    system PATH inside setup_steps. Proves the runner prepends mise's
    shims so worktree-pinned tool versions resolve without per-command
    ``mise exec --`` wrapping."""
    shims = tmp_path / "mise" / "shims"
    shims.mkdir(parents=True)
    fake_pnpm = shims / "pnpm"
    fake_pnpm.write_text('#!/bin/sh\necho "from-shim-pnpm $@"\n')
    fake_pnpm.chmod(0o755)
    monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "mise"))

    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repo_path,
        setup_steps=[{"cmd": "pnpm hello", "cwd": ""}],
    )

    row = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    assert row.status == "ready"
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature")
    log = r.json()["log"]
    assert any("from-shim-pnpm hello" in line for line in log), log


def test_setup_runs_when_mise_shims_dir_absent(
    _isolate: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When ``$MISE_DATA_DIR/shims`` doesn't exist (mise not installed),
    setup still succeeds — the runner silently no-ops the PATH prepend
    instead of crashing."""
    monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "no-mise-here"))

    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repo_path,
        setup_steps=[{"cmd": "echo setup-ran", "cwd": ""}],
    )

    row = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    assert row.status == "ready"
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/feature")
    log = r.json()["log"]
    assert any("setup-ran" in line for line in log), log


def test_missing_branch_marks_failed(_isolate: dict[str, Path]) -> None:
    """Pre-worktree-add failure (branch doesn't exist) → still
    `failed`. There's no usable code on disk."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    row = asyncio.run(wt_svc.create_and_wait("myapp", "nope-not-real"))
    assert row.status == "failed"
    assert not Path(row.path).is_dir()
    with TestClient(app) as client:
        r = client.get("/api/worktree/myapp/nope_not_real")
    detail = r.json()
    assert any("not found locally or on origin" in line for line in detail["log"])


# --- create_worktree idempotency contract --------------------------------


def test_create_idempotent_returns_existing_ready_row(
    _isolate: dict[str, Path],
) -> None:
    """Already-ready row + matching on-disk worktree: a second
    ``create_worktree`` call returns the existing row immediately
    without re-running any git step (no log lines from a second pass)."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    first = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    assert first.status == "ready"

    # Reset the in-memory log buffer so we can prove no git step ran
    # on the second call.
    wt_svc.reset_log("myapp", "feature")

    second = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    assert second.status == "ready"
    assert second.created_at == first.created_at
    # Empty log buffer → _create_worktree_async didn't run on the
    # second call.
    assert wt_svc.get_log("myapp", "feature") == []


def test_create_idempotent_resumes_setting_up_row(
    _isolate: dict[str, Path],
) -> None:
    """Pre-existing setting_up row + matching on-disk worktree
    (simulates a backend killed during setup_steps): retry should
    complete setup_steps and transition the row to ready without
    deleting/reinserting."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repo_path,
        setup_steps=[{"cmd": "echo resumed-setup", "cwd": ""}],
    )

    # Seed a partially-completed first attempt: row in setting_up,
    # on-disk git worktree already registered.
    target = _isolate["dev_root"] / "myapp_worktree_feature"
    import subprocess
    subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "add", str(target), "feature"],
        check=True,
        capture_output=True,
    )
    seed_worktree(
        _isolate["db_path"],
        "myapp",
        "feature",
        path=target,
        branch="feature",
        status="setting_up",
        mkdir=False,
    )
    seeded_created_at = wt_svc.get_worktree_sync(
        "myapp", "feature", db_path=_isolate["db_path"]
    )
    assert seeded_created_at is not None
    seeded_ts = seeded_created_at.created_at

    row = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    assert row.status == "ready"
    assert row.created_at == seeded_ts  # same row, not reinserted
    # Setup step actually re-ran.
    assert any("resumed-setup" in line for line in wt_svc.get_log("myapp", "feature"))


def test_create_raises_on_branch_mismatch(_isolate: dict[str, Path]) -> None:
    """An existing row with a different branch on the same short name is
    a genuine collision — surface it as ``WorktreeCreationError`` so the
    user picks a different name or deletes the existing row."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    seed_worktree(
        _isolate["db_path"],
        "myapp",
        "feature",
        branch="main",  # different from the requested "feature"
        status="ready",
    )
    with pytest.raises(wt_svc.WorktreeCreationError) as exc_info:
        asyncio.run(wt_svc.create_worktree("myapp", "feature"))
    msg = str(exc_info.value)
    assert "already exists" in msg
    assert "different branch" in msg


def test_create_recovers_stray_directory_at_target(
    _isolate: dict[str, Path],
) -> None:
    """No DB row but a stray (non-worktree) directory sits at the
    target path: ``create_worktree`` must remove it and proceed."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    target = _isolate["dev_root"] / "myapp_worktree_feature"
    target.mkdir()
    (target / "stale.txt").write_text("debris from a prior killed attempt\n")

    row = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    assert row.status == "ready"
    assert Path(row.path).is_dir()
    assert not (Path(row.path) / "stale.txt").exists()


# --- create_worktree background-task contract ----------------------------


def test_create_worktree_returns_setting_up_immediately(
    _isolate: dict[str, Path],
) -> None:
    """create_worktree returns as soon as the row is inserted —
    BEFORE the background task runs setup_steps. The returned row's
    status is `setting_up`, and the target directory hasn't been
    created yet."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repo_path,
        setup_steps=[{"cmd": "sleep 0.5 && echo slow-step", "cwd": ""}],
    )

    async def call_and_inspect() -> tuple[str, bool]:
        row = await wt_svc.create_worktree("myapp", "feature")
        # Inspect state BEFORE awaiting the background task. The
        # target directory should not yet exist (git worktree add
        # hasn't run) for a fresh-insert path.
        snapshot_status = row.status
        snapshot_path_exists = Path(row.path).exists()
        await wt_svc.wait_for_setup_complete("myapp", "feature")
        return snapshot_status, snapshot_path_exists

    snapshot_status, snapshot_path_exists = asyncio.run(call_and_inspect())
    assert snapshot_status == "setting_up"
    assert not snapshot_path_exists


def test_concurrent_create_worktree_returns_existing_task(
    _isolate: dict[str, Path],
) -> None:
    """Two concurrent create_worktree calls for the same (repo, name)
    only spawn one background task — the second call sees the
    in-flight task and returns the existing setting_up row. Pins the
    double-click race guard."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(
        _isolate["config_path"],
        _isolate["dev_root"],
        repo_path,
        setup_steps=[{"cmd": "sleep 0.3 && echo slow-step", "cwd": ""}],
    )

    async def race() -> tuple:
        # Kick off the first call, let it spawn the task, then fire
        # the second one immediately. Both should resolve before the
        # background work finishes.
        first_task = asyncio.create_task(
            wt_svc.create_worktree("myapp", "feature")
        )
        # Yield so first_task can insert + spawn its background task
        # before second_task evaluates the in-flight guard.
        await asyncio.sleep(0)
        second = await wt_svc.create_worktree("myapp", "feature")
        first = await first_task
        await wt_svc.wait_for_setup_complete("myapp", "feature")
        return first, second

    first, second = asyncio.run(race())
    assert first.created_at == second.created_at
    # Only one git-worktree-add log entry — the second call did not
    # spawn a duplicate task.
    log = wt_svc.get_log("myapp", "feature")
    add_entries = [line for line in log if "(worktree-add)" in line]
    assert len(add_entries) == 1, log


def test_create_worktree_background_failure_flips_to_failed(
    _isolate: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `_create_worktree_async` raises an unexpected exception, the
    wrapper flips the row to a recovery status (failed when the target
    path doesn't exist, code_on_disk when it does) instead of leaving
    the row stuck in setting_up forever."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(wt_svc, "_create_worktree_async", boom)

    row = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    # Target path doesn't exist (worktree add never ran) so the
    # wrapper routes to `failed`, matching the lifespan reconciler's
    # path-existence routing.
    assert row.status == "failed"
    assert ("myapp", "feature") not in wt_svc._setting_up_tasks


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

    seeded = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    assert seeded.status == "ready"
    with TestClient(app) as client:
        r = client.post("/api/worktree/myapp/feature/recreate")
    assert r.status_code == 409
    assert "stale" in r.json()["detail"]


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

    seeded = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    assert seeded.status == "code_on_disk"
    old_created_at = seeded.created_at
    # Recreate should be accepted (and will fail setup again, since we
    # didn't fix the failing step — but that's the user's problem,
    # not the endpoint's). Plan-67: the response returns as soon as
    # the fresh setting_up row is inserted; the eventual terminal
    # status (code_on_disk again, since the failing step is still
    # there) plays out in a background task that TestClient's
    # per-request event loop doesn't observe. Service-level setup
    # completion is covered separately by the create_and_wait tests.
    with TestClient(app) as client:
        r = client.post("/api/worktree/myapp/feature/recreate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "setting_up"
    assert body["created_at"] != old_created_at  # fresh row


def test_recreate_still_rejects_failed(_isolate: dict[str, Path]) -> None:
    """Recreate is not validated for genuinely-failed rows (no code on
    disk). Keep the rejection until that path is exercised."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    seeded = asyncio.run(wt_svc.create_and_wait("myapp", "nope-not-real"))
    assert seeded.status == "failed"
    with TestClient(app) as client:
        r = client.post("/api/worktree/myapp/nope_not_real/recreate")
    assert r.status_code == 409
    assert "code_on_disk" in r.json()["detail"]


def test_recreate_stale_row_drops_and_reinserts(_isolate: dict[str, Path]) -> None:
    """End-to-end: create a worktree, mark it stale in the DB to
    simulate "user deleted the directory outside CDH and ran Sync",
    then click Recreate. The row should be replaced with a fresh
    ready row pointing at the same branch."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    seeded = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    assert seeded.status == "ready"
    old_path = seeded.path
    old_created_at = seeded.created_at

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
    # same branch and return a fresh setting_up row. (Plan-67: setup
    # now runs in a background task; the response returns immediately
    # after insert. The eventual transition to ready plays out
    # asynchronously and isn't observable from TestClient — that
    # path is covered at the service level by the create_and_wait
    # tests above.)
    with TestClient(app) as client:
        r = client.post("/api/worktree/myapp/feature/recreate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "setting_up"
    assert body["branch"] == "feature"
    assert body["name"] == "feature"
    assert body["created_at"] != old_created_at  # fresh insert


# --- DELETE /api/worktree/{repo}/{name} ----------------------------------


def test_delete_404_when_worktree_missing(_isolate: dict[str, Path]) -> None:
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], _isolate["dev_root"])
    with TestClient(app) as client:
        r = client.delete("/api/worktree/myapp/nope")
    assert r.status_code == 404


def test_delete_happy_path_removes_row_and_directory(
    _isolate: dict[str, Path],
) -> None:
    """End-to-end: create a worktree, delete it, confirm the row is
    gone and the on-disk directory was removed."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    seeded = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    wt_path = Path(seeded.path)
    assert wt_path.exists()

    with TestClient(app) as client:
        r1 = client.delete("/api/worktree/myapp/feature")
        assert r1.status_code == 200, r1.text
        assert r1.json() == {"deleted": True}

        # Row dropped.
        r2 = client.get("/api/worktrees")
        assert r2.json()["worktrees"] == []

    # On-disk path is gone.
    assert not wt_path.exists()


def test_delete_gcs_orphaned_pr_row(_isolate: dict[str, Path]) -> None:
    """Deleting a worktree whose linked PR isn't bookmarked or noted
    GC's the now-unheld pr row — the worktree was the only thing holding
    it (the FK is ON DELETE SET NULL, so the row would otherwise leak)."""
    from app.services import pr_db

    write_repo_config(
        _isolate["config_path"], _isolate["dev_root"], _isolate["dev_root"]
    )
    seed_worktree(
        _isolate["db_path"], "myapp", "feature",
        branch="feature", pr_repo="acme/myapp", pr_number=7,
    )
    assert (
        pr_db.get_pr_sync("acme/myapp", 7, db_path=_isolate["db_path"])
        is not None
    )
    with TestClient(app) as client:
        r = client.delete("/api/worktree/myapp/feature")
    assert r.status_code == 200, r.text
    assert (
        pr_db.get_pr_sync("acme/myapp", 7, db_path=_isolate["db_path"]) is None
    )


def test_delete_preserves_bookmarked_pr_row(_isolate: dict[str, Path]) -> None:
    """A bookmarked PR survives worktree deletion — the bookmark holds
    the row (it re-renders as a non-local card)."""
    from app.services import pr_db

    write_repo_config(
        _isolate["config_path"], _isolate["dev_root"], _isolate["dev_root"]
    )
    seed_worktree(
        _isolate["db_path"], "myapp", "feature",
        branch="feature", pr_repo="acme/myapp", pr_number=8,
    )
    pr_db.set_bookmark_flag_sync(
        "acme/myapp", 8, True,
        bookmarked_at="2026-01-01T00:00:00Z", db_path=_isolate["db_path"],
    )
    with TestClient(app) as client:
        r = client.delete("/api/worktree/myapp/feature")
    assert r.status_code == 200, r.text
    pr = pr_db.get_pr_sync("acme/myapp", 8, db_path=_isolate["db_path"])
    assert pr is not None and pr.is_bookmarked is True


def test_delete_409_when_status_is_setting_up(
    _isolate: dict[str, Path],
) -> None:
    """Reject deletion of mid-flight setting_up rows — let the active
    create_worktree task finish or fail before the user retries.

    Seed the row INSIDE the TestClient context so the lifespan-time
    ``_reconcile_orphaned_setting_up`` doesn't flip it to failed/
    code_on_disk before the test runs."""
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], _isolate["dev_root"])
    with TestClient(app) as client:
        seed_worktree(
            _isolate["db_path"], "myapp", "feature",
            branch="feature",
            status="setting_up",
        )
        r = client.delete("/api/worktree/myapp/feature")
    assert r.status_code == 409
    assert "setting_up" in r.json()["detail"]


def test_delete_409_when_status_is_removing(_isolate: dict[str, Path]) -> None:
    """Another delete in flight; second click bounces."""
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], _isolate["dev_root"])
    seed_worktree(
        _isolate["db_path"], "myapp", "feature",
        branch="feature",
        status="removing",
    )
    with TestClient(app) as client:
        r = client.delete("/api/worktree/myapp/feature")
    assert r.status_code == 409
    assert "removing" in r.json()["detail"]


def test_delete_succeeds_when_path_already_gone(
    _isolate: dict[str, Path],
) -> None:
    """Stale-ish row (path vanished outside CDH). Delete should still
    drop the row — no git remove to attempt — and prune git's tracking."""
    repo_path = _isolate["dev_root"] / "myapp"
    _init_git_repo(repo_path)
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)

    seeded = asyncio.run(wt_svc.create_and_wait("myapp", "feature"))
    wt_path = Path(seeded.path)

    # User `rm -rf`d the directory outside CDH.
    import shutil
    shutil.rmtree(wt_path)
    assert not wt_path.exists()

    with TestClient(app) as client:
        r1 = client.delete("/api/worktree/myapp/feature")
        assert r1.status_code == 200, r1.text

        r2 = client.get("/api/worktrees")
        assert r2.json()["worktrees"] == []


def test_delete_drops_row_even_when_git_remove_fails(
    _isolate: dict[str, Path],
) -> None:
    """If git is upset (e.g., repo path config drifted) we still drop
    the row — the alternative is a stuck-in-removing row that forces
    the user into a terminal. Re-Sync can reconcile any orphaned git
    state later."""
    # Configure a repo whose path exists but isn't a git repo, so
    # `git worktree remove` will error.
    repo_path = _isolate["dev_root"] / "not-a-git-repo"
    repo_path.mkdir()
    write_repo_config(_isolate["config_path"], _isolate["dev_root"], repo_path)
    # Seed a worktree row pointing at a bogus path. We can't actually
    # create one via the API for a non-git repo, so seed directly.
    fake_wt = _isolate["dev_root"] / "myapp_feat"
    seed_worktree(
        _isolate["db_path"], "myapp", "feature",
        path=fake_wt,
        branch="feature",
        status="ready",
    )
    # Touch the path so the route's `wt_path.exists()` check triggers
    # the (doomed) git remove. seed_worktree may have created the dir
    # already, so tolerate that.
    fake_wt.mkdir(exist_ok=True)

    with TestClient(app) as client:
        r = client.delete("/api/worktree/myapp/feature")
    # Row dropped despite the failing git invocation.
    assert r.status_code == 200, r.text
    assert seed_worktree.__module__  # silence linter on unused-import
    from app.services.worktree import get_worktree_sync

    assert get_worktree_sync("myapp", "feature", db_path=_isolate["db_path"]) is None


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
