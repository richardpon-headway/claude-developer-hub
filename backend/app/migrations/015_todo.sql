-- Todo widget: a single global checklist rendered in the hub's right
-- rail. Independent of repos/worktrees — these are the user's own
-- free-form tasks, not tied to any PR.
--
-- The widget's code lives in app/widgets/todo/; only this schema file
-- sits in the central, forward-only numbered migration sequence that
-- every install replays in order. Per-widget migration ownership is a
-- future enhancement for when widgets become yaml-toggleable.
--
-- bullets is a JSON array of strings — an ordered, item-owned list of
-- sub-points. Storing it inline (rather than in a child table) keeps
-- the autosave path a single-row UPDATE and matches the widget's
-- "one item = one editable card" model.
--
-- sort_order positions PENDING items for drag-to-reorder. Completed
-- items ignore it and sort by completed_at desc (most recently
-- finished on top). Copy-preserving: a pure additive CREATE, no drops.

BEGIN;

CREATE TABLE todo (
  id           INTEGER PRIMARY KEY,
  title        TEXT    NOT NULL DEFAULT '',
  bullets      TEXT    NOT NULL DEFAULT '[]',
  done         INTEGER NOT NULL DEFAULT 0,
  sort_order   REAL    NOT NULL DEFAULT 0,
  completed_at TEXT,
  created_at   TEXT    NOT NULL
);

CREATE INDEX idx_todo_done_sort ON todo (done, sort_order);

COMMIT;
