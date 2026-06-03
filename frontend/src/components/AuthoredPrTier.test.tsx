import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// Stub Link so PrCard's "Pulled" link renders without a router context.
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

vi.mock("../api/authored");

import * as authoredApi from "../api/authored";

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
    notes: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(authoredApi.pullDownAuthoredPr).mockReset();
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
    // Post-success the affordance flips to a link to the new
    // workspace's detail page (plan-67). The Link stub above renders
    // `to` literally without interpolating params.
    await waitFor(() => {
      const pulled = screen.getByRole("link", { name: /pulled/i });
      expect(pulled).toHaveAttribute("href", "/workspace/$repo/$name");
    });
  });

  test("PR link button opens the PR URL in a new tab", () => {
    renderTier([
      pr({
        pr_repo: "acme/myapp",
        pr_number: 42,
        url: "https://github.com/acme/myapp/pull/42",
      }),
    ]);
    const prLink = screen.getByRole("link", { name: /^pr$/i });
    expect(prLink).toHaveAttribute("href", "https://github.com/acme/myapp/pull/42");
    expect(prLink).toHaveAttribute("target", "_blank");
  });

  test("unconfigured repo still shows a plain Pull down button", () => {
    // The "Configure repo + pull down" onboarding flow was removed with
    // the inbox; repo_configured no longer changes the affordance. The
    // backend 400s on click if the repo isn't configured.
    renderTier([
      pr({ pr_repo: "other/elsewhere", pr_number: 9, repo_configured: false }),
    ]);
    expect(
      screen.getByRole("button", { name: /^pull down$/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /configure repo/i }),
    ).not.toBeInTheDocument();
  });
});
