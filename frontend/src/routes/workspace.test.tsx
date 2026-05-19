import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { ApiError } from "../api/client";
import * as worktreesApi from "../api/worktrees";
import type { Worktree, WorktreeDetail } from "../api/types";

// Stub the Link import so the workspace page renders without a router context.
vi.mock("@tanstack/react-router", async () => {
  const actual = await vi.importActual<object>("@tanstack/react-router");
  return {
    ...actual,
    Link: ({ children, to, ...rest }: {
      children: React.ReactNode;
      to?: string;
      [k: string]: unknown;
    }) => <a href={to as string} {...rest}>{children}</a>,
  };
});

vi.mock("../api/worktrees");
vi.mock("../api/config");

// Import after the mocks so the page picks them up.
import * as configApi from "../api/config";
import { WorkspacePage } from "./workspace.$repo.$name";

function renderPage(repo = "myrepo", name = "feature") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RadixTooltip.Provider>
        <WorkspacePage repo={repo} name={name} />
      </RadixTooltip.Provider>
    </QueryClientProvider>,
  );
}

function makeWorktree(overrides: Partial<Worktree> = {}): Worktree {
  return {
    repo: "myrepo",
    name: "feature",
    path: "/tmp/wt",
    branch: "feature",
    ticket: null,
    pr_number: null,
    pr_repo: null,
    pr_author_login: null,
    created_at: "2026-01-01T00:00:00Z",
    status: "ready",
    has_claude_session: false,
    pr_state: null,
    ...overrides,
  };
}

function makeDetail(
  overrides: Partial<Worktree> = {},
  log: string[] = [],
): WorktreeDetail {
  return { row: makeWorktree(overrides), log };
}

beforeEach(() => {
  vi.mocked(worktreesApi.getWorktree).mockReset();
  vi.mocked(worktreesApi.spawnIterm).mockReset();
  vi.mocked(worktreesApi.runSkill).mockReset();
  vi.mocked(worktreesApi.sendText).mockReset();
  vi.mocked(configApi.getWorkspaceSkills).mockReset();
  vi.mocked(configApi.getWorkspaceSkills).mockResolvedValue([
    {
      name: "pr-finalize-for-review",
      label: "/pr-finalize-for-review",
      description: null,
    },
    {
      name: "pr-review",
      label: "/pr-review",
      description: null,
    },
  ]);
});

afterEach(() => {
  cleanup();
});

describe("WorkspacePage", () => {
  test("skill buttons stay enabled without a session (auto-spawn)", async () => {
    // When no Claude session exists, the skill button still fires —
    // the backend spawns iTerm2 with `claude '/<skill>'` as the
    // initial prompt. The "open this workspace in iTerm2 first" gate
    // only applied before the spawn-on-miss path landed.
    vi.mocked(worktreesApi.getWorktree).mockResolvedValue(
      makeDetail({ has_claude_session: false, status: "ready" }),
    );
    renderPage();
    const finalizeBtn = await screen.findByRole("button", {
      name: "/pr-finalize-for-review",
    });
    expect(finalizeBtn).toBeEnabled();
  });

  test("skill buttons disabled when worktree is not ready", async () => {
    vi.mocked(worktreesApi.getWorktree).mockResolvedValue(
      makeDetail({ has_claude_session: false, status: "failed" }),
    );
    renderPage();
    const finalizeBtn = await screen.findByRole("button", {
      name: "/pr-finalize-for-review",
    });
    expect(finalizeBtn).toBeDisabled();
  });

  test("skill buttons enabled when claude session is open", async () => {
    vi.mocked(worktreesApi.getWorktree).mockResolvedValue(
      makeDetail({ has_claude_session: true }),
    );
    renderPage();
    const finalizeBtn = await screen.findByRole("button", {
      name: "/pr-finalize-for-review",
    });
    expect(finalizeBtn).toBeEnabled();
  });

  test("clicking 'Open in iTerm2' calls spawn", async () => {
    vi.mocked(worktreesApi.getWorktree).mockResolvedValue(
      makeDetail({ has_claude_session: false, status: "ready" }),
    );
    vi.mocked(worktreesApi.spawnIterm).mockResolvedValue({
      window_id: "W",
      claude_session_id: "C",
      shell_session_id: "S",
      claude_session_uuid: "U",
      sidecar_path: null,
    });
    renderPage();
    const openBtn = await screen.findByRole("button", { name: /open in iterm2/i });
    fireEvent.click(openBtn);
    await waitFor(() => {
      expect(worktreesApi.spawnIterm).toHaveBeenCalledWith("myrepo", "feature");
    });
  });

  test("running a skill calls run-skill with correct slash name", async () => {
    vi.mocked(worktreesApi.getWorktree).mockResolvedValue(
      makeDetail({ has_claude_session: true }),
    );
    vi.mocked(worktreesApi.runSkill).mockResolvedValue({ sent: true });
    renderPage();
    const btn = await screen.findByRole("button", {
      name: "/pr-finalize-for-review",
    });
    fireEvent.click(btn);
    await waitFor(() => {
      expect(worktreesApi.runSkill).toHaveBeenCalledWith(
        "myrepo",
        "feature",
        "pr-finalize-for-review",
      );
    });
  });

  test("renders send-gate 409 error inline", async () => {
    vi.mocked(worktreesApi.getWorktree).mockResolvedValue(
      makeDetail({ has_claude_session: true }),
    );
    vi.mocked(worktreesApi.runSkill).mockRejectedValue(
      new ApiError(409, "Claude is awaiting input. Resolve the prompt first."),
    );
    renderPage();
    const btn = await screen.findByRole("button", {
      name: "/pr-finalize-for-review",
    });
    fireEvent.click(btn);
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/awaiting input/i);
    });
  });

  test("Open button disabled when worktree status is not ready", async () => {
    vi.mocked(worktreesApi.getWorktree).mockResolvedValue(
      makeDetail({ status: "failed" }),
    );
    renderPage();
    const openBtn = await screen.findByRole("button", { name: /open in iterm2/i });
    expect(openBtn).toBeDisabled();
  });
});
