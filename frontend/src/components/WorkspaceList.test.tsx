import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// Stub Link so WorkspaceList renders without a router context.
vi.mock("@tanstack/react-router", async () => {
  const actual = await vi.importActual<object>("@tanstack/react-router");
  return {
    ...actual,
    Link: ({
      children,
      to,
      ...rest
    }: {
      children: React.ReactNode;
      to?: string;
      [k: string]: unknown;
    }) => (
      <a href={to as string} {...rest}>
        {children}
      </a>
    ),
  };
});

vi.mock("../api/worktrees");

import * as worktreesApi from "../api/worktrees";
import { ApiError } from "../api/client";

import { WorkspaceList } from "./WorkspaceList";
import type { PrHeadline, PrStateSummary, Worktree } from "../api/types";

function renderWorkspaces(worktrees: Worktree[]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RadixTooltip.Provider>
        <WorkspaceList worktrees={worktrees} jira={null} />
      </RadixTooltip.Provider>
    </QueryClientProvider>,
  );
}

function prState(
  headline: PrHeadline,
  labels: PrHeadline[] = [headline],
): PrStateSummary {
  return {
    headline,
    labels,
    pr_number: 1,
    url: "https://github.com/o/r/pull/1",
    title: "t",
    is_draft: false,
    mergeable: null,
    merge_state_status: null,
    review_decision: null,
    checks: { passed: 0, fail: 0, pending: 0, total: 0 },
    comments: { human: 0, bot: 0, total: 0 },
    unresolved_threads: 0,
    base_ref: null,
    head_ref: null,
    updated_at: null,
    checked_at: "2026-05-15T00:00:00Z",
  };
}

function wt(overrides: Partial<Worktree> = {}): Worktree {
  return {
    repo: "myapp",
    name: "feat_x",
    path: "/tmp/myapp_worktree_feat_x",
    branch: "feat/x",
    ticket: null,
    pr_number: 1,
    pr_repo: "acme/myapp",
    created_at: "2026-05-15T00:00:00Z",
    status: "ready",
    has_claude_session: false,
    pr_state: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(worktreesApi.spawnIterm).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("WorkspaceList", () => {
  test("renders nothing when there are no worktrees", () => {
    const { container } = renderWorkspaces([]);
    expect(container.firstChild).toBeNull();
  });

  test("groups by tier (no per-headline sub-boxes)", () => {
    renderWorkspaces([
      wt({ name: "done", pr_state: prState("merged") }),
      wt({ name: "needs", pr_state: prState("ci_failing") }),
      wt({ name: "ready", pr_state: prState("ready_to_merge") }),
      wt({ name: "wip", pr_state: prState("draft") }),
      wt({ name: "fresh", pr_state: null }),
    ]);
    // Tier headers are <h3>s. The chip with the same text family
    // ("Approved - Ready to Merge") is a <span>, so role/heading
    // narrows the lookup.
    expect(
      screen.getByRole("heading", { name: /Merged/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Needs your action/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Ready to merge/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /In progress/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /No PR yet/i }),
    ).toBeInTheDocument();
  });

  test("tier order: Merged comes before Ready to merge before Needs your action", () => {
    renderWorkspaces([
      wt({ name: "done", pr_state: prState("merged") }),
      wt({ name: "approved", pr_state: prState("ready_to_merge") }),
      wt({ name: "failing", pr_state: prState("ci_failing") }),
    ]);
    const headings = screen
      .getAllByRole("heading", { level: 3 })
      .map((h) => h.textContent ?? "");
    const mergedIdx = headings.findIndex((t) => /Merged/i.test(t));
    const readyIdx = headings.findIndex((t) => /Ready to merge/i.test(t));
    const needsIdx = headings.findIndex((t) => /Needs your action/i.test(t));
    expect(mergedIdx).toBeGreaterThanOrEqual(0);
    expect(mergedIdx).toBeLessThan(readyIdx);
    expect(readyIdx).toBeLessThan(needsIdx);
  });

  test("renders all labels as inline chips on a multi-label row", () => {
    renderWorkspaces([
      wt({
        name: "multi",
        pr_state: prState("ci_failing", ["ci_failing", "human_comment"]),
      }),
    ]);
    expect(screen.getByText("ci fail")).toBeInTheDocument();
    expect(screen.getByText("review")).toBeInTheDocument();
  });

  test("renders the unaddressed chip when unresolved_comments is present", () => {
    renderWorkspaces([
      wt({
        name: "unresolved",
        pr_state: prState("unresolved_comments", [
          "unresolved_comments",
          "human_comment",
        ]),
      }),
    ]);
    expect(screen.getByText("unaddressed")).toBeInTheDocument();
    // Sits under Needs your action.
    expect(
      screen.getByRole("heading", { name: /Needs your action/i }),
    ).toBeInTheDocument();
  });

  test("tier is determined by labels[0], not by suppressed signals", () => {
    // merged + ci_failing co-occur. labels[0]=merged → "Needs your action".
    renderWorkspaces([
      wt({
        name: "merged_with_old_ci_fail",
        pr_state: prState("merged", ["merged", "ci_failing"]),
      }),
    ]);
    // Row sits under "Needs your action" since `merged` is a cleanup
    // action item per the existing tier mapping.
    expect(screen.getByText(/Needs your action/i)).toBeInTheDocument();
    // Both chips render
    expect(screen.getByText("merged")).toBeInTheDocument();
    expect(screen.getByText("ci fail")).toBeInTheDocument();
  });

  test("back-compat: row with only headline (no labels array) still renders one chip", () => {
    // Simulate an old API payload pre-multi-label refactor.
    const stateWithoutLabels = {
      ...prState("ci_failing", []),
      labels: [],
    } as PrStateSummary;
    renderWorkspaces([
      wt({ name: "old", pr_state: stateWithoutLabels }),
    ]);
    expect(screen.getByText("ci fail")).toBeInTheDocument();
  });

  test("iTerm2 spawn failure surfaces inline error + flips the button to a red state", async () => {
    // Reproduces the "underlying worktree was deleted" case: backend
    // returns 400 with "worktree path missing on disk", and the user
    // sees nothing happen unless the error is rendered inline.
    vi.mocked(worktreesApi.spawnIterm).mockRejectedValue(
      new ApiError(400, "worktree path missing on disk: /tmp/gone"),
    );

    renderWorkspaces([wt({ name: "ghost" })]);

    const btn = screen.getByRole("button", { name: /^iterm2$/i });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(worktreesApi.spawnIterm).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(
        screen.getByText(/worktree path missing on disk/i),
      ).toBeInTheDocument();
    });
    // Button visibly flipped to the error state.
    expect(
      screen.getByRole("button", { name: /iterm2 ✗/i }),
    ).toBeInTheDocument();
  });
});
