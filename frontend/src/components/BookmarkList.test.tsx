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

vi.mock("../api/bookmarks");

import * as bookmarksApi from "../api/bookmarks";

import { BookmarkList } from "./BookmarkList";
import type { BookmarkPr, JiraConfig } from "../api/types";

function renderBookmarks(
  bookmarks: BookmarkPr[],
  jira: JiraConfig | null = null,
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RadixTooltip.Provider>
        <BookmarkList jira={jira} bookmarksOverride={bookmarks} />
      </RadixTooltip.Provider>
    </QueryClientProvider>,
  );
}

function bookmark(overrides: Partial<BookmarkPr> = {}): BookmarkPr {
  return {
    pr_repo: "o/r",
    pr_number: 1,
    title: "default title",
    author_login: "alice",
    url: "https://github.com/o/r/pull/1",
    state: "open",
    notes: null,
    ticket: null,
    bookmarked_at: "2026-05-21T00:00:00Z",
    last_refreshed_at: "2026-05-21T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(bookmarksApi.addBookmark).mockReset();
  vi.mocked(bookmarksApi.deleteBookmark).mockReset();
  vi.mocked(bookmarksApi.updateBookmarkNotes).mockReset();
  vi.mocked(bookmarksApi.pullDownBookmark).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("BookmarkList", () => {
  test("renders empty state when no bookmarks exist", () => {
    renderBookmarks([]);
    expect(screen.getByText(/^Bookmarks$/)).toBeInTheDocument();
    expect(screen.getByText(/no bookmarks yet/i)).toBeInTheDocument();
  });

  test("renders the add-bookmark URL input even when empty", () => {
    renderBookmarks([]);
    expect(screen.getByPlaceholderText(/paste a github pr url/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /\+ bookmark pr/i })).toBeInTheDocument();
  });

  test("renders bookmark rows with state + bookmark chip", () => {
    renderBookmarks([
      bookmark({ pr_number: 1, title: "open PR", state: "open" }),
      bookmark({ pr_number: 2, title: "merged PR", state: "merged" }),
      bookmark({ pr_number: 3, title: "closed PR", state: "closed" }),
    ]);
    expect(screen.getByText("open PR")).toBeInTheDocument();
    expect(screen.getByText("merged PR")).toBeInTheDocument();
    expect(screen.getByText("closed PR")).toBeInTheDocument();
    // State chip per row
    expect(screen.getByText("open")).toBeInTheDocument();
    expect(screen.getByText("merged")).toBeInTheDocument();
    expect(screen.getByText("closed")).toBeInTheDocument();
    // Bookmark chip on every row
    expect(screen.getAllByText("bookmark")).toHaveLength(3);
  });

  test("submits the URL and clears the input on success", async () => {
    vi.mocked(bookmarksApi.addBookmark).mockResolvedValue(
      bookmark({ pr_repo: "acme/myapp", pr_number: 42, title: "added" }),
    );
    renderBookmarks([]);

    const input = screen.getByPlaceholderText(/paste a github pr url/i);
    fireEvent.change(input, {
      target: { value: "https://github.com/acme/myapp/pull/42" },
    });
    fireEvent.click(screen.getByRole("button", { name: /\+ bookmark pr/i }));

    await waitFor(() => {
      expect(bookmarksApi.addBookmark).toHaveBeenCalledWith(
        "https://github.com/acme/myapp/pull/42",
      );
    });
    await waitFor(() => {
      expect(input).toHaveValue("");
    });
  });

  test("renders ticket as Jira link when jira config has base_url", () => {
    renderBookmarks(
      [bookmark({ pr_number: 1, ticket: "PROJ-218" })],
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

  test("Pull-down click fires the API and surfaces a link to the workspace on success", async () => {
    vi.mocked(bookmarksApi.pullDownBookmark).mockResolvedValue({
      repo: "myapp",
      name: "feat_x",
    });
    renderBookmarks([
      bookmark({ pr_repo: "acme/myapp", pr_number: 42 }),
    ]);
    const btn = screen.getByRole("button", { name: /^pull down$/i });
    expect(btn).toBeEnabled();
    fireEvent.click(btn);

    await waitFor(() => {
      expect(bookmarksApi.pullDownBookmark).toHaveBeenCalledWith(
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

  test("Unbookmark click fires the API", async () => {
    vi.mocked(bookmarksApi.deleteBookmark).mockResolvedValue({ deleted: true });
    renderBookmarks([
      bookmark({ pr_repo: "acme/myapp", pr_number: 42 }),
    ]);
    const btn = screen.getByRole("button", { name: /^unbookmark$/i });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(bookmarksApi.deleteBookmark).toHaveBeenCalledWith(
        "acme/myapp",
        42,
      );
    });
  });

  test("PR link button opens the PR URL in a new tab", () => {
    renderBookmarks([
      bookmark({
        pr_repo: "acme/myapp",
        pr_number: 99,
        url: "https://github.com/acme/myapp/pull/99",
      }),
    ]);
    const prLink = screen.getByRole("link", { name: /^pr$/i });
    expect(prLink).toHaveAttribute("href", "https://github.com/acme/myapp/pull/99");
    expect(prLink).toHaveAttribute("target", "_blank");
  });

  test("notes editor renders with existing notes", () => {
    renderBookmarks([
      bookmark({ pr_number: 1, notes: "follow up next week" }),
    ]);
    // Two textbox roles render: the URL input and the notes textarea.
    // Disambiguate by placeholder.
    const textarea = screen.getByPlaceholderText("+ Add note");
    expect(textarea).toHaveValue("follow up next week");
  });
});
