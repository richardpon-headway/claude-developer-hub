# Claude Developer Hub (CDH)

A localhost web app that orchestrates git worktrees, spawns iTerm2 windows
with Claude Code pre-launched, and surfaces PRs, Jira tickets, and token
usage in one hub.

The orchestration layer is the moat. Existing tools cover individual pieces
(gh-dash for PRs, lazyworktree for worktrees, iTerm2 for terminals, your
favourite token monitor for usage). CDH ties ticket → worktree → Claude
session → PR → CI → merge in one place.

> **Status: early adopter.** The end-to-end ticket → worktree → Claude
> session → PR → merge flow works today; rough edges and missing affordances
> remain (see the issue tracker). Public adoption-ready polish (full README
> walkthrough with screenshots, error messages for every failure mode) is
> still in progress.

## Screenshots and demo

_(Coming once the hub renders something interesting.)_

## Requirements

Hard prereqs:

- macOS (iTerm2 is Mac-only)
- iTerm2 ≥ 3.5 with the Python API enabled
  (Preferences → Magic → Enable Python API; one-time auth dialog approval
  on the first connection)
- Python 3.13+
- Node 20+
- `gh` CLI, authenticated (`gh auth status` succeeds)
- Claude Code installed and authorized — onboarding leans on an existing
  Claude session in your terminal
- `mise`, `uv`, `pnpm`, `jq` on your `PATH`

Optional:

- A token-usage monitor on `localhost:47821` (CDH's hub tile reads from it
  if present; it is otherwise rendered as a small "offline" badge)
- `jira-cli` or `acli` on `PATH` if you want Jira features

## Quickstart

```sh
# from the repo root
make install
make run
```

`make run` starts:

- the FastAPI backend on `http://127.0.0.1:47823`
- the Vite dev server on `http://127.0.0.1:5174` (proxies `/api` → `:47823`)

Database migrations run automatically on backend startup. There is no
separate `make migrate` step.

Open `http://localhost:5174/` (dev) or `http://localhost:47823/` (after
`make build-ui`).

> CDH's Vite intentionally binds `5174`, not Vite's default `5173`, so it
> can run side-by-side with other Vite projects (e.g. claude-token-monitor,
> which uses `5173` / backend `47821`). `strictPort: true` makes CDH fail
> loudly if `5174` is also occupied rather than silently shifting to
> another port.

## Configuration

User-local settings live at `~/.config/cdh/config.yaml`. The primary path
for adding a repo is the UI:

1. Click "Add a repo" in the hub.
2. Paste the absolute path to your repo.
3. CDH hands a structured prompt to Claude Code; Claude inspects the repo
   and proposes a config entry (setup commands, branch prefix, ticket
   pattern, etc.).
4. Review the proposed entry in the UI. Edit any field inline. Save.

The codebase ships with no built-in heuristics for "if `package.json`
exists, run `pnpm install`" — those rules live in Claude's onboarding
prompt, so the inspector adapts to whatever conventions a repo uses
without code changes.

**Hand-editing the YAML.** Several knobs aren't exposed through the UI
yet and require editing `~/.config/cdh/config.yaml` directly:

