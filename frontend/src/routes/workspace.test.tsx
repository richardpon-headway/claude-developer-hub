import { render, screen, waitFor, cleanup, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { ApiError } from "../api/client";
import * as worktreesApi from "../api/worktrees";
import type { Worktree, WorktreeDetail } from "../api/types";

// Stub Link + useNavigate so the workspace page renders without a router context.
const navigateSpy = vi.fn();

vi.mock("@tanstack/react-router", async () => {
  const actual = await vi.importActual<object>("@tanstack/react-router");
  return {
    ...actual,
    Link: ({ children, to, ...rest }: {
      children: React.ReactNode;
      to?: string;
      [k: string]: unknown;
    }) => <a href={to as string} {...rest}>{children}</a>,
    useNavigate: () => navigateSpy,
  };
});

vi.mock("../api/worktrees");
vi.mock("../api/config");

// Import after the mocks so the page picks them up.
import * as configApi from "../api/config";
import { WorkspacePage } from "./workspace.$repo.$name.index";

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
    notes: null,
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
  vi.mocked(worktreesApi.deleteWorktree).mockReset();
  navigateSpy.mockReset();
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

  test("Delete button hidden when status is setting_up", async () => {
    vi.mocked(worktreesApi.getWorktree).mockResolvedValue(
      makeDetail({ status: "setting_up" }),
    );
    renderPage();
    // Wait for the page to settle on the loaded state — once the
    // "Open in iTerm2" button renders, the page has hydrated.
    await screen.findByRole("button", { name: /open in iterm2/i });
    expect(
      screen.queryByRole("button", { name: /delete worktree/i }),
    ).not.toBeInTheDocument();
  });

  test("Delete button visible for ready worktrees", async () => {
    vi.mocked(worktreesApi.getWorktree).mockResolvedValue(
      makeDetail({ status: "ready" }),
    );
    renderPage();
    const btn = await screen.findByRole("button", { name: /delete worktree/i });
    expect(btn).toBeEnabled();
  });

  test("Delete flow: click → confirm → API called → navigate home", async () => {
    vi.mocked(worktreesApi.getWorktree).mockResolvedValue(
      makeDetail({ status: "ready", path: "/tmp/wt" }),
    );
    vi.mocked(worktreesApi.deleteWorktree).mockResolvedValue({ deleted: true });

    renderPage();
    const trigger = await screen.findByRole("button", { name: /delete worktree/i });
    fireEvent.click(trigger);

    // Dialog opens; show the path being deleted.
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("/tmp/wt");

    // Confirm via the dialog's Delete button (the second one — the
    // first "Delete worktree" trigger is outside the dialog).
    const confirmBtn = within(dialog).getByRole("button", { name: /^delete$/i });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(worktreesApi.deleteWorktree).toHaveBeenCalledWith("myrepo", "feature");
    });
    await waitFor(() => {
      expect(navigateSpy).toHaveBeenCalledWith({ to: "/" });
    });
  });

  test("Delete failure shows inline error", async () => {
    vi.mocked(worktreesApi.getWorktree).mockResolvedValue(
      makeDetail({ status: "ready" }),
    );
    vi.mocked(worktreesApi.deleteWorktree).mockRejectedValue(
      new ApiError(502, "git worktree remove exploded"),
    );

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /delete worktree/i }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/git worktree remove exploded/i);
    });
    // Stays on the page (no navigate) so the user can read the error.
    expect(navigateSpy).not.toHaveBeenCalled();
  });
});
