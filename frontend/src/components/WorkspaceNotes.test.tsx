import { act, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("../api/worktrees");

import * as worktreesApi from "../api/worktrees";
import { ApiError } from "../api/client";

import { WorkspaceNotes } from "./WorkspaceNotes";

function renderNotes(props: {
  notes: string | null;
  variant?: "compact" | "full";
}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceNotes
        repo="myapp"
        name="feat_x"
        notes={props.notes}
        variant={props.variant ?? "compact"}
      />
    </QueryClientProvider>,
  );
}

// Helper: click the view-mode container and let React flush the state
// + the deferred focus() call, then return the textarea.
async function enterEditMode(): Promise<HTMLTextAreaElement> {
  fireEvent.click(screen.getByRole("button"));
  // setTimeout(0) inside the component defers focus; advance just
  // enough to flush it. Tests using fake timers manage their own
  // advance; tests with real timers will resolve via the microtask
  // queue alone.
  if (vi.isFakeTimers()) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
  } else {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
  return screen.getByRole("textbox") as HTMLTextAreaElement;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(worktreesApi.updateNotes).mockReset();
  vi.mocked(worktreesApi.updateNotes).mockResolvedValue({ notes: "" });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("WorkspaceNotes", () => {
  // --- mode toggle ------------------------------------------------------

  test("empty notes: view mode renders '+ Add note' placeholder, click switches to edit", async () => {
    renderNotes({ notes: null });
    expect(screen.getByText(/\+ Add note/i)).toBeInTheDocument();
    // No textarea in view mode.
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();

    const textarea = await enterEditMode();
    expect(textarea).toBeInTheDocument();
    // Placeholder is gone; focus landed on the textarea.
    expect(textarea).toHaveFocus();
  });

  test("non-empty notes: view mode renders markdown preview, click switches to edit", async () => {
    renderNotes({ notes: "**bold** thing" });
    // Preview is the rendered markdown.
    expect(screen.getByText("bold").tagName).toBe("STRONG");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();

    const textarea = await enterEditMode();
    // Edit mode shows the raw markdown source — not the rendered HTML.
    expect(textarea.value).toBe("**bold** thing");
  });

  test("blur switches back to view mode", async () => {
    renderNotes({ notes: "preview me" });
    const textarea = await enterEditMode();
    expect(textarea).toBeInTheDocument();

    fireEvent.blur(textarea);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    // Preview re-renders with the (unchanged) content.
    expect(screen.getByText("preview me")).toBeInTheDocument();
  });

  test("Escape switches back to view mode without losing the draft", async () => {
    renderNotes({ notes: "first" });
    const textarea = await enterEditMode();
    fireEvent.change(textarea, { target: { value: "first edited" } });
    fireEvent.keyDown(textarea, { key: "Escape" });
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    // Preview reflects the edited draft (it shows draft, not committed).
    expect(screen.getByText("first edited")).toBeInTheDocument();
  });

  test("clicking a link inside the preview opens the link without entering edit mode", async () => {
    renderNotes({ notes: "see [docs](https://example.com)" });
    const link = screen.getByRole("link", { name: "docs" });
    expect(link).toHaveAttribute("href", "https://example.com");
    expect(link).toHaveAttribute("target", "_blank");

    fireEvent.click(link);
    // Edit mode did NOT activate (link click propagation was stopped).
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  // --- auto-save --------------------------------------------------------

  test("does not save on initial mount when value matches props", async () => {
    renderNotes({ notes: "existing note" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(worktreesApi.updateNotes).not.toHaveBeenCalled();
  });

  test("debounced save fires ~1s after the last keystroke", async () => {
    renderNotes({ notes: null });
    const textarea = await enterEditMode();

    fireEvent.change(textarea, { target: { value: "first" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(worktreesApi.updateNotes).not.toHaveBeenCalled();

    fireEvent.change(textarea, { target: { value: "first edit" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(worktreesApi.updateNotes).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    expect(worktreesApi.updateNotes).toHaveBeenCalledWith(
      "myapp",
      "feat_x",
      "first edit",
    );
    expect(worktreesApi.updateNotes).toHaveBeenCalledTimes(1);
  });

  test("empty string is a valid save (clears the note)", async () => {
    renderNotes({ notes: "delete me" });
    const textarea = await enterEditMode();

    fireEvent.change(textarea, { target: { value: "" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    expect(worktreesApi.updateNotes).toHaveBeenCalledWith(
      "myapp",
      "feat_x",
      "",
    );
  });

  test("save failure surfaces a 'save failed' status with the error detail in title", async () => {
    vi.mocked(worktreesApi.updateNotes).mockRejectedValue(
      new ApiError(500, "boom"),
    );
    renderNotes({ notes: null });
    const textarea = await enterEditMode();

    fireEvent.change(textarea, { target: { value: "x" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    expect(worktreesApi.updateNotes).toHaveBeenCalled();

    const status = screen.getByText(/save failed/i);
    expect(status).toHaveAttribute("title", "boom");
  });
});
