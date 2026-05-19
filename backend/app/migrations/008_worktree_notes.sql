-- Add a free-form notes column to the worktree table.
--
-- Each worktree gets a single TEXT slot for the user's own
-- annotations — "blocking COR-218, don't archive", "sent review
-- request in #cor-engineering Tuesday", "reviewer asked for
-- follow-up before merging". The hub renders the same value on
-- both the workspace row and the detail page so the user can
-- compare context across workspaces without clicking through.
--
-- NULL default means existing rows render as "no notes" without
-- needing a backfill. SQLite supports nullable ADD COLUMN
-- directly, so no table rebuild required.

ALTER TABLE worktree ADD COLUMN notes TEXT DEFAULT NULL;
