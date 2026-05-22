import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("../api/inbox");

import * as inboxApi from "../api/inbox";

import { InboxList } from "./InboxList";
import type { InboxPr, JiraConfig } from "../api/types";

function renderInbox(prs: InboxPr[], jira: JiraConfig | null = null) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RadixTooltip.Provider>
        <InboxList jira={jira} inboxOverride={{ prs }} />
      </RadixTooltip.Provider>
    </QueryClientProvider>,
  );
}

function pr(overrides: Partial<InboxPr> = {}): InboxPr {
  return {
    pr_repo: "o/r",
    pr_number: 1,
    title: "default title",
    author_login: "me",
    is_draft: false,
    url: "https://github.com/o/r/pull/1",
    ci_status: "pass",
    sources: ["reviewer"],
    notes: null,
    ticket: null,
    pr_updated_at: "2026-05-14T00:00:00Z",
    added_at: "2026-05-14T00:00:00Z",
    last_seen_at: "2026-05-14T00:00:00Z",
    repo_configured: true,
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(inboxApi.pullDownPr).mockReset();
  vi.mocked(inboxApi.configureAndPullDown).mockReset();
  vi.mocked(inboxApi.archiveInboxPr).mockReset();
  vi.mocked(inboxApi.updateInboxNotes).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("InboxList", () => {
  test("renders the empty-state when there are no PRs", () => {
    renderInbox([]);
    expect(screen.getByText(/^Inbox$/)).toBeInTheDocument();
    expect(screen.getByText(/no prs need your attention/i)).toBeInTheDocument();
  });

  test("renders source chips for reviewer / assignee / mentions", () => {
    renderInbox([
      pr({
        pr_number: 1,
        title: "Direct-reviewed",
        sources: ["reviewer"],
      }),
      pr({
        pr_number: 2,
        title: "Assigned",
        sources: ["assignee"],
      }),
      pr({
        pr_number: 3,
        title: "Mentioned",
        sources: ["mentions"],
      }),
    ]);
    expect(screen.getByText("reviewer")).toBeInTheDocument();
    expect(screen.getByText("assignee")).toBeInTheDocument();
    expect(screen.getByText("mention")).toBeInTheDocument();
  });

  test("renders multiple source chips when a PR matches multiple queries", () => {
    renderInbox([
      pr({
        pr_number: 5,
        title: "Multi-source PR",
        sources: ["reviewer", "assignee"],
      }),
    ]);
    expect(screen.getByText("reviewer")).toBeInTheDocument();
    expect(screen.getByText("assignee")).toBeInTheDocument();
  });

  test("ci status maps to a visible badge", () => {
    renderInbox([
      pr({ pr_number: 1, title: "passing", ci_status: "pass" }),
      pr({ pr_number: 2, title: "failing", ci_status: "fail" }),
    ]);
    expect(screen.getByText("ci ✓")).toBeInTheDocument();
    expect(screen.getByText("ci ✗")).toBeInTheDocument();
  });

  test("renders the ticket as a Jira link when jira config is set", () => {
    renderInbox(
      [pr({ pr_number: 1, ticket: "PROJ-218" })],
      {
        tool: "none",
        base_url: "https://acme.atlassian.net",
        list_jql: null,
      },
    );
    const link = screen.getByRole("link", { name: "PROJ-218" });
    expect(link).toHaveAttribute(
      "href",
      "https://acme.atlassian.net/browse/PROJ-218",
    );
  });

  test("renders ticket as plain text when jira config has no base_url", () => {
    renderInbox([pr({ pr_number: 1, ticket: "PROJ-218" })]);
    expect(screen.getByText(/PROJ-218/)).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "PROJ-218" }),
    ).not.toBeInTheDocument();
  });

  test("Configure-and-pull-down button shows when repo isn't configured", () => {
    renderInbox([
      pr({ pr_number: 1, title: "unconfigured PR", repo_configured: false }),
    ]);
    const btn = screen.getByRole("button", {
      name: /configure repo \+ pull down/i,
    });
    expect(btn).toBeEnabled();
  });

  test("Configure-and-pull-down click fires the API and shows opened state", async () => {
    vi.mocked(inboxApi.configureAndPullDown).mockResolvedValue({
      session_id: "sess-abc",
    });
    renderInbox([
      pr({
        pr_repo: "acme/myapp",
        pr_number: 42,
        title: "unconfigured PR",
        repo_configured: false,
      }),
    ]);
    const btn = screen.getByRole("button", {
      name: /configure repo \+ pull down/i,
    });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(inboxApi.configureAndPullDown).toHaveBeenCalledWith(
        "acme/myapp",
        42,
      );
    });
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /claude opened/i }),
      ).toBeDisabled();
    });
  });

  test("Pull-down click fires the API and disables the button on success", async () => {
    vi.mocked(inboxApi.pullDownPr).mockResolvedValue({
      repo: "myapp",
      name: "feat_x",
    });
    renderInbox([
      pr({
        pr_repo: "acme/myapp",
        pr_number: 42,
        title: "ready PR",
        repo_configured: true,
      }),
    ]);
    const btn = screen.getByRole("button", { name: /^pull down$/i });
    expect(btn).toBeEnabled();
    fireEvent.click(btn);

    await waitFor(() => {
      expect(inboxApi.pullDownPr).toHaveBeenCalledWith("acme/myapp", 42);
    });
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /pulled/i }),
      ).toBeDisabled();
    });
  });

  test("Remove (archive) button fires the API", async () => {
    vi.mocked(inboxApi.archiveInboxPr).mockResolvedValue(
      pr({ pr_repo: "acme/myapp", pr_number: 42 }),
    );
    renderInbox([
      pr({
        pr_repo: "acme/myapp",
        pr_number: 42,
        title: "archive me",
      }),
    ]);
    const btn = screen.getByRole("button", { name: /^remove$/i });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(inboxApi.archiveInboxPr).toHaveBeenCalledWith("acme/myapp", 42);
    });
  });

  test("PR link button opens the PR URL in a new tab", () => {
    renderInbox([
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

  test("notes editor renders with existing notes pre-populated", () => {
    renderInbox([
      pr({
        pr_number: 1,
        title: "PR with notes",
        notes: "blocked on COR-218",
      }),
    ]);
    // Textarea is empty by default ('+ Add note' placeholder) and
    // pre-filled when notes are present.
    const textarea = screen.getByRole("textbox");
    expect(textarea).toHaveValue("blocked on COR-218");
  });
});
