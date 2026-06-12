import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { TodoWidget } from "./TodoWidget";
import * as api from "./api";
import type { TodoItem, TodoList } from "./api";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof api>();
  return {
    ...actual,
    listTodos: vi.fn(),
    createTodo: vi.fn(),
    updateTodo: vi.fn(),
    deleteTodo: vi.fn(),
    reorderTodos: vi.fn(),
  };
});

function item(over: Partial<TodoItem> & { id: number }): TodoItem {
  return {
    title: "",
    done: false,
    sort_order: 0,
    completed_at: null,
    created_at: "2026-06-10T00:00:00Z",
    ...over,
  };
}

function renderWidget() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <TodoWidget />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(api.listTodos).mockReset();
  vi.mocked(api.createTodo).mockReset();
  vi.mocked(api.updateTodo).mockReset();
  vi.mocked(api.deleteTodo).mockReset();
  vi.mocked(api.reorderTodos).mockReset();
});

afterEach(() => cleanup());

const EMPTY: TodoList = { pending: [], completed: [] };

describe("TodoWidget", () => {
  test("renders pending items and a completed section", async () => {
    vi.mocked(api.listTodos).mockResolvedValue({
      pending: [item({ id: 1, title: "write the widget" })],
      completed: [item({ id: 2, title: "draft the plan", done: true })],
    });
    renderWidget();

    await waitFor(() => {
      expect(screen.getByText("write the widget")).toBeInTheDocument();
    });
    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(screen.getByText("draft the plan")).toBeInTheDocument();
  });

  test("renders a URL in the title as a clickable link", async () => {
    vi.mocked(api.listTodos).mockResolvedValue({
      pending: [item({ id: 1, title: "review https://example.com/pr/9" })],
      completed: [],
    });
    renderWidget();

    await waitFor(() => {
      expect(
        screen.getByRole("link", { name: "https://example.com/pr/9" }),
      ).toHaveAttribute("href", "https://example.com/pr/9");
    });
  });

  test("clicking + Add todo creates an item", async () => {
    vi.mocked(api.listTodos).mockResolvedValue(EMPTY);
    vi.mocked(api.createTodo).mockResolvedValue(item({ id: 5 }));
    renderWidget();

    await waitFor(() =>
      expect(screen.getByText("Nothing pending.")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByText("+ Add todo"));
    await waitFor(() => expect(api.createTodo).toHaveBeenCalledTimes(1));
  });

  test("checking an item marks it done", async () => {
    vi.mocked(api.listTodos).mockResolvedValue({
      pending: [item({ id: 1, title: "finish me" })],
      completed: [],
    });
    vi.mocked(api.updateTodo).mockResolvedValue(
      item({ id: 1, title: "finish me", done: true }),
    );
    renderWidget();

    const checkbox = await screen.findByLabelText("mark as done");
    fireEvent.click(checkbox);
    await waitFor(() =>
      expect(api.updateTodo).toHaveBeenCalledWith(1, { done: true }),
    );
  });

  test("completed items have a delete affordance; pending items do not", async () => {
    vi.mocked(api.listTodos).mockResolvedValue({
      pending: [item({ id: 1, title: "still going" })],
      completed: [item({ id: 7, title: "all done", done: true })],
    });
    vi.mocked(api.deleteTodo).mockResolvedValue({ deleted: true });
    renderWidget();

    await waitFor(() => expect(screen.getByText("still going")).toBeInTheDocument());
    // Exactly one ✕ — for the completed item, not the pending one.
    const deletes = screen.getAllByLabelText("delete todo");
    expect(deletes).toHaveLength(1);

    fireEvent.click(deletes[0]);
    await waitFor(() => expect(api.deleteTodo).toHaveBeenCalledWith(7));
  });
});
