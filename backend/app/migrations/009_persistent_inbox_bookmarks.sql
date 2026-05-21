-- Persistent inbox + sticky archive + bookmarks.
--
-- Replaces the ephemeral in-memory inbox cache with a SQLite table.
-- Adds a separate `bookmark` table for explicit manual watches, and
-- `inbox_archived` to record user-dismissed PRs so they don't re-enter
-- the inbox on the next poll tick.
--
-- All three tables key on (pr_repo, pr_number), where `pr_repo` is the
-- GitHub "owner/name" pair. These rows can reference upstream repos
-- CDH isn't locally configured against, so we don't FK into the
-- `worktree` table (which keys on the locally-configured repo name).

BEGIN;

CREATE TABLE inbox (
  pr_repo            TEXT    NOT NULL,
  pr_number          INTEGER NOT NULL,
  title              TEXT    NOT NULL,
  author_login       TEXT    NOT NULL,
  url                TEXT    NOT NULL,
  is_draft           INTEGER NOT NULL DEFAULT 0,
  ci_status          TEXT    NOT NULL CHECK (ci_status IN ('pass','fail','pending','none')),
  -- JSON array, priority-ordered. Same shape as the prior ephemeral
  -- InboxPr.sources (e.g. ["reviewer","assignee"]). The frontend reads
  -- sources[0] to pick the primary chip on the row.
  sources            TEXT    NOT NULL,
  -- Free-form per-row notes. Same UX as worktree.notes; NULL when
  -- the user hasn't typed anything.
  notes              TEXT,
  -- Extracted ticket id (e.g. "PROJ-123"), when one of the configured
  -- repos' ticket_pattern matches the head_ref. NULL when nothing
  -- matched. Used for the Jira link on the row.
  ticket             TEXT,
  -- PR updatedAt from `gh search prs`. Drives row sort order.
  pr_updated_at      TEXT    NOT NULL,
  -- First time this PR landed in the inbox. Never updated.
  added_at           TEXT    NOT NULL,
  -- Most recent tick where `gh search prs` returned this row. Used by
  -- the auto-removal sweep to rate-limit the `gh pr view` per-row
  -- state probe (rows last seen this tick don't need probing).
  last_seen_at       TEXT    NOT NULL,
  PRIMARY KEY (pr_repo, pr_number)
);

-- Ordering index: hub paints rows newest-first. Used by the inbox
-- list query.
CREATE INDEX inbox_updated_idx ON inbox(pr_updated_at DESC);

-- Sticky-dismissal storage. Presence of a row blocks re-discovery
-- from the auto-watch poll. The user never sees this directly;
-- they trigger it via the "Remove from inbox" button.
CREATE TABLE inbox_archived (
  pr_repo      TEXT    NOT NULL,
  pr_number    INTEGER NOT NULL,
  archived_at  TEXT    NOT NULL,
  PRIMARY KEY (pr_repo, pr_number)
);

-- Manual watch list. Distinct from `inbox` because bookmarks have a
-- different lifecycle: never auto-removed on close/merge (the user's
-- explicit pin overrides the PR's terminal state until they
-- unbookmark). Cached PR metadata (title, author, state) is refreshed
-- periodically by a background task so the card stays current; the
-- ROW stays until manual deletion.
CREATE TABLE bookmark (
  pr_repo            TEXT    NOT NULL,
  pr_number          INTEGER NOT NULL,
  title              TEXT    NOT NULL,
  author_login       TEXT    NOT NULL,
  url                TEXT    NOT NULL,
  state              TEXT    NOT NULL CHECK (state IN ('open','closed','merged')),
  notes              TEXT,
  ticket             TEXT,
  bookmarked_at      TEXT    NOT NULL,
  last_refreshed_at  TEXT    NOT NULL,
  PRIMARY KEY (pr_repo, pr_number)
);

COMMIT;
