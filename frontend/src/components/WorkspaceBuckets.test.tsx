import { render, screen, cleanup, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// Stub Link so cards render without a router context.
vi.mock("@tanstack/react-router", async () => {
  const actual = await vi.importActual<object>("@tanstack/react-router");
  return {
    ...actual,
    Link: ({ children, to, ...rest }: { children: React.ReactNode; to?: string; [k: string]: unknown }) => (
      <a href={to as string} {...rest}>{children}</a>
    ),
  };
});

vi.mock("../api/workspaces");

import * as wsApi from "../api/workspaces";
import { WorkspaceBuckets } from "./WorkspaceBuckets";
import type { WorkspaceEntity } from "../api/types";

function entity(overrides: Partial<WorkspaceEntity> = {}): WorkspaceEntity {
  return {
    pr_repo: "acme/app",
    pr_number: 1,
    title: "default title",
    url: "https://github.com/acme/app/pull/1",
    author_login: "me",
    is_bookmarked: false,
    state: "open",
    ci_status: "pass",
    is_draft: false,
    ticket: null,
    notes: null,
    worktree: null,
    pr_state: null,
    ...overrides,
  };
}

function renderBuckets(workspaces: WorkspaceEntity[], userLogin: string | null = "me") {
  vi.mocked(wsApi.getWorkspaces).mockResolvedValue({ user_login: userLogin, workspaces });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <RadixTooltip.Provider>
        <WorkspaceBuckets jira={null} />
      </RadixTooltip.Provider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(wsApi.getWorkspaces).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("WorkspaceBuckets", () => {
  test("splits My Work vs Reviewing by authorship", async () => {
    renderBuckets([
      entity({ pr_number: 1, title: "mine", author_login: "me" }),
      entity({ pr_number: 2, title: "theirs", author_login: "alice" }),
    ]);
    expect(await screen.findByText("My Work")).toBeInTheDocument();
    expect(screen.getByText("Reviewing")).toBeInTheDocument();
    expect(screen.getByText("mine")).toBeInTheDocument();
    expect(screen.getByText("theirs")).toBeInTheDocument();
  });

  test("null-author entity buckets into My Work", async () => {
    renderBuckets([
      entity({ pr_number: null, pr_repo: null, url: "", author_login: null, title: "scratch",
        worktree: { repo: "app", name: "scratch", path: "/x", branch: "scratch", status: "ready", has_claude_session: false } }),
    ]);
    expect(await screen.findByText("My Work")).toBeInTheDocument();
    expect(screen.queryByText("Reviewing")).not.toBeInTheDocument();
  });

  test("★ chip renders on bookmarked rows", async () => {
    renderBuckets([entity({ author_login: "alice", is_bookmarked: true })]);
    expect(await screen.findByText("★")).toBeInTheDocument();
  });

  test("bookmarked-only filter hides non-bookmarked reviewing rows", async () => {
    renderBuckets([
      entity({ pr_number: 1, title: "tracked", author_login: "alice", is_bookmarked: true }),
      entity({ pr_number: 2, title: "untracked", author_login: "bob", is_bookmarked: false }),
    ]);
    expect(await screen.findByText("untracked")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/bookmarked only/i));
    await waitFor(() => {
      expect(screen.queryByText("untracked")).not.toBeInTheDocument();
    });
    expect(screen.getByText("tracked")).toBeInTheDocument();
  });

  test("non-local card has no terminal button; local card has Details", async () => {
    renderBuckets([
      entity({ pr_number: 1, title: "remote", author_login: "alice", is_bookmarked: true }),
      entity({ pr_number: 2, title: "local", author_login: "me",
        worktree: { repo: "app", name: "feat", path: "/x", branch: "feat", status: "ready", has_claude_session: false } }),
    ]);
    await screen.findByText("remote");
    // The terminal button is labeled with the terminal display name.
    expect(screen.getByRole("button", { name: /iTerm2/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /details/i })).toBeInTheDocument();
  });

  test("scalar state drives the chip when pr_state is absent", async () => {
    renderBuckets([
      entity({ author_login: "alice", is_bookmarked: true, state: "merged", pr_state: null }),
    ]);
    // "merged" chip from the scalar fallback (not "no PR").
    expect(await screen.findByText("merged")).toBeInTheDocument();
    expect(screen.queryByText("no PR")).not.toBeInTheDocument();
  });

  test("pr_state labels drive the chip when present", async () => {
    renderBuckets([
      entity({
        author_login: "alice", is_bookmarked: true,
        pr_state: {
          headline: "ci_failing", labels: ["ci_failing"], pr_number: 1,
          url: null, title: null, is_draft: false, mergeable: null,
          merge_state_status: null, review_decision: null,
          checks: { passed: 0, fail: 1, pending: 0, total: 1 },
          comments: { human: 0, bot: 0, total: 0 },
          base_ref: null, head_ref: null, updated_at: null,
          author_login: "alice", checked_at: "2026-01-01T00:00:00Z",
          unresolved_threads: 0,
        },
      }),
    ]);
    expect(await screen.findByText("ci fail")).toBeInTheDocument();
  });

  test("empty hub renders nothing in either bucket", async () => {
    renderBuckets([]);
    // Only the bookmark intake remains once the (empty) data resolves.
    expect(
      await screen.findByPlaceholderText(/paste a github pr url/i),
    ).toBeInTheDocument();
    expect(screen.queryByText("My Work")).not.toBeInTheDocument();
    expect(screen.queryByText("Reviewing")).not.toBeInTheDocument();
  });
});
