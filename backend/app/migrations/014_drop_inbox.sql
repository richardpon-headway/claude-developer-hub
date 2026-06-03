-- Remove the inbox feature. Drop the inbox/archive columns from the
-- unified `pr` table: is_inbox, is_archived, inbox_added_at,
-- archived_at, inbox_sources. Bookmarks, authored-PR discovery, and
-- worktrees are unaffected.
--
-- Copy-preserving by default (per `[[project_cdh_migration_data_
-- preservation]]`): every row carrying USER INPUT or LOCAL STATE
-- survives the rebuild —
--   * bookmarked rows (is_bookmarked=1),
--   * rows with a user note (notes IS NOT NULL),
--   * rows attached to a local worktree.
-- Rows that existed ONLY because of the inbox (someone else's PR that
-- was never bookmarked/noted/pulled-down) are dropped — there's no
-- surface left to show them. Authored-discovery rows that match none
-- of the keep conditions are also dropped; the authored poll
-- re-discovers them on its next tick. This is exactly the keep-set the
-- post-change `maybe_gc_sync` enforces.
--
-- FK handling mirrors the proven 012/013 recipe: with foreign_keys=OFF
-- we create `pr_new`, copy the keepers, drop the old `pr`, and rename
-- `pr_new` -> `pr`. `worktree` and `pr_state` reference `pr` by name
-- and nothing references `pr_new`, so their FKs stay pointed at `pr`
-- and resolve again after the rename. The DROP doesn't fire the
-- pr_state ON DELETE CASCADE (FKs are off), so we explicitly sweep any
-- pr_state row whose PR didn't survive the copy.
--
-- The `DROP TABLE IF EXISTS pr_new` guards a re-run after a crash
-- between executescript's implicit COMMIT and the migration runner's
-- `_migration` INSERT.

PRAGMA foreign_keys=OFF;

BEGIN;

DROP TABLE IF EXISTS pr_new;

CREATE TABLE pr_new (
  pr_repo            TEXT    NOT NULL,
  pr_number          INTEGER NOT NULL,
  is_bookmarked      INTEGER NOT NULL DEFAULT 0,
  bookmarked_at      TEXT,
  title              TEXT,
  author_login       TEXT,
  url                TEXT,
  ticket             TEXT,
  state              TEXT CHECK (
                       state IS NULL OR state IN ('open','closed','merged')
                     ),
  is_draft           INTEGER NOT NULL DEFAULT 0,
  ci_status          TEXT CHECK (
                       ci_status IS NULL OR
                       ci_status IN ('pass','fail','pending','none')
                     ),
  pr_updated_at      TEXT,
  notes              TEXT,
  last_seen_at       TEXT,
  last_refreshed_at  TEXT,
  PRIMARY KEY (pr_repo, pr_number)
);

INSERT INTO pr_new (
  pr_repo, pr_number, is_bookmarked, bookmarked_at,
  title, author_login, url, ticket, state, is_draft, ci_status,
  pr_updated_at, notes, last_seen_at, last_refreshed_at
)
SELECT
  pr_repo, pr_number, is_bookmarked, bookmarked_at,
  title, author_login, url, ticket, state, is_draft, ci_status,
  pr_updated_at, notes, last_seen_at, last_refreshed_at
FROM pr
WHERE is_bookmarked = 1
   OR notes IS NOT NULL
   OR EXISTS (
     SELECT 1 FROM worktree w
     WHERE w.pr_repo = pr.pr_repo AND w.pr_number = pr.pr_number
   );

DROP TABLE pr;
ALTER TABLE pr_new RENAME TO pr;

-- Drop pr_state rows orphaned by the copy (their PR wasn't a keeper).
-- The ON DELETE CASCADE didn't fire because foreign_keys is OFF.
DELETE FROM pr_state
  WHERE NOT EXISTS (
    SELECT 1 FROM pr
    WHERE pr.pr_repo = pr_state.pr_repo AND pr.pr_number = pr_state.pr_number
  );

-- Recreate the surviving pr indexes (the inbox/archive ones vanished
-- with the old table).
CREATE INDEX pr_bookmarked_idx       ON pr(is_bookmarked);
CREATE INDEX pr_author_login_idx     ON pr(author_login);
CREATE INDEX pr_updated_at_idx       ON pr(pr_updated_at DESC);

COMMIT;

PRAGMA foreign_keys=ON;
