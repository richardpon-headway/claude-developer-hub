import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("../api/authored");
vi.mock("../api/inbox");

import * as authoredApi from "../api/authored";
import * as inboxApi from "../api/inbox";

import { AuthoredPrTier } from "./AuthoredPrTier";
import type { AuthoredPr, JiraConfig } from "../api/types";

function renderTier(
  rows: AuthoredPr[],
  jira: JiraConfig | null = null,
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RadixTooltip.Provider>
        <AuthoredPrTier jira={jira} authoredOverride={rows} />
      </RadixTooltip.Provider>
    </QueryClientProvider>,
  );
}

function pr(overrides: Partial<AuthoredPr> = {}): AuthoredPr {
  return {
    pr_repo: "acme/myapp",
    pr_number: 1,
    title: "default title",
    url: "https://github.com/acme/myapp/pull/1",
    is_draft: false,
    ci_status: "pass",
    ticket: null,
    pr_updated_at: "2026-05-21T00:00:00Z",
    repo_configured: true,
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(authoredApi.pullDownAuthoredPr).mockReset();
  vi.mocked(inboxApi.configureAndPullDown).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("AuthoredPrTier", () => {
  test("renders nothing when there are no authored PRs (clean hub)", () => {
    const { container } = renderTier([]);
    expect(container.firstChild).toBeNull();
  });

  test("renders the tier heading and rows when populated", () => {
    renderTier([
      pr({ pr_number: 1, title: "authored A" }),
      pr({ pr_number: 2, title: "authored B" }),
    ]);
    expect(screen.getByText(/my prs \(no worktree\)/i)).toBeInTheDocument();
    expect(screen.getByText("authored A")).toBeInTheDocument();
    expect(screen.getByText("authored B")).toBeInTheDocument();
  });

  test("renders the draft chip on draft PRs", () => {
    renderTier([pr({ pr_number: 1, is_draft: true })]);
    expect(screen.getByText("draft")).toBeInTheDocument();
  });

  test("renders ticket as Jira link when jira is set", () => {
    renderTier(
      [pr({ pr_number: 1, ticket: "PROJ-9" })],
      { tool: "none", base_url: "https://acme.atlassian.net", list_jql: null },
    );
    const link = screen.getByRole("link", { name: "PROJ-9" });
    expect(link).toHaveAttribute(
      "href",
      "https://acme.atlassian.net/browse/PROJ-9",
    );
  });

  test("Pull-down click fires the authored endpoint", async () => {
    vi.mocked(authoredApi.pullDownAuthoredPr).mockResolvedValue({
      repo: "myapp",
      name: "feat_x",
    });
    renderTier([
      pr({ pr_repo: "acme/myapp", pr_number: 42, repo_configured: true }),
    ]);
    fireEvent.click(screen.getByRole("button", { name: /^pull down$/i }));

    await waitFor(() => {
      expect(authoredApi.pullDownAuthoredPr).toHaveBeenCalledWith(
        "acme/myapp",
        42,
      );
    });
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /pulled/i }),
      ).toBeDisabled();
    });
  });

  test("unconfigured repo falls back to Configure + pull down", async () => {
    vi.mocked(inboxApi.configureAndPullDown).mockResolvedValue({
      session_id: "sess",
    });
    renderTier([
      pr({ pr_repo: "other/elsewhere", pr_number: 9, repo_configured: false }),
    ]);
    const btn = screen.getByRole("button", {
      name: /configure repo \+ pull down/i,
    });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(inboxApi.configureAndPullDown).toHaveBeenCalledWith(
        "other/elsewhere",
        9,
      );
    });
  });
});
