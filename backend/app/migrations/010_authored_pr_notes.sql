-- Notes storage for "My PRs (no worktree)" tier rows.
--
-- The authored-PR tier is not persisted — `app.services.authored_prs`
-- recomputes it each request from `gh search prs --author:@me`. To
-- attach notes to those rows, we need a tiny persistence layer keyed
-- on the PR identifiers (since there's no other row to hang notes off
-- of). On surface transition (pull-down → worktree, or bookmark), the
-- route handler copies the notes into the destination surface's notes
-- column and deletes the row here.

CREATE TABLE authored_pr_notes (
  pr_repo    TEXT    NOT NULL,
  pr_number  INTEGER NOT NULL,
  notes      TEXT    NOT NULL,
  updated_at TEXT    NOT NULL,
  PRIMARY KEY (pr_repo, pr_number)
);
