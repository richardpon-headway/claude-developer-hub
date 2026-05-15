import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("../api/config");

import * as configApi from "../api/config";

import { GlobalSkillsTile } from "./GlobalSkillsTile";

function renderTile() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RadixTooltip.Provider>
        <GlobalSkillsTile />
      </RadixTooltip.Provider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(configApi.getGlobalSkills).mockReset();
  vi.mocked(configApi.runGlobalSkill).mockReset();
  vi.mocked(configApi.runGlobalFreeform).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("GlobalSkillsTile", () => {
  test("renders the tile with the freeform input even when no skills are configured", async () => {
    vi.mocked(configApi.getGlobalSkills).mockResolvedValue([]);
    renderTile();
    await waitFor(() => {
      expect(configApi.getGlobalSkills).toHaveBeenCalled();
    });
    // The freeform "Ask Claude" surface is independent of the skill list.
    expect(screen.getByLabelText(/ask claude/i)).toBeInTheDocument();
  });

  test("renders one button per configured skill and clicking fires the api", async () => {
    vi.mocked(configApi.getGlobalSkills).mockResolvedValue([
      {
        name: "pr-check-action-required",
        label: "Check action required",
        description: "Open PRs needing attention",
        cwd: "home",
      },
    ]);
    vi.mocked(configApi.runGlobalSkill).mockResolvedValue({
      window_id: "W",
      claude_session_id: "S",
    });

    renderTile();
    const btn = await screen.findByRole("button", { name: /check action required/i });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(configApi.runGlobalSkill).toHaveBeenCalledWith(
        "pr-check-action-required",
      );
    });
  });

  test("freeform Run button is disabled until the user types something", async () => {
    vi.mocked(configApi.getGlobalSkills).mockResolvedValue([]);
    renderTile();
    const input = await screen.findByLabelText(/ask claude/i);
    const runBtn = screen.getByRole("button", { name: /^run$/i });
    expect(runBtn).toBeDisabled();

    fireEvent.change(input, { target: { value: "what's up" } });
    expect(runBtn).toBeEnabled();

    // Whitespace-only re-disables (the trim() check).
    fireEvent.change(input, { target: { value: "   " } });
    expect(runBtn).toBeDisabled();
  });

  test("freeform submit calls the api with the trimmed prompt and clears the input on success", async () => {
    vi.mocked(configApi.getGlobalSkills).mockResolvedValue([]);
    vi.mocked(configApi.runGlobalFreeform).mockResolvedValue({
      window_id: "W",
      claude_session_id: "S",
    });

    renderTile();
    const input = (await screen.findByLabelText(/ask claude/i)) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "  summarize my inbox  " } });
    fireEvent.click(screen.getByRole("button", { name: /^run$/i }));

    await waitFor(() => {
      // Trimmed before the API call.
      expect(configApi.runGlobalFreeform).toHaveBeenCalledWith("summarize my inbox");
    });
    await waitFor(() => {
      expect(input.value).toBe("");
    });
  });

  test("Enter in the input submits the freeform prompt", async () => {
    vi.mocked(configApi.getGlobalSkills).mockResolvedValue([]);
    vi.mocked(configApi.runGlobalFreeform).mockResolvedValue({
      window_id: "W",
      claude_session_id: "S",
    });

    renderTile();
    const input = await screen.findByLabelText(/ask claude/i);
    fireEvent.change(input, { target: { value: "hello" } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => {
      expect(configApi.runGlobalFreeform).toHaveBeenCalledWith("hello");
    });
  });
});
