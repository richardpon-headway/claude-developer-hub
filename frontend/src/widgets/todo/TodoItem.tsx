import { useState } from "react";
import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";

import { EditableText } from "./EditableText";
import type { TodoItem, UpdateTodoBody } from "./api";

interface CardProps {
  item: TodoItem;
  onPatch: (body: UpdateTodoBody) => void;
  onDelete: () => void;
  // Start the title in edit mode (freshly-created item).
  autoEditTitle?: boolean;
  // Drag handle slot — present for pending items, omitted for completed.
  handle?: React.ReactNode;
}

/** Presentational todo card: checkbox, editable title, bullets, delete. */
export function TodoCard({
  item,
  onPatch,
  onDelete,
  autoEditTitle = false,
  handle,
}: CardProps) {
  // Whether the most recently added bullet should open in edit mode.
  const [focusNewBullet, setFocusNewBullet] = useState(false);

  const replaceBullet = (index: number, text: string) =>
    item.bullets.map((b, i) => (i === index ? text : b));
  const removeBullet = (index: number) =>
    item.bullets.filter((_, i) => i !== index);

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
          placeholder="Add a title…"
          autoEdit={autoEditTitle}
          onSave={(text) => onPatch({ title: text })}
          className={
            "text-sm " + (item.done ? "text-zinc-500 line-through" : "")
          }
        />

        {item.bullets.length > 0 && (
          <ul className="mt-1 space-y-0.5">
            {item.bullets.map((bullet, i) => (
              <li
                key={i}
                className="flex items-start gap-1.5 text-xs text-zinc-400"
              >
                <span className="mt-1.5 select-none text-zinc-600">•</span>
                <div className="min-w-0 flex-1">
                  <EditableText
                    value={bullet}
                    placeholder="bullet…"
                    autoEdit={focusNewBullet && i === item.bullets.length - 1}
                    onSave={(text) =>
                      onPatch({ bullets: replaceBullet(i, text) })
                    }
                    onBlur={(text) => {
                      setFocusNewBullet(false);
                      if (text.trim() === "") {
                        onPatch({ bullets: removeBullet(i) });
                      }
                    }}
                    className={
                      "text-xs " + (item.done ? "line-through" : "")
                    }
                  />
                </div>
              </li>
            ))}
          </ul>
        )}

        <button
          type="button"
          onClick={() => {
            setFocusNewBullet(true);
            onPatch({ bullets: [...item.bullets, ""] });
          }}
          className="mt-1 text-[11px] text-zinc-600 hover:text-zinc-400"
        >
          + bullet
        </button>
      </div>

      <button
        type="button"
        onClick={onDelete}
        aria-label="delete todo"
        className="shrink-0 self-start text-zinc-700 opacity-0 transition group-hover:opacity-100 hover:text-red-400"
      >
        ✕
      </button>
    </div>
  );
}

interface SortableProps {
  item: TodoItem;
  onPatch: (body: UpdateTodoBody) => void;
  onDelete: () => void;
  autoEditTitle?: boolean;
}

/** A pending card wired for drag-to-reorder via its handle. */
export function SortableTodoItem({
  item,
  onPatch,
  onDelete,
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
        onDelete={onDelete}
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
