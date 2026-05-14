import { render, screen, cleanup, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, test } from "vitest";

import { InboxList } from "./InboxList";
import type { InboxPr } from "../api/types";

function renderInbox(prs: InboxPr[]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <InboxList inboxOverride={{ prs, checked_at: "2026-05-14T00:00:00Z" }} />
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
        source: "team:acme/corrections",
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
        source: "team:acme/corrections",
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
});
