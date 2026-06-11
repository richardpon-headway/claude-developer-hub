import { useState } from "react";
import {
  DndContext,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  TODOS_QUERY_KEY,
  createTodo,
  deleteTodo,
  listTodos,
  reorderTodos,
  updateTodo,
  type TodoItem,
  type TodoList,
  type UpdateTodoBody,
} from "./api";
import { TodoCard, SortableTodoItem } from "./TodoItem";

// Apply a PATCH body to the cached list, moving the item between the
// pending and completed sections when `done` flips. Pure — used for
// optimistic cache updates so edits feel instant.
function applyPatch(list: TodoList, id: number, body: UpdateTodoBody): TodoList {
  const all = [...list.pending, ...list.completed];
  const target = all.find((i) => i.id === id);
  if (!target) return list;

  const updated: TodoItem = {
    ...target,
    ...(body.title !== undefined ? { title: body.title } : {}),
    ...(body.bullets !== undefined ? { bullets: body.bullets } : {}),
  };

  if (body.done !== undefined && body.done !== target.done) {
    updated.done = body.done;
    updated.completed_at = body.done ? new Date().toISOString() : null;
    if (body.done) {
      return {
        pending: list.pending.filter((i) => i.id !== id),
        completed: [updated, ...list.completed],
      };
    }
    return {
      pending: [...list.pending.filter((i) => i.id !== id), updated],
      completed: list.completed.filter((i) => i.id !== id),
    };
  }

  return {
    pending: list.pending.map((i) => (i.id === id ? updated : i)),
    completed: list.completed.map((i) => (i.id === id ? updated : i)),
  };
}

export function TodoWidget() {
  const queryClient = useQueryClient();
  const [focusId, setFocusId] = useState<number | null>(null);

  const query = useQuery({ queryKey: TODOS_QUERY_KEY, queryFn: listTodos });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: TODOS_QUERY_KEY });

  const patch = useMutation({
    mutationFn: ({ id, body }: { id: number; body: UpdateTodoBody }) =>
      updateTodo(id, body),
    onMutate: async ({ id, body }) => {
      await queryClient.cancelQueries({ queryKey: TODOS_QUERY_KEY });
      const prev = queryClient.getQueryData<TodoList>(TODOS_QUERY_KEY);
      if (prev) {
        queryClient.setQueryData<TodoList>(
          TODOS_QUERY_KEY,
          applyPatch(prev, id, body),
        );
      }
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(TODOS_QUERY_KEY, ctx.prev);
    },
    onSettled: invalidate,
  });

  const remove = useMutation({
    mutationFn: (id: number) => deleteTodo(id),
    onMutate: async (id) => {
      await queryClient.cancelQueries({ queryKey: TODOS_QUERY_KEY });
      const prev = queryClient.getQueryData<TodoList>(TODOS_QUERY_KEY);
      if (prev) {
        queryClient.setQueryData<TodoList>(TODOS_QUERY_KEY, {
          pending: prev.pending.filter((i) => i.id !== id),
          completed: prev.completed.filter((i) => i.id !== id),
        });
      }
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(TODOS_QUERY_KEY, ctx.prev);
    },
    onSettled: invalidate,
  });

  const reorder = useMutation({
    mutationFn: (ids: number[]) => reorderTodos(ids),
    onMutate: async (ids) => {
      await queryClient.cancelQueries({ queryKey: TODOS_QUERY_KEY });
      const prev = queryClient.getQueryData<TodoList>(TODOS_QUERY_KEY);
      if (prev) {
        const byId = new Map(prev.pending.map((i) => [i.id, i]));
        const reordered = ids
          .map((id) => byId.get(id))
          .filter((i): i is TodoItem => i !== undefined);
        queryClient.setQueryData<TodoList>(TODOS_QUERY_KEY, {
          ...prev,
          pending: reordered,
        });
      }
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(TODOS_QUERY_KEY, ctx.prev);
    },
    onSettled: invalidate,
  });

  const create = useMutation({
    mutationFn: () => createTodo({}),
    onSuccess: (item) => {
      setFocusId(item.id);
      invalidate();
    },
  });

  const sensors = useSensors(
    // A small drag threshold so clicking the title/checkbox/handle
    // isn't misread as the start of a drag.
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
  );

  function onDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const pending = query.data?.pending ?? [];
    const ids = pending.map((i) => i.id);
    const from = ids.indexOf(active.id as number);
    const to = ids.indexOf(over.id as number);
    if (from === -1 || to === -1) return;
    reorder.mutate(arrayMove(ids, from, to));
  }

  const data = query.data;
  const pending = data?.pending ?? [];
  const completed = data?.completed ?? [];

  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
      <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
        Todo
      </h3>

      {query.isError && (
        <p role="alert" className="mt-2 text-xs text-red-400">
          Failed to load todos.
        </p>
      )}

      <div className="mt-3 space-y-1.5">
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragEnd={onDragEnd}
        >
          <SortableContext
            items={pending.map((i) => i.id)}
            strategy={verticalListSortingStrategy}
          >
            <ul className="space-y-1.5">
              {pending.map((item) => (
                <SortableTodoItem
                  key={item.id}
                  item={item}
                  autoEditTitle={item.id === focusId}
                  onPatch={(body) => patch.mutate({ id: item.id, body })}
                  onDelete={() => remove.mutate(item.id)}
                />
              ))}
            </ul>
          </SortableContext>
        </DndContext>

        {pending.length === 0 && !query.isLoading && (
          <p className="text-xs text-zinc-600">Nothing pending.</p>
        )}
      </div>

      <button
        type="button"
        onClick={() => create.mutate()}
        disabled={create.isPending}
        className="mt-2 text-xs text-zinc-500 hover:text-zinc-300"
      >
        + Add todo
      </button>

      {completed.length > 0 && (
        <div className="mt-4 border-t border-zinc-800/70 pt-3">
          <h4 className="text-[11px] font-medium uppercase tracking-wide text-zinc-600">
            Completed
          </h4>
          <ul className="mt-2 space-y-1.5">
            {completed.map((item) => (
              <li key={item.id}>
                <TodoCard
                  item={item}
                  onPatch={(body) => patch.mutate({ id: item.id, body })}
                  onDelete={() => remove.mutate(item.id)}
                />
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
