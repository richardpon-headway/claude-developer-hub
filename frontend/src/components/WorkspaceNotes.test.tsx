import { act, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("../api/worktrees");

import * as worktreesApi from "../api/worktrees";
import { ApiError } from "../api/client";

import { WorkspaceNotes } from "./WorkspaceNotes";

function renderNotes(props: { notes: string | null }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceNotes
        repo="myapp"
        name="feat_x"
        notes={props.notes}
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
  test("renders an empty textarea with '+ Add note' placeholder when notes is null", () => {
    renderNotes({ notes: null });
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(textarea.value).toBe("");
    expect(textarea).toHaveAttribute("placeholder", "+ Add note");
  });

  test("renders existing notes value in the textarea", () => {
    renderNotes({ notes: "blocking PROJ-218" });
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(textarea.value).toBe("blocking PROJ-218");
  });

  test("does not save on initial mount when value matches props", async () => {
    renderNotes({ notes: "existing note" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(worktreesApi.updateNotes).not.toHaveBeenCalled();
  });

  test("debounced save fires ~1s after the last keystroke", async () => {
    renderNotes({ notes: null });
    const textarea = screen.getByRole("textbox");

    fireEvent.change(textarea, { target: { value: "first" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(worktreesApi.updateNotes).not.toHaveBeenCalled();

    // Another edit before the timer fires — previous timer cancelled.
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
    const textarea = screen.getByRole("textbox");

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
