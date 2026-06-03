import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// Stub Link so HubPage renders without a router context.
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

vi.mock("../api/repos");
vi.mock("../api/worktrees");
vi.mock("../api/workspaces");
vi.mock("../api/config");

import * as configApi from "../api/config";
import * as reposApi from "../api/repos";
import * as workspacesApi from "../api/workspaces";
import * as worktreesApi from "../api/worktrees";

import { HubPage } from "./index";

function renderHub() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RadixTooltip.Provider>
        <HubPage />
      </RadixTooltip.Provider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(reposApi.listRepos).mockReset();
  vi.mocked(worktreesApi.syncWorktrees).mockReset();
  vi.mocked(workspacesApi.getWorkspaces).mockReset();
  vi.mocked(workspacesApi.getWorkspaces).mockResolvedValue({
    user_login: null,
    workspaces: [],
  });
  vi.mocked(worktreesApi.getTokenUsage).mockReset();
  vi.mocked(worktreesApi.getTokenUsage).mockResolvedValue({
    offline: true,
    today_output: 0,
    today_input: 0,
    today_messages: 0,
    rows: [],
  });
  vi.mocked(configApi.getJiraConfig).mockReset();
  vi.mocked(configApi.getJiraConfig).mockResolvedValue({
    tool: "none",
    base_url: null,
    list_jql: null,
  });
});

afterEach(() => {
  cleanup();
});

describe("Hub — Sync button", () => {
  test("hidden when no repos configured", async () => {
    vi.mocked(reposApi.listRepos).mockResolvedValue([]);
    renderHub();
    await waitFor(() => {
      expect(screen.getByText(/No repos configured yet/i)).toBeInTheDocument();
    });
    expect(
      screen.queryByRole("button", { name: /^sync$/i }),
    ).not.toBeInTheDocument();
  });

  test("shown when repos exist; clicking fires syncWorktrees", async () => {
    vi.mocked(reposApi.listRepos).mockResolvedValue([
      {
        name: "myrepo",
        path: "/tmp/r",
        default_branch: "main",
        branch_prefix: "",
        worktree_path_template: "x",
        setup_steps: [],
        ticket_pattern: null,
      },
    ]);
    vi.mocked(worktreesApi.syncWorktrees).mockResolvedValue({
      imported: [
        {
          repo: "myrepo",
          name: "feature1",
          path: "/tmp/r_worktree_feature1",
          branch: "feature1",
          ticket: null,
        },
      ],
      removed: [
        {
          repo: "myrepo",
          name: "feature_gone",
          path: "/tmp/r_worktree_feature_gone",
          reason: "missing from git worktree list",
        },
      ],
      skipped: [
        { repo: "myrepo", path: "/tmp/r", reason: "main checkout" },
      ],
    });
    renderHub();

    const btn = await screen.findByRole("button", { name: /^sync$/i });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(worktreesApi.syncWorktrees).toHaveBeenCalled();
    });

    // The summary reflects the worktrees-sync result.
    await waitFor(() => {
      expect(
        screen.getByText(/imported 1.*removed 1.*skipped 1/i),
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/main checkout/i)).toBeInTheDocument();
  });
});
