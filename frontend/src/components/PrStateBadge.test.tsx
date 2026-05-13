import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("../api/worktrees");

import * as worktreesApi from "../api/worktrees";

import { PrStateBadge } from "./PrStateBadge";
import type { PrHeadline, PrStateSummary } from "../api/types";

function renderBadge(state: PrStateSummary) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <PrStateBadge repo="r" name="wt" state={state} />
    </QueryClientProvider>,
  );
}

function makeState(headline: PrHeadline, overrides: Partial<PrStateSummary> = {}): PrStateSummary {
  return {
    headline,
    pr_number: 42,
    url: "https://github.com/x/y/pull/42",
    title: "the PR",
    is_draft: false,
    mergeable: "MERGEABLE",
    merge_state_status: "CLEAN",
    review_decision: null,
    checks: { passed: 3, fail: 0, pending: 0, total: 3 },
    comments: { human: 0, bot: 0, total: 0 },
    base_ref: "main",
    head_ref: "feat/x",
    updated_at: "2026-05-13T10:00:00Z",
    checked_at: "2026-05-13T11:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(worktreesApi.refreshPrState).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("PrStateBadge", () => {
  test("renders nothing when headline is no_pr", () => {
    const { container } = renderBadge(makeState("no_pr"));
    expect(container.querySelector("button")).toBeNull();
  });

  test("renders headline-specific label and tone", () => {
    renderBadge(makeState("ci_failing", { checks: { passed: 5, fail: 1, pending: 0, total: 6 } }));
    const btn = screen.getByRole("button", { name: /PR CI fail/i });
    // Red tone — emerald/amber are explicitly different palettes.
    expect(btn.className).toMatch(/red/);
  });

  test("ready_to_merge uses green tone", () => {
    renderBadge(makeState("ready_to_merge", { review_decision: "APPROVED" }));
    const btn = screen.getByRole("button", { name: /PR ready/i });
    expect(btn.className).toMatch(/emerald/);
  });

  test("clicking opens popover with the detail rows", async () => {
    renderBadge(
      makeState("ci_failing", {
        checks: { passed: 5, fail: 1, pending: 0, total: 6 },
        comments: { human: 2, bot: 4, total: 6 },
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /PR CI fail/i }));
    await waitFor(() => {
      expect(screen.getByText(/PR #42/)).toBeInTheDocument();
    });
    expect(screen.getByText(/1 failing \/ 6/)).toBeInTheDocument();
    expect(screen.getByText(/2 human/)).toBeInTheDocument();
    expect(screen.getByText(/4 bot/)).toBeInTheDocument();
  });

  test("Refresh now fires refreshPrState and updates the rendered state", async () => {
    vi.mocked(worktreesApi.refreshPrState).mockResolvedValue(
      makeState("ready_to_merge", {
        review_decision: "APPROVED",
        checks: { passed: 12, fail: 0, pending: 0, total: 12 },
      }),
    );
    renderBadge(makeState("checks_running", { checks: { passed: 2, fail: 0, pending: 3, total: 5 } }));
    fireEvent.click(screen.getByRole("button", { name: /PR checks/i }));
    await waitFor(() => {
      expect(screen.getByText(/3 pending \/ 5/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /refresh now/i }));
    await waitFor(() => {
      expect(worktreesApi.refreshPrState).toHaveBeenCalledWith("r", "wt");
    });
    // After refresh: the popover detail re-renders with the new checks line.
    await waitFor(() => {
      expect(screen.getByText(/12\/12 ✓/)).toBeInTheDocument();
    });
  });
});
