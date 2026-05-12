import { render, screen, waitFor, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { TokenUsageTile } from "./TokenUsageTile";
import * as worktreesApi from "../api/worktrees";

vi.mock("../api/worktrees");

function renderTile() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <TokenUsageTile />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(worktreesApi.getTokenUsage).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("TokenUsageTile", () => {
  test("renders the offline badge when monitor is unreachable", async () => {
    vi.mocked(worktreesApi.getTokenUsage).mockResolvedValue({
      offline: true,
      rows: [],
    });
    renderTile();
    await waitFor(() => {
      expect(screen.getByText(/monitor offline/i)).toBeInTheDocument();
    });
  });

  test("renders totals and top topics when monitor is live", async () => {
    vi.mocked(worktreesApi.getTokenUsage).mockResolvedValue({
      offline: false,
      rows: [
        {
          topic_id: "A",
          sessions: 5,
          output: 12000,
          input: 100000,
          messages: 30,
          last_at: null,
          label: "PROJ-1",
          summary: "fix the bug",
        },
        {
          topic_id: "B",
          sessions: 3,
          output: 3000,
          input: 50000,
          messages: 12,
          last_at: null,
          label: "PROJ-2",
          summary: null,
        },
      ],
    });
    renderTile();
    await waitFor(() => {
      expect(screen.getByText(/15,000/)).toBeInTheDocument(); // total output
    });
    expect(screen.getByText(/across 8 sessions/)).toBeInTheDocument();
    expect(screen.getByText("PROJ-1")).toBeInTheDocument();
    expect(screen.getByText("PROJ-2")).toBeInTheDocument();
  });
});