- `polling` — pr_state / inbox poll intervals (raise these if you're
  hitting GitHub's 5000/hr GraphQL quota)
- `inbox.teams` — `owner/team-slug`s whose review-requested PRs should
  surface in the hub inbox
- `global_skills` / `workspace_skills` — custom Claude slash-command
  buttons on the hub or workspace pages
- `jira` — Jira tool selection and JQL for the assigned-tickets panel
- `iterm2.default_window` — frame coords for spawned iTerm2 windows
- `iterm2.send_gate_patterns` — regex list; CDH refuses to programmatically
  send text to a Claude session when the last visible screen line matches
  any pattern (default catches `[y/N]` confirmation prompts so a skill
  button can't accidentally answer "y")

See [`config.example.yaml`](config.example.yaml) for the full shape with
every block's defaults, and
[`backend/app/config/schema.py`](backend/app/config/schema.py) for the
authoritative Pydantic schema (unknown keys are rejected). All defaults
are generic — no Headway-specific or user-specific data is baked into
the codebase.

If your repo lives under `development_root` (default `~/development`),
it'll appear in the "Add a repo" modal as a clickable card; otherwise
paste the absolute path into the manual input below the list. Worktrees
are excluded from the list (use the "Discover worktrees" button on the
hub once the parent repo is added).

## Troubleshooting

- **iTerm2 spawn buttons return HTTP 503.** Check the iTerm2 Python API
  is enabled and the first-connection auth dialog has been approved.
- **Hub shows "gh not authenticated".** Run `gh auth login`.
- **Token tile shows "offline".** The optional token-usage monitor is
  not running on `localhost:47821`. CDH continues to work without it.

## Architecture (high level)

- **Backend**: FastAPI + Python 3.13 (uv-managed), port `47823`, bound to
  `127.0.0.1`.
- **Frontend**: React 19 + Vite 6 + TypeScript + Tailwind v4 + TanStack
  Router + TanStack Query.
- **Persistence**: SQLite at `~/Library/Application Support/cdh/cdh.db`.
- **iTerm2 control**: the official `iterm2` Python package (not
  AppleScript), driven by a long-lived asyncio supervisor.
- **No auth**: localhost-only, single user.

URL routing:

| URL                              | Page                                           |
| -------------------------------- | ---------------------------------------------- |
| `/`                              | Hub: workspaces, PRs, Jira, token-usage tile   |
| `/workspace/<repo>/<name>`       | Workspace control panel                        |
| `/pr/<owner>/<repo>/<num>`       | PR review (rich)                               |
| `/jira/<key>`                    | Ticket entry point (thin)                      |

Visiting a URL is pure rendering. Spawning iTerm2, launching Cursor, or
creating a worktree from a PR all require explicit button clicks.

## Importing existing worktrees

CDH normally only tracks worktrees it created itself. If you have a
backlog of worktrees on disk from another tool (vanilla `git worktree
add`, `lazyworktree`, a personal skill), click **Discover worktrees**
on the hub after configuring the parent repo. CDH will run
`git worktree list --porcelain` for each configured repo and ingest
every worktree it finds, skipping main checkouts, already-tracked
paths, name collisions, and detached-HEAD worktrees.

**Detached-HEAD worktrees are intentionally skipped.** These show up
when you `gh pr checkout` without an explicit branch. CDH's downstream
features (ticket extraction from branch name, skill-runner button
labels, sidecar `worktree` field) all assume a branch is present, so
importing detached-HEAD worktrees would create rows that misbehave in
subtle ways. The Discover summary reports them in the skipped count
with reason `detached HEAD`; if you want one in CDH, check out a
branch on the worktree first, then re-run Discover.

## Optional: `cdh` shell function

A tiny shell function lets you jump from any terminal into the right
hub URL without typing it. Drop this into your `~/.zshrc` or
`~/.bashrc`:

```bash
cdh() {
  if [ $# -eq 0 ]; then
    local cwd target
    cwd=$(pwd -P)
    target=$(curl -s --max-time 2 \
      "http://localhost:47823/api/workspace/from-path?path=$cwd" 2>/dev/null \
      | jq -r '.url // "/"' 2>/dev/null)
    open "http://localhost:47823${target:-/}"
  else
    case "$1" in
      /*) open "http://localhost:47823$1" ;;
      *)  open "http://localhost:47823/$1" ;;
    esac
  fi
}
```

Behaviour:

- `cdh` (no args) — read the cwd, ask the backend which workspace lives
  here, open that URL. Falls back to the hub if no match (so dropping
  into any directory still opens something useful).
- `cdh /workspace/foo/bar` — open that explicit URL.
- `cdh hub` — open `/hub`.

Requires `jq` on `PATH` and CDH already running (`make run`). If the
backend is unreachable, the function falls back to opening the hub URL
directly; you'll see the connection error in the browser.

A fish-shell variant uses the same backend endpoint but different syntax:

```fish
function cdh
  if test (count $argv) -eq 0
    set -l cwd (pwd -P)
    set -l target (curl -s --max-time 2 \
      "http://localhost:47823/api/workspace/from-path?path=$cwd" 2>/dev/null \
      | jq -r '.url // "/"' 2>/dev/null)
    test -z "$target"; and set target "/"
    open "http://localhost:47823$target"
  else if string match -q -- '/*' $argv[1]
    open "http://localhost:47823$argv[1]"
  else
    open "http://localhost:47823/$argv[1]"
  end
end
```

CDH never auto-modifies your shell rc files. Copy-paste install is the
only supported path right now; an opt-in auto-installer (with backup at
`~/.zshrc.cdh-backup-<timestamp>`) may land later.

## Privacy and telemetry

**No telemetry, no analytics, no phone-home.** The only network calls CDH
makes are to:

- `gh` (your GitHub auth, your traffic)
- `jira-cli` / `acli` (your Jira auth, your traffic)
- `localhost:47821` (your local token-usage monitor; only if you run one)

CDH never transmits config, session data, code, or sidecar contents to
any external service. SECURITY.md restates this guarantee.

State written to your machine:

- `~/Library/Application Support/cdh/cdh.db` (SQLite state)
- `~/.config/cdh/config.yaml` (user config; only when you onboard a repo)
- `~/.cache/<token-monitor>/session-meta/<uuid>.json` (sidecars; only when
  you spawn a workspace)
- `~/.zshrc` (only if you opt into auto-install of the `cdh` shell
  function; a backup is saved at `~/.zshrc.cdh-backup-<timestamp>`)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The project follows the
[Contributor Covenant](CODE_OF_CONDUCT.md).

## License

See [LICENSE](LICENSE).
