import { render, screen, cleanup, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("../api/inbox");

import * as inboxApi from "../api/inbox";

import { InboxList } from "./InboxList";
import type { InboxPr } from "../api/types";

function renderInbox(prs: InboxPr[]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RadixTooltip.Provider>
        <InboxList inboxOverride={{ prs, checked_at: "2026-05-14T00:00:00Z" }} />
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
    head_ref: "feat/x",
    base_ref: "main",
    is_draft: false,
    url: "https://github.com/o/r/pull/1",
    updated_at: "2026-05-14T00:00:00Z",
    ci_status: "pass",
    source: "author",
    stack_top_pr_number: null,
    stack_size: 1,
    stack_position: 1,
    repo_configured: true,
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(inboxApi.pullDownPr).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("InboxList", () => {
  test("renders nothing when there are no PRs", () => {
    const { container } = renderInbox([]);
    expect(container.firstChild).toBeNull();
  });

  test("groups by source: authored vs reviewer subsections", () => {
    renderInbox([
      pr({ pr_number: 1, title: "My PR", source: "author" }),
      pr({
        pr_number: 2,
        title: "Their PR",
        source: "team:headway/corrections",
        head_ref: "feat/their",
      }),
    ]);
    expect(screen.getByText(/\[YOU AUTHORED\]/i)).toBeInTheDocument();
    expect(screen.getByText(/\[REVIEWER\]/i)).toBeInTheDocument();
    expect(screen.getByText("My PR")).toBeInTheDocument();
    expect(screen.getByText("Their PR")).toBeInTheDocument();
  });

  test("renders the source chip per row (team name for team:, 'me' otherwise)", () => {
    renderInbox([
      pr({ pr_number: 1, title: "Authored", source: "author" }),
      pr({
        pr_number: 2,
        title: "Team-reviewed",
        source: "team:headway/corrections",
        head_ref: "feat/y",
      }),
      pr({
        pr_number: 3,
        title: "Direct-reviewed",
        source: "reviewer",
        head_ref: "feat/z",
      }),
    ]);
    // Two "me" chips (author + direct reviewer) + one "corrections"
    expect(screen.getAllByText("me")).toHaveLength(2);
    expect(screen.getByText("corrections")).toBeInTheDocument();
  });

  test("ci status maps to a visible badge", () => {
    renderInbox([
      pr({ pr_number: 1, title: "passing", ci_status: "pass" }),
      pr({
        pr_number: 2,
        title: "failing",
        ci_status: "fail",
        head_ref: "feat/fail",
      }),
    ]);
    expect(screen.getByText("ci ✓")).toBeInTheDocument();
    expect(screen.getByText("ci ✗")).toBeInTheDocument();
  });

  test("stack of 3 renders in a bordered group with a Graphite-linked title", () => {
    const stack: InboxPr[] = [
      pr({
        pr_number: 10,
        title: "bottom",
        head_ref: "feat/a",
        base_ref: "main",
        stack_top_pr_number: 12,
        stack_size: 3,
        stack_position: 1,
        source: "reviewer",
      }),
      pr({
        pr_number: 11,
        title: "middle",
        head_ref: "feat/b",
        base_ref: "feat/a",
        stack_top_pr_number: 12,
        stack_size: 3,
        stack_position: 2,
        source: "reviewer",
      }),
      pr({
        pr_number: 12,
        title: "top",
        head_ref: "feat/c",
        base_ref: "feat/b",
        stack_top_pr_number: 12,
        stack_size: 3,
        stack_position: 3,
        source: "reviewer",
      }),
    ];
    renderInbox(stack);

    const stackTitle = screen.getByRole("link", { name: /Graphite · 3-PR stack/i });
    expect(stackTitle).toBeInTheDocument();
    expect(stackTitle).toHaveAttribute(
      "href",
      "https://app.graphite.com/github/pr/o/r/12",
    );

    // Top of stack reads first inside the box.
    const stackBox = stackTitle.parentElement!;
    const items = within(stackBox).getAllByRole("link", { name: /top|middle|bottom/ });
    expect(items.map((el) => el.textContent)).toEqual(["top", "middle", "bottom"]);
  });

  test("single PR renders without a stack box", () => {
    renderInbox([pr({ pr_number: 7, title: "lone PR" })]);
    expect(screen.queryByRole("link", { name: /Graphite/ })).not.toBeInTheDocument();
    expect(screen.getByText("lone PR")).toBeInTheDocument();
  });

  test("Pull-down button is disabled when repo isn't configured", () => {
    renderInbox([
      pr({ pr_number: 1, title: "unconfigured PR", repo_configured: false }),
    ]);
    const btn = screen.getByRole("button", { name: /configure first/i });
    expect(btn).toBeDisabled();
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
});
