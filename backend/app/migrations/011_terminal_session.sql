-- Rename `iterm_session` -> `terminal_session` and add a `terminal_kind`
-- discriminator so a future Ghostty adapter can co-exist with the
-- existing iTerm2 one.
--
-- Forward-only, ALTER-based. Existing rows are preserved; the new
-- `terminal_kind` column backfills to 'iterm2' for every row, which
-- is correct: anything tracked before this migration was an iTerm2
-- window. The FK from this table to worktree(repo, name) survives
-- the rename — SQLite re-binds it on the next reference because the
-- parent table name didn't change.
--
-- Column renames need SQLite >= 3.25 (2018-09-15). All supported
-- macOS Python builds ship a newer SQLite than that.
--
-- The `iterm_lifecycle` table stays as-is — it stores iTerm2-specific
-- restart-detection state (`iterm2_started_at`) that has no Ghostty
-- equivalent, so its name accurately reflects its scope.

BEGIN;

ALTER TABLE iterm_session RENAME TO terminal_session;
ALTER TABLE terminal_session RENAME COLUMN iterm_window_id TO window_id;
ALTER TABLE terminal_session RENAME COLUMN iterm_session_id TO session_id;
ALTER TABLE terminal_session ADD COLUMN terminal_kind TEXT NOT NULL DEFAULT 'iterm2';

DROP INDEX iterm_session_id_idx;
CREATE INDEX terminal_session_id_idx ON terminal_session(session_id);

COMMIT;
