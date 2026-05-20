import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";

import type { DiffConfig } from "../api/config";
import type { FileViewResponse } from "../api/worktrees";
import { FileDiffView } from "./FileDiffView";

const diff: DiffConfig = {
  default_context_lines: 25,
  expand_all_threshold: 200,
};

function base(overrides: Partial<FileViewResponse>): FileViewResponse {
  return {
    path: "foo.py",
    workspace_branch: "feature",
    pr_branch: "feature",
    branch_matches_pr: true,
    file_in_pr_diff: true,
    is_binary: false,
    is_large: false,
    is_missing: false,
    size_bytes: 100,
    rename_from: null,
    on_disk_content: "x = 1\ny = MODIFIED\nw = 4\n",
    line_count: 3,
    hunks: [],
    is_generated_or_lockfile: false,
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("FileDiffView removes", () => {
  test("renders committed_remove ghost lines for a modify hunk", () => {
    // Matches the real backend payload for replacing 2 committed lines
    // with 1 new line at on-disk position 2. The user sees:
    //   line 1  x = 1
    //   ghost   y = 2          (removed)
    //   ghost   z = 3          (removed)
    //   line 2  y = MODIFIED   (added)
    //   line 3  w = 4
    const response = base({
      on_disk_content: "x = 1\ny = MODIFIED\nw = 4\n",
      line_count: 3,
      hunks: [
        {
          on_disk_start: 2,
          on_disk_end: 2,
          lines: [
            {
              kind: "committed_remove",
              content: "y = 2",
              on_disk_lineno: null,
            },
            {
              kind: "committed_remove",
              content: "z = 3",
              on_disk_lineno: null,
            },
            {
              kind: "committed_add",
              content: "y = MODIFIED",
              on_disk_lineno: 2,
            },
          ],
        },
      ],
    });
    render(<FileDiffView response={response} diff={diff} expandAll={false} />);
    expect(screen.getByText("y = 2")).toBeInTheDocument();
    expect(screen.getByText("z = 3")).toBeInTheDocument();
    expect(screen.getByText("y = MODIFIED")).toBeInTheDocument();
  });

  test("renders committed_remove ghosts for a pure-delete hunk in the middle of the file", () => {
    // Old: a, b, c, d, e -> New: a, b, e. Deleted c and d.
    // git diff -U0 emits: @@ -3,2 +2,0 @@\n-c\n-d
    // The ghosts should appear between on-disk line 2 (b) and 3 (e).
    const response = base({
      on_disk_content: "a\nb\ne\n",
      line_count: 3,
      hunks: [
        {
          on_disk_start: 2,
          on_disk_end: 2,
          lines: [
            { kind: "committed_remove", content: "c", on_disk_lineno: null },
            { kind: "committed_remove", content: "d", on_disk_lineno: null },
          ],
        },
      ],
    });
    render(<FileDiffView response={response} diff={diff} expandAll={false} />);
    expect(screen.getByText("c")).toBeInTheDocument();
    expect(screen.getByText("d")).toBeInTheDocument();
  });

  test("renders ghosts for a delete-at-start-of-file hunk", () => {
    // Old: a, b, c -> New: c. Deleted a and b at start.
    // git diff -U0: @@ -1,2 +0,0 @@\n-a\n-b
    // on_disk_start = 0 (before line 1 of new file).
    const response = base({
      on_disk_content: "c\n",
      line_count: 1,
      hunks: [
        {
          on_disk_start: 0,
          on_disk_end: 0,
          lines: [
            { kind: "committed_remove", content: "a", on_disk_lineno: null },
            { kind: "committed_remove", content: "b", on_disk_lineno: null },
          ],
        },
      ],
    });
    render(<FileDiffView response={response} diff={diff} expandAll={false} />);
    expect(screen.getByText("a")).toBeInTheDocument();
    expect(screen.getByText("b")).toBeInTheDocument();
  });

  test("renders ghosts for a delete-at-end-of-file hunk", () => {
    // Old: a, b, c -> New: a. Deleted b and c at end.
    // git diff -U0: @@ -2,2 +1,0 @@\n-b\n-c
    // on_disk_start = 1 (the last surviving line in the new file).
    const response = base({
      on_disk_content: "a\n",
      line_count: 1,
      hunks: [
        {
          on_disk_start: 1,
          on_disk_end: 1,
          lines: [
            { kind: "committed_remove", content: "b", on_disk_lineno: null },
            { kind: "committed_remove", content: "c", on_disk_lineno: null },
          ],
        },
      ],
    });
    render(<FileDiffView response={response} diff={diff} expandAll={false} />);
    expect(screen.getByText("b")).toBeInTheDocument();
    expect(screen.getByText("c")).toBeInTheDocument();
  });

  test("renders uncommitted_remove ghosts alongside committed adds", () => {
    const response = base({
      on_disk_content: "x = 1\nz = 3\n",
      line_count: 2,
      hunks: [
        {
          on_disk_start: 2,
          on_disk_end: 2,
          lines: [
            {
              kind: "uncommitted_remove",
              content: "y = 2",
              on_disk_lineno: null,
            },
          ],
        },
      ],
    });
    render(<FileDiffView response={response} diff={diff} expandAll={false} />);
    expect(screen.getByText("y = 2")).toBeInTheDocument();
  });
});
