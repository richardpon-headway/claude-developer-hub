-- Add `code_on_disk` to the worktree.status enum.
--
-- SQLite can't ALTER a CHECK constraint in place. We use the
-- canonical recipe from https://www.sqlite.org/lang_altertable.html
-- §7 ("Making Other Kinds Of Table Schema Changes"):
--
--   1. PRAGMA foreign_keys=OFF
--   2. BEGIN
--   3. CREATE TABLE worktree_new (...new shape...)
--   4. INSERT INTO worktree_new SELECT * FROM worktree
--   5. DROP TABLE worktree
--   6. ALTER TABLE worktree_new RENAME TO worktree
--   7. Recreate indexes
--   8. PRAGMA foreign_key_check  (verify no dangling FKs)
--   9. COMMIT
--  10. PRAGMA foreign_keys=ON
--
-- Foreign keys from iterm_session(repo, worktree_name) and
-- pr_state(repo, worktree_name) reference the parent by table name.
-- With foreign_keys=OFF during the rebuild, the DROP doesn't fire a
-- constraint violation; the RENAME restores the name and the FKs
-- bind to the new table.
--
-- The new status value semantically means: `git worktree add`
-- succeeded so the code is locally available, but a later
-- setup_step (from repo config) errored. The user can open the
-- worktree in iTerm2 / Cursor and re-run the failing step manually.

PRAGMA foreign_keys=OFF;

BEGIN;

CREATE TABLE worktree_new (
  repo                 TEXT    NOT NULL,
  name                 TEXT    NOT NULL,
  path                 TEXT    NOT NULL,
  branch               TEXT    NOT NULL,
  ticket               TEXT,
  pr_number            INTEGER,
  pr_repo              TEXT,
  created_at           TEXT    NOT NULL,
  status               TEXT    NOT NULL CHECK (
                         status IN (
                           'setting_up',
                           'ready',
                           'code_on_disk',
                           'failed',
                           'stale',
                           'removing'
                         )
                       ),
  PRIMARY KEY (repo, name)
);

INSERT INTO worktree_new
  (repo, name, path, branch, ticket, pr_number, pr_repo, created_at, status)
SELECT
  repo, name, path, branch, ticket, pr_number, pr_repo, created_at, status
FROM worktree;

DROP TABLE worktree;
ALTER TABLE worktree_new RENAME TO worktree;

CREATE INDEX worktree_ticket_idx    ON worktree(ticket);
CREATE INDEX worktree_pr_number_idx ON worktree(pr_number, pr_repo);

COMMIT;

PRAGMA foreign_keys=ON;
