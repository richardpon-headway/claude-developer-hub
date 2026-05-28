-- Unify the four PR-keyed surfaces (bookmark, inbox, inbox_archived,
-- authored_pr_notes) into a single `pr` table keyed by GitHub identity
-- (pr_repo, pr_number); rekey `pr_state` to the same identity; drop
-- `worktree.pr_author_login` (the field stays on WorktreeRow but is
-- projected via JOIN to `pr.author_login` at read time).
--
-- Why one table: today a single PR that is bookmarked AND has a
-- worktree appears in two places with two row identities and two
-- independently-polled state caches. Origin booleans (is_bookmarked,
-- is_inbox, is_archived) + per-origin timestamps let us collapse the
-- four parallel surfaces into a single row that the shim layer
-- projects per consumer in plan-59, and that routes will consume
-- directly once plan-61 lands.
--
-- Copy-preserving by default (per `[[project_cdh_migration_data_
-- preservation]]`): every bookmark note, inbox note, inbox-archive
-- dismissal, authored note, and worktree-attached PR carries forward.
-- User input is never thrown away.
--
-- The defensive `DROP TABLE IF EXISTS pr; DROP TABLE IF EXISTS
-- pr_state_new; DROP TABLE IF EXISTS worktree_new;` at the top of
-- the transaction guards re-run after a crash between executescript's
-- implicit COMMIT and the migration runner's `_migration` INSERT —
-- see the comment block at `backend/app/db.py:147-149`.

PRAGMA foreign_keys=OFF;

BEGIN;

DROP TABLE IF EXISTS pr;
DROP TABLE IF EXISTS pr_state_new;
DROP TABLE IF EXISTS worktree_new;

CREATE TABLE pr (
  pr_repo            TEXT    NOT NULL,
  pr_number          INTEGER NOT NULL,
  -- Origin flags. Multiple may be true (e.g. bookmarked AND in the
  -- inbox); the shim layer filters per surface.
  is_bookmarked      INTEGER NOT NULL DEFAULT 0,
  is_inbox           INTEGER NOT NULL DEFAULT 0,
  is_archived        INTEGER NOT NULL DEFAULT 0,
  -- Per-origin timestamps. NULL when the corresponding flag is 0.
  bookmarked_at      TEXT,
  inbox_added_at     TEXT,
  archived_at        TEXT,
  -- JSON array of inbox source-priority strings (e.g. ["reviewer"]).
  -- NULL when is_inbox=0.
  inbox_sources      TEXT,
  -- PR metadata. NULL until at least one source has populated.
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
  -- User-owned notes. Per-surface uniqueness is gone (intended — one
  -- PR, one note). Merged at fold time so nothing the user typed is
  -- lost.
  notes              TEXT,
  -- Freshness markers. `last_seen_at` is bumped on inbox discovery
  -- ticks. `last_refreshed_at` is bumped on bookmark `gh pr view`
  -- probes — same semantic as the legacy bookmark column.
  last_seen_at       TEXT,
  last_refreshed_at  TEXT,
  PRIMARY KEY (pr_repo, pr_number)
);

-- Source-ordered folds. Each ON CONFLICT COALESCEs around the
-- highest-priority surface for that field. Ordering matters: inbox
-- writes title/author/url/sources/ci first, bookmark annotates with
-- its own state + refresh marker, inbox_archived sets archive,
-- worktree contributes author_login when neither inbox nor bookmark
-- carried it, and authored_pr_notes preserves notes-only rows.

INSERT INTO pr (
  pr_repo, pr_number, is_inbox, inbox_added_at, inbox_sources,
  title, author_login, url, is_draft, ci_status, notes, ticket,
  pr_updated_at, last_seen_at
)
SELECT
  pr_repo, pr_number, 1, added_at, sources,
  title, author_login, url, is_draft, ci_status, notes, ticket,
  pr_updated_at, last_seen_at
FROM inbox;

INSERT INTO pr (
  pr_repo, pr_number, is_bookmarked, bookmarked_at,
  title, author_login, url, state, notes, ticket, last_refreshed_at
)
SELECT
  pr_repo, pr_number, 1, bookmarked_at,
  title, author_login, url, state, notes, ticket, last_refreshed_at
FROM bookmark
WHERE true
ON CONFLICT(pr_repo, pr_number) DO UPDATE SET
  is_bookmarked     = 1,
  bookmarked_at     = excluded.bookmarked_at,
  -- Bookmark is the only source for `state` and `last_refreshed_at`;
  -- always overwrite.
  state             = excluded.state,
  last_refreshed_at = excluded.last_refreshed_at,
  -- Bookmark notes win when present (the bookmark add typically
  -- happened after inbox-discovery, so the bookmark note is the
  -- newer one). Falls back to the inbox-set value when NULL.
  notes             = COALESCE(excluded.notes, pr.notes),
  ticket            = COALESCE(pr.ticket, excluded.ticket);

