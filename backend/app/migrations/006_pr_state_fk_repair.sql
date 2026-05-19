-- Repair pr_state's FK target after migration 005's broken first iteration.
--
-- The shipped migration 005 (PR #71) rebuilds the `worktree` table using
-- the SQLite-recommended dance with `PRAGMA foreign_keys=OFF`. An earlier
-- in-flight iteration of 005 (since corrected, never released) used a
-- different approach — `ALTER TABLE worktree RENAME TO worktree_old_005`
-- WITHOUT disabling FK enforcement first. That iteration ran against any
-- developer DB whose backend restarted while the broken file was on disk.
--
-- Consequence on those DBs: SQLite auto-rewrote pr_state's FK from
-- `REFERENCES worktree` to `REFERENCES worktree_old_005`, then the
-- subsequent `DROP TABLE worktree_old_005` fired ON DELETE CASCADE and
-- wiped every pr_state row. The pr_state table's FK target name is now
-- a dangling reference; new inserts fail with "no such table:
-- worktree_old_005".
--
-- This migration is idempotent against well-formed DBs: it unconditionally
-- rebuilds pr_state with the correct FK target. On DBs that never hit the
-- broken iteration (i.e. the FK was already pointing at `worktree`), the
-- rebuild is a no-op shape-wise; existing rows survive the copy.

PRAGMA foreign_keys=OFF;

BEGIN;

CREATE TABLE pr_state_new (
  repo          TEXT NOT NULL,
  worktree_name TEXT NOT NULL,
  headline      TEXT NOT NULL CHECK (headline IN (
    'no_pr',
    'merged',
    'closed',
    'ci_failing',
    'merge_conflicts',
    'in_merge_queue',
    'ready_to_merge',
    'unresolved_comments',
    'human_comment',
    'review_requested',
    'checks_running',
    'waiting_on_others',
    'draft'
  )),
  payload       TEXT NOT NULL,
  checked_at    TEXT NOT NULL,
  PRIMARY KEY (repo, worktree_name),
  FOREIGN KEY (repo, worktree_name)
    REFERENCES worktree(repo, name)
    ON DELETE CASCADE
);

INSERT INTO pr_state_new
  (repo, worktree_name, headline, payload, checked_at)
SELECT
  repo, worktree_name, headline, payload, checked_at
FROM pr_state;

DROP TABLE pr_state;
ALTER TABLE pr_state_new RENAME TO pr_state;

CREATE INDEX pr_state_headline_idx ON pr_state(headline);

COMMIT;

PRAGMA foreign_keys=ON;
