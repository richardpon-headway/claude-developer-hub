import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";

import { EditableText } from "./EditableText";
import type { TodoItem, UpdateTodoBody } from "./api";

interface CardProps {
  item: TodoItem;
  onPatch: (body: UpdateTodoBody) => void;
  // Delete affordance. Only wired for completed items — pending items
  // aren't directly deletable (complete it first), so a stray click
  // can't lose in-progress work. Omitted → no ✕ rendered.
  onDelete?: () => void;
  // Start the title in edit mode (freshly-created item).
  autoEditTitle?: boolean;
  // Drag handle slot — present for pending items, omitted for completed.
  handle?: React.ReactNode;
}

/** Presentational todo card: checkbox, editable multi-line text, delete. */
export function TodoCard({
  item,
  onPatch,
  onDelete,
  autoEditTitle = false,
  handle,
}: CardProps) {
  return (
    <div className="group flex gap-2 rounded-md border border-zinc-800 bg-zinc-950/40 p-2">
      {handle}
      <input
        type="checkbox"
        checked={item.done}
        onChange={(e) => onPatch({ done: e.target.checked })}
        aria-label={item.done ? "mark as not done" : "mark as done"}
        className="mt-1 h-4 w-4 shrink-0 cursor-pointer accent-indigo-500"
      />
      <div className="min-w-0 flex-1">
        <EditableText
          value={item.title}
          placeholder="Add a task… (Shift+Enter for a new line)"
          autoEdit={autoEditTitle}
          onSave={(text) => onPatch({ title: text })}
          className={
            "text-sm " + (item.done ? "text-zinc-500 line-through" : "")
          }
        />
      </div>

      {onDelete && (
        <button
          type="button"
          onClick={onDelete}
          aria-label="delete todo"
          className="shrink-0 self-start text-zinc-700 opacity-0 transition group-hover:opacity-100 hover:text-red-400"
        >
          ✕
        </button>
      )}
    </div>
  );
}

interface SortableProps {
  item: TodoItem;
  onPatch: (body: UpdateTodoBody) => void;
  autoEditTitle?: boolean;
}

/** A pending card wired for drag-to-reorder via its handle. Pending
 * items have no delete affordance — they're removed by completing then
 * deleting from the Completed section. */
export function SortableTodoItem({
  item,
  onPatch,
  autoEditTitle,
}: SortableProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: item.id });

  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.6 : 1,
  };

  return (
    <li ref={setNodeRef} style={style}>
      <TodoCard
        item={item}
        onPatch={onPatch}
        autoEditTitle={autoEditTitle}
        handle={
          <button
            type="button"
            aria-label="drag to reorder"
            className="shrink-0 cursor-grab self-start text-zinc-700 hover:text-zinc-500 active:cursor-grabbing"
            {...attributes}
            {...listeners}
          >
            ⠿
          </button>
        }
      />
    </li>
  );
}
