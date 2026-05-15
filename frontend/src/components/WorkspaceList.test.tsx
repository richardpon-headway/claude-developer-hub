import { render, screen, cleanup } from "@testing-library/react";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, test, vi } from "vitest";

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
      wt({ name: "needs", pr_state: prState("ci_failing") }),
      wt({ name: "ready", pr_state: prState("ready_to_merge") }),
      wt({ name: "wip", pr_state: prState("draft") }),
      wt({ name: "fresh", pr_state: null }),
    ]);
    expect(screen.getByText(/Needs your action/i)).toBeInTheDocument();
    expect(screen.getByText(/Ready to merge/i)).toBeInTheDocument();
    expect(screen.getByText(/In progress/i)).toBeInTheDocument();
    expect(screen.getByText(/No PR yet/i)).toBeInTheDocument();
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
});
