-- Repair terminal_session's FK target after migration 005's broken first
-- iteration.
--
-- This is the same bug migration 006 fixed for `pr_state`: an in-flight
-- iteration of 005 (since corrected, never released) used
-- `ALTER TABLE worktree RENAME TO worktree_old_005` WITHOUT disabling
-- FK enforcement first. That iteration ran against any developer DB
-- whose backend restarted while the broken file was on disk.
--
-- Consequence: SQLite auto-rewrote the iterm_session FK from
-- `REFERENCES worktree` to `REFERENCES worktree_old_005`, then the
-- subsequent `DROP TABLE worktree_old_005` fired ON DELETE CASCADE
-- (well, actually it just left a dangling reference). Migration 011
-- renamed iterm_session -> terminal_session but the dangling FK target
-- name traveled with it, so any INSERT now blows up with
-- "no such table: worktree_old_005".
--
-- Migration 006 repaired pr_state but never touched iterm_session.
-- We do the same dance here for terminal_session.
--
-- Idempotent against well-formed DBs: the rebuild produces the same
-- shape regardless of whether the existing FK is broken or correct;
-- existing rows survive the copy.

PRAGMA foreign_keys=OFF;

BEGIN;

CREATE TABLE terminal_session_new (
  repo                 TEXT    NOT NULL,
  worktree_name        TEXT    NOT NULL,
  role                 TEXT    NOT NULL CHECK (role IN ('claude','shell')),
  terminal_kind        TEXT    NOT NULL DEFAULT 'iterm2',
  window_id            TEXT    NOT NULL,
  session_id           TEXT    NOT NULL,
  claude_session_uuid  TEXT,
  spawned_at           TEXT    NOT NULL,
  PRIMARY KEY (repo, worktree_name, role),
  FOREIGN KEY (repo, worktree_name)
    REFERENCES worktree(repo, name)
    ON DELETE CASCADE
);

INSERT INTO terminal_session_new
  (repo, worktree_name, role, terminal_kind, window_id, session_id,
   claude_session_uuid, spawned_at)
SELECT
  repo, worktree_name, role, terminal_kind, window_id, session_id,
  claude_session_uuid, spawned_at
FROM terminal_session;

DROP INDEX IF EXISTS terminal_session_id_idx;
DROP TABLE terminal_session;
ALTER TABLE terminal_session_new RENAME TO terminal_session;

CREATE INDEX terminal_session_id_idx ON terminal_session(session_id);

COMMIT;

PRAGMA foreign_keys=ON;
