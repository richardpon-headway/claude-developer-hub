import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("../api/config");

import * as configApi from "../api/config";

import { AskClaudeTile } from "./AskClaudeTile";

function renderTile() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RadixTooltip.Provider>
        <AskClaudeTile />
      </RadixTooltip.Provider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(configApi.runGlobalFreeform).mockReset();
  vi.mocked(configApi.openGlobalClaude).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("AskClaudeTile", () => {
  test("renders the freeform input", () => {
    renderTile();
    expect(screen.getByLabelText(/ask claude/i)).toBeInTheDocument();
  });

  test("freeform Run button is disabled until the user types something", () => {
    renderTile();
    const input = screen.getByLabelText(/ask claude/i);
    const runBtn = screen.getByRole("button", { name: /^run$/i });
    expect(runBtn).toBeDisabled();

    fireEvent.change(input, { target: { value: "what's up" } });
    expect(runBtn).toBeEnabled();

    // Whitespace-only re-disables (the trim() check).
    fireEvent.change(input, { target: { value: "   " } });
    expect(runBtn).toBeDisabled();
  });

  test("freeform submit calls the api with the trimmed prompt and clears the input on success", async () => {
    vi.mocked(configApi.runGlobalFreeform).mockResolvedValue({ spawned: true });

    renderTile();
    const input = screen.getByLabelText(/ask claude/i) as HTMLInputElement;
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

  test("plain Enter inserts a newline; Cmd+Enter submits", async () => {
    vi.mocked(configApi.runGlobalFreeform).mockResolvedValue({ spawned: true });

    renderTile();
    const input = screen.getByLabelText(/ask claude/i);
    fireEvent.change(input, { target: { value: "hello" } });

    // Plain Enter is a no-op (default textarea newline behavior).
    fireEvent.keyDown(input, { key: "Enter" });
    expect(configApi.runGlobalFreeform).not.toHaveBeenCalled();

    // Cmd+Enter (macOS) submits.
    fireEvent.keyDown(input, { key: "Enter", metaKey: true });
    await waitFor(() => {
      expect(configApi.runGlobalFreeform).toHaveBeenCalledWith("hello");
    });
  });

  test("Ctrl+Enter also submits (cross-platform)", async () => {
    vi.mocked(configApi.runGlobalFreeform).mockResolvedValue({ spawned: true });

    renderTile();
    const input = screen.getByLabelText(/ask claude/i);
    fireEvent.change(input, { target: { value: "hi" } });
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });

    await waitFor(() => {
      expect(configApi.runGlobalFreeform).toHaveBeenCalledWith("hi");
    });
  });

  test("Open Claude button fires the blank-session api (no prompt needed)", async () => {
    vi.mocked(configApi.openGlobalClaude).mockResolvedValue({ spawned: true });

    renderTile();
    const btn = screen.getByRole("button", { name: /^open claude$/i });
    // Enabled even with an empty freeform input — it takes no prompt.
    expect(btn).toBeEnabled();
    fireEvent.click(btn);

    await waitFor(() => {
      expect(configApi.openGlobalClaude).toHaveBeenCalledTimes(1);
    });
  });
});
