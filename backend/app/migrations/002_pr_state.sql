-- pr_state caches the "what does this PR need" classification for each
-- worktree. Populated by the background polling task in
-- services/pr_state_poll.py (every ~3 min) and on demand by the manual
-- refresh endpoint. Read by the hub list-worktrees query via LEFT JOIN
-- so the frontend gets PR state in the same payload as the rest of the
-- row, with no extra fetch.
--
-- headline is denormalized out of the JSON payload so SQL can filter or
-- sort by it without parsing. Priority order matches the
-- pr-check-action-required skill.

BEGIN;

CREATE TABLE pr_state (
  repo          TEXT NOT NULL,
  worktree_name TEXT NOT NULL,
  headline      TEXT NOT NULL CHECK (headline IN (
    'no_pr',
    'ci_failing',
    'merge_conflicts',
    'in_merge_queue',
    'ready_to_merge',
    'human_comment',
    'review_requested',
    'checks_running',
    'waiting_on_others',
    'draft'
  )),
  -- JSON-encoded; shape mirrors PrSummary in services/pr_state.py.
  payload       TEXT NOT NULL,
  checked_at    TEXT NOT NULL,
  PRIMARY KEY (repo, worktree_name),
  FOREIGN KEY (repo, worktree_name)
    REFERENCES worktree(repo, name)
    ON DELETE CASCADE
);
CREATE INDEX pr_state_headline_idx ON pr_state(headline);

COMMIT;