INSERT INTO pr (pr_repo, pr_number, is_archived, archived_at)
SELECT pr_repo, pr_number, 1, archived_at
FROM inbox_archived
WHERE true
ON CONFLICT(pr_repo, pr_number) DO UPDATE SET
  is_archived  = 1,
  archived_at  = excluded.archived_at;

-- Worktree-attached PRs that no other surface tracked — fold these in
-- explicitly so the worktree rebuild's FK to pr resolves. Only
-- author_login carries over; worktree.notes is per-checkout (not
-- per-PR) and stays on the worktree row.
INSERT INTO pr (pr_repo, pr_number, author_login)
SELECT pr_repo, pr_number, pr_author_login
FROM worktree
WHERE pr_repo IS NOT NULL AND pr_number IS NOT NULL
ON CONFLICT(pr_repo, pr_number) DO UPDATE SET
  author_login = COALESCE(pr.author_login, excluded.author_login);

INSERT INTO pr (pr_repo, pr_number, notes, last_refreshed_at)
SELECT pr_repo, pr_number, notes, updated_at
FROM authored_pr_notes
WHERE true
ON CONFLICT(pr_repo, pr_number) DO UPDATE SET
  notes             = COALESCE(pr.notes, excluded.notes),
  last_refreshed_at = COALESCE(pr.last_refreshed_at, excluded.last_refreshed_at);

-- Rekey pr_state from (repo, worktree_name) to (pr_repo, pr_number)
-- via the worktree JOIN. Worktrees with no attached PR drop their
-- pr_state row — pr_state_poll re-discovers if a PR opens later.
-- INSERT OR IGNORE handles the rare case of two worktrees mapping to
-- the same PR (last-fold-wins is acceptable; both rows describe the
-- same upstream PR).

CREATE TABLE pr_state_new (
  pr_repo       TEXT    NOT NULL,
  pr_number     INTEGER NOT NULL,
  headline      TEXT    NOT NULL CHECK (headline IN (
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
  PRIMARY KEY (pr_repo, pr_number),
  FOREIGN KEY (pr_repo, pr_number)
    REFERENCES pr(pr_repo, pr_number)
    ON DELETE CASCADE
);

INSERT OR IGNORE INTO pr_state_new
  (pr_repo, pr_number, headline, payload, checked_at)
SELECT
  w.pr_repo, w.pr_number, ps.headline, ps.payload, ps.checked_at
FROM pr_state ps
JOIN worktree w ON ps.repo = w.repo AND ps.worktree_name = w.name
WHERE w.pr_repo IS NOT NULL AND w.pr_number IS NOT NULL;

-- Rebuild worktree: drop pr_author_login (projected at read time);
-- add nullable FK on (pr_repo, pr_number) to pr ON DELETE SET NULL.
-- The shim layer is expected to never delete a worktree-attached pr
-- row, but SET NULL is the safe fallback if it ever happens.

CREATE TABLE worktree_new (
  repo                 TEXT    NOT NULL,
  name                 TEXT    NOT NULL,
  path                 TEXT    NOT NULL,
  branch               TEXT    NOT NULL,
  ticket               TEXT,
  pr_number            INTEGER,
  pr_repo              TEXT,
  notes                TEXT,
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
  PRIMARY KEY (repo, name),
  FOREIGN KEY (pr_repo, pr_number)
    REFERENCES pr(pr_repo, pr_number)
    ON DELETE SET NULL
);

INSERT INTO worktree_new
  (repo, name, path, branch, ticket, pr_number, pr_repo, notes,
   created_at, status)
SELECT
  repo, name, path, branch, ticket, pr_number, pr_repo, notes,
  created_at, status
FROM worktree;

-- Drop the legacy tables and rename the rebuilt ones. The old
-- pr_state's FK was to worktree; with foreign_keys=OFF the DROP
-- doesn't trigger cascades. terminal_session's FK to worktree
-- survives the rebuild via the same recipe used in 005/006/012.

DROP TABLE pr_state;
DROP TABLE inbox;
DROP TABLE bookmark;
DROP TABLE inbox_archived;
DROP TABLE authored_pr_notes;
DROP TABLE worktree;

ALTER TABLE pr_state_new RENAME TO pr_state;
ALTER TABLE worktree_new RENAME TO worktree;

CREATE INDEX worktree_ticket_idx     ON worktree(ticket);
CREATE INDEX worktree_pr_idx         ON worktree(pr_repo, pr_number);
CREATE INDEX pr_state_headline_idx   ON pr_state(headline);
CREATE INDEX pr_state_checked_at_idx ON pr_state(checked_at);
CREATE INDEX pr_inbox_idx            ON pr(is_inbox);
CREATE INDEX pr_bookmarked_idx       ON pr(is_bookmarked);
CREATE INDEX pr_archived_idx         ON pr(is_archived);
CREATE INDEX pr_author_login_idx     ON pr(author_login);
CREATE INDEX pr_updated_at_idx       ON pr(pr_updated_at DESC);

COMMIT;

PRAGMA foreign_keys=ON;
