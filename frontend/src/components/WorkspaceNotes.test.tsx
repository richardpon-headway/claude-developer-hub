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

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(worktreesApi.updateNotes).mockReset();
  vi.mocked(worktreesApi.updateNotes).mockResolvedValue({ notes: "" });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("WorkspaceNotes", () => {
  test("does not save on initial mount when value matches props", async () => {
    renderNotes({ notes: "existing note" });
    // Even after the full debounce window, no save fires for a no-op
    // mount — initial value == committed.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(worktreesApi.updateNotes).not.toHaveBeenCalled();
  });

  test("debounced save fires ~1s after the last keystroke", async () => {
    renderNotes({ notes: null });
    const textarea = screen.getByRole("textbox");

    fireEvent.change(textarea, { target: { value: "first" } });
    // Mid-debounce: nothing yet.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(worktreesApi.updateNotes).not.toHaveBeenCalled();

    // Another edit before the timer fires — the previous timer is
    // cancelled and a new one starts.
    fireEvent.change(textarea, { target: { value: "first edit" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(worktreesApi.updateNotes).not.toHaveBeenCalled();

    // Now settle past the debounce window.
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
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;

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

  test("renders markdown preview when the textarea is blurred and has content", async () => {
    renderNotes({ notes: "**bold** and a [link](https://example.com)" });
    // The textarea is unfocused by default, so the preview should
    // render. `<strong>` confirms markdown actually compiled.
    expect(screen.getByText("bold").tagName).toBe("STRONG");
    expect(screen.getByRole("link", { name: "link" })).toHaveAttribute(
      "href",
      "https://example.com",
    );
  });

  test("save failure surfaces a 'save failed' status with the error detail in title", async () => {
    vi.mocked(worktreesApi.updateNotes).mockRejectedValue(
      new ApiError(500, "boom"),
    );
    renderNotes({ notes: null });
    const textarea = screen.getByRole("textbox");

    fireEvent.change(textarea, { target: { value: "x" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    expect(worktreesApi.updateNotes).toHaveBeenCalled();

    const status = screen.getByText(/save failed/i);
    expect(status).toHaveAttribute("title", "boom");
  });
});
