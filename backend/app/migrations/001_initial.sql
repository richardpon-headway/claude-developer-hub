-- Initial schema (plan §9). Tracks worktrees, the iTerm2 sessions they're
-- bound to, and a small key/value table for iTerm2 lifecycle probes
-- (e.g., the "iterm2_started_at" value the supervisor compares against
-- on reconnect to detect a restart).
--
-- The _migration table is created by the runner before this script
-- executes; it is not part of any migration file.

BEGIN;

CREATE TABLE worktree (
  repo                 TEXT    NOT NULL,
  name                 TEXT    NOT NULL,
  path                 TEXT    NOT NULL,
  branch               TEXT    NOT NULL,
  ticket               TEXT,
  pr_number            INTEGER,
  pr_repo              TEXT,
  created_at           TEXT    NOT NULL,
  status               TEXT    NOT NULL CHECK (
                         status IN ('setting_up','ready','failed','stale','removing')
                       ),
  PRIMARY KEY (repo, name)
);
CREATE INDEX worktree_ticket_idx    ON worktree(ticket);
CREATE INDEX worktree_pr_number_idx ON worktree(pr_number, pr_repo);

CREATE TABLE iterm_session (
  repo                 TEXT    NOT NULL,
  worktree_name        TEXT    NOT NULL,
  role                 TEXT    NOT NULL CHECK (role IN ('claude','shell')),
  iterm_window_id      TEXT    NOT NULL,
  iterm_session_id     TEXT    NOT NULL,
  claude_session_uuid  TEXT,
  spawned_at           TEXT    NOT NULL,
  PRIMARY KEY (repo, worktree_name, role),
  FOREIGN KEY (repo, worktree_name)
    REFERENCES worktree(repo, name)
    ON DELETE CASCADE
);
CREATE INDEX iterm_session_id_idx ON iterm_session(iterm_session_id);

CREATE TABLE iterm_lifecycle (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

COMMIT;
