-- Add pr_author_login to the worktree table.
--
-- The hub uses this to split "my work" from "PRs I'm reviewing" without
-- re-querying gh on every list-worktrees call. The column is populated by
-- two paths:
--   1. Inbox pull-down — writes author_login captured from the InboxPr
--      cache entry at the moment of pull-down.
--   2. pr_state polling — lazy-fills the column from gh pr view's author
--      field on the next tick, so worktrees created before this column
--      existed still get a value without a manual backfill.
--
-- NULL means "not yet known". The frontend treats NULL as owner-by-
-- default, which keeps legacy rows behaving exactly as they did before
-- this migration landed.
--
-- SQLite supports ALTER TABLE ADD COLUMN directly for nullable columns
-- with no default, so no table rebuild is needed.

ALTER TABLE worktree ADD COLUMN pr_author_login TEXT;
