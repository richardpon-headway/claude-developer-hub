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

function renderWorkspaces(
  worktrees: Worktree[],
  userLogin: string | null = null,
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RadixTooltip.Provider>
        <WorkspaceList
          worktrees={worktrees}
          jira={null}
          userLogin={userLogin}
        />
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
    author_login: null,
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
    pr_author_login: null,
    created_at: "2026-05-15T00:00:00Z",
    status: "ready",
    has_claude_session: false,
    pr_state: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(worktreesApi.spawnIterm).mockReset();
  vi.mocked(worktreesApi.focusIterm).mockReset();
  vi.mocked(worktreesApi.recreateWorktree).mockReset();
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

  test("within Needs your action: approval-ready rows sort above blocker-only rows", () => {
    // PROJ-100 has only unaddressed (blocker), no approval.
    // PROJ-200 has unaddressed AND ready_to_merge (one comment-resolve
    // away from merge). The latter should appear first inside the
    // tier even though "C200" sorts after "C100" alphabetically.
    renderWorkspaces([
      wt({
        name: "PROJ-100_aaa_unaddressed",
        pr_state: prState("unresolved_comments", ["unresolved_comments"]),
      }),
      wt({
        name: "PROJ-200_zzz_approved_with_unaddressed",
        pr_state: prState("unresolved_comments", [
          "unresolved_comments",
          "ready_to_merge",
        ]),
      }),
    ]);
    const titles = screen
      .getAllByRole("link")
      // Workspace title is rendered as a <Link>; in tests Link is
      // stubbed to <a>. Its text is the worktree name.
      .map((a) => a.textContent ?? "")
      .filter((t) => t.startsWith("PROJ-"));
    expect(titles[0]).toContain("PROJ-200_zzz_approved_with_unaddressed");
    expect(titles[1]).toContain("PROJ-100_aaa_unaddressed");
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
    expect(screen.getByText("unaddressed_comments")).toBeInTheDocument();
    // Sits under Needs your action.
    expect(
      screen.getByRole("heading", { name: /Needs your action/i }),
    ).toBeInTheDocument();
  });

  test("frontend renders all labels in the payload (terminal-state suppression happens on the backend)", () => {
    // The backend now collapses terminal-state label sets to a
    // single chip (merged + ci_failing → just [merged]), so a hub
    // payload like ["merged", "ci_failing"] won't occur in practice.
    // But the frontend's responsibility is to render whatever it
    // receives — pinning this keeps the rendering layer dumb if the
    // backend rule ever changes.
    renderWorkspaces([
      wt({
        name: "merged_with_old_ci_fail",
        pr_state: prState("merged", ["merged", "ci_failing"]),
      }),
    ]);
    // Tier still comes from labels[0]=merged → MERGED.
    expect(
      screen.getByRole("heading", { name: /^Merged/i }),
    ).toBeInTheDocument();
    // Both chips render — the frontend doesn't second-guess the
    // backend's label set.
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

  test("ready + claude session → Focus iTerm2 button calls focus-iterm", async () => {
    vi.mocked(worktreesApi.focusIterm).mockResolvedValue({ focused: true });
    renderWorkspaces([
      wt({ name: "with-claude", status: "ready", has_claude_session: true }),
    ]);
    const btn = screen.getByRole("button", { name: /^focus iterm2$/i });
    expect(btn).toBeEnabled();
    fireEvent.click(btn);
    await waitFor(() => {
      expect(worktreesApi.focusIterm).toHaveBeenCalledWith(
        "myapp",
        "with-claude",
      );
    });
  });

  test("ready + no claude session → iTerm2 button calls spawn-iterm", async () => {
    vi.mocked(worktreesApi.spawnIterm).mockResolvedValue({
      window_id: "W",
      claude_session_id: "C",
      shell_session_id: "S",
      claude_session_uuid: null,
      sidecar_path: null,
    });
    renderWorkspaces([
      wt({ name: "no-claude", status: "ready", has_claude_session: false }),
    ]);
    const btn = screen.getByRole("button", { name: /^iterm2$/i });
    expect(btn).toBeEnabled();
    fireEvent.click(btn);
    await waitFor(() => {
      expect(worktreesApi.spawnIterm).toHaveBeenCalledWith("myapp", "no-claude");
    });
  });

  test("setting_up → Configuring… button is disabled", () => {
    renderWorkspaces([
      wt({ name: "wip", status: "setting_up", has_claude_session: false }),
    ]);
    const btn = screen.getByRole("button", { name: /^configuring…$/i });
    expect(btn).toBeDisabled();
  });

  test("failed → Setup failed renders as link to the workspace Manage page", () => {
    renderWorkspaces([
      wt({ name: "broken", status: "failed", has_claude_session: false }),
    ]);
    const link = screen.getByRole("link", { name: /^setup failed$/i });
    // Link is stubbed to <a href={to}> in the router mock at file top.
    expect(link).toHaveAttribute("href", "/workspace/$repo/$name");
  });

  test("stale → Recreate workspace button calls recreate endpoint", async () => {
    vi.mocked(worktreesApi.recreateWorktree).mockResolvedValue({
      repo: "myapp",
      name: "gone",
      path: "/tmp/p",
      branch: "feat/gone",
      ticket: null,
      pr_number: null,
      pr_repo: null,
      pr_author_login: null,
      created_at: "2026-05-18T00:00:00Z",
      status: "ready",
      has_claude_session: false,
      pr_state: null,
    });
    renderWorkspaces([
      wt({ name: "gone", status: "stale", has_claude_session: false }),
    ]);
    const btn = screen.getByRole("button", { name: /^recreate workspace$/i });
    fireEvent.click(btn);
    await waitFor(() => {
      expect(worktreesApi.recreateWorktree).toHaveBeenCalledWith("myapp", "gone");
    });
  });

  // --- REVIEWING tier ---------------------------------------------------

  test("reviewing tier renders at top with author chip when pr_author_login != user_login", () => {
    renderWorkspaces(
      [
        wt({ name: "my-feat", pr_state: prState("ci_failing") }),
        wt({
          name: "their-pr",
          pr_author_login: "sarah-h",
          pr_state: prState("unresolved_comments"),
        }),
      ],
      "octocat",
    );
    const headings = screen
      .getAllByRole("heading", { level: 3 })
      .map((h) => h.textContent ?? "");
    // Reviewing is at the top of the stack.
    expect(headings[0]).toMatch(/Reviewing/i);
    // Author chip renders on the reviewer row.
    expect(screen.getByText("@sarah-h")).toBeInTheDocument();
  });

  test("reviewer-owned merged row sorts into REVIEWING, not MERGED", () => {
    // Ownership trumps state: even though the labels would put this
    // row into MERGED, it sorts under REVIEWING because the author
    // isn't the local user.
    renderWorkspaces(
      [
        wt({
          name: "their-merged",
          pr_author_login: "alex-r",
          pr_state: prState("merged"),
        }),
      ],
      "octocat",
    );
    // Reviewing tier has the row.
    const reviewingHeading = screen.getByRole("heading", { name: /Reviewing/i });
    const reviewingSection = reviewingHeading.closest("section");
    expect(reviewingSection?.textContent).toContain("their-merged");
    // Merged tier is empty (50% opacity placeholder).
    const mergedHeading = screen.getByRole("heading", { name: /^Merged/i });
    const mergedSection = mergedHeading.closest("section");
    expect(mergedSection?.textContent).not.toContain("their-merged");
  });

  test("self-authored row sorts by state-tier, not REVIEWING", () => {
    renderWorkspaces(
      [
        wt({
          name: "mine",
          pr_author_login: "octocat",
          pr_state: prState("ci_failing"),
        }),
      ],
      "octocat",
    );
    const needsActionHeading = screen.getByRole("heading", {
      name: /Needs your action/i,
    });
    expect(needsActionHeading.closest("section")?.textContent).toContain(
      "mine",
    );
    // No author chip on owner rows.
    expect(screen.queryByText("@octocat")).not.toBeInTheDocument();
  });

  test("null pr_author_login sorts by state-tier (legacy / pre-backfill rows)", () => {
    renderWorkspaces(
      [wt({ name: "legacy", pr_state: prState("ci_failing") })],
      "octocat",
    );
    const needsActionHeading = screen.getByRole("heading", {
      name: /Needs your action/i,
    });
    expect(needsActionHeading.closest("section")?.textContent).toContain(
      "legacy",
    );
  });

  test("null userLogin disables the split — every row sorts by state-tier", () => {
    // gh-missing or fail-open: no userLogin → no REVIEWING. Even a
    // row with a known non-self author falls through to its state-
    // tier so the hub keeps working.
    renderWorkspaces(
      [
        wt({
          name: "their-pr",
          pr_author_login: "sarah-h",
          pr_state: prState("merged"),
        }),
      ],
      null,
    );
    const mergedHeading = screen.getByRole("heading", { name: /^Merged/i });
    expect(mergedHeading.closest("section")?.textContent).toContain("their-pr");
    // No author chip without the split active.
    expect(screen.queryByText("@sarah-h")).not.toBeInTheDocument();
  });

  test("approval-ready promotion still applies inside REVIEWING", () => {
    // Same within-tier sort: ready_to_merge bubbles to the top, even
    // when the row is reviewer-owned and bucketed into REVIEWING.
    renderWorkspaces(
      [
        wt({
          name: "their_aaa_unaddressed",
          pr_author_login: "sarah-h",
          pr_state: prState("unresolved_comments", ["unresolved_comments"]),
        }),
        wt({
          name: "their_zzz_approved",
          pr_author_login: "alex-r",
          pr_state: prState("unresolved_comments", [
            "unresolved_comments",
            "ready_to_merge",
          ]),
        }),
      ],
      "octocat",
    );
    const reviewingHeading = screen.getByRole("heading", { name: /Reviewing/i });
    const section = reviewingHeading.closest("section")!;
    const names = Array.from(section.querySelectorAll("a"))
      .map((a) => a.textContent ?? "")
      .filter((t) => t.startsWith("their_"));
    expect(names[0]).toContain("their_zzz_approved");
    expect(names[1]).toContain("their_aaa_unaddressed");
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
