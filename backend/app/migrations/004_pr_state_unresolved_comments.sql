-- Extend the pr_state.headline CHECK constraint to allow
-- 'unresolved_comments'. Added when we started surfacing per-thread
-- isResolved counts via GraphQL — a PR with open review threads now
-- emits this headline as the loudest signal (above human_comment).
--
-- SQLite can't ALTER a CHECK in place; the standard idiom is
-- drop + recreate. Safe because pr_state is fully derived from gh —
-- the next polling tick (within ~3 min of daemon start) repopulates
-- everything from scratch.

BEGIN;

DROP TABLE IF EXISTS pr_state;

CREATE TABLE pr_state (
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
CREATE INDEX pr_state_headline_idx ON pr_state(headline);

COMMIT;
