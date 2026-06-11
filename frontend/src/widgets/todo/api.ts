// API client for the todo widget. Mirrors the backend's
// /api/widgets/todo surface; kept inside the widget folder so the
// widget stays self-contained (only the shared fetch primitives are
// imported from core).

import { apiDelete, apiGet, apiPatch, apiPost } from "../../api/client";

export interface TodoItem {
  id: number;
  // Free-form, multi-line text (the item's full content).
  title: string;
  done: boolean;
  sort_order: number;
  completed_at: string | null;
  created_at: string;
}

export interface TodoList {
  pending: TodoItem[];
  completed: TodoItem[];
}

export interface CreateTodoBody {
  title?: string;
}

// PATCH body — send only the fields that changed (autosave).
export interface UpdateTodoBody {
  title?: string;
  done?: boolean;
}

const BASE = "/api/widgets/todo";

export const listTodos = () => apiGet<TodoList>(`${BASE}/items`);

export const createTodo = (body: CreateTodoBody = {}) =>
  apiPost<TodoItem>(`${BASE}/items`, body);

export const updateTodo = (id: number, body: UpdateTodoBody) =>
  apiPatch<TodoItem>(`${BASE}/items/${id}`, body);

export const deleteTodo = (id: number) =>
  apiDelete<{ deleted: true }>(`${BASE}/items/${id}`);

export const reorderTodos = (ids: number[]) =>
  apiPost<TodoList>(`${BASE}/reorder`, { ids });

// Shared react-query key so every mutation invalidates the same cache.
export const TODOS_QUERY_KEY = ["widgets", "todo"] as const;
