import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { linkify } from "./linkify";

// Debounce between the last keystroke and the autosave — mirrors the
// workspace NotesEditor so the whole hub feels consistent.
const SAVE_DEBOUNCE_MS = 1000;

interface Props {
  value: string;
  placeholder: string;
  // Persist a changed value. Fired on debounce while editing and again
  // on commit (blur / Enter) if the value differs from the last save.
  onSave: (text: string) => void;
  // Fired when the field loses focus, with the current draft. Lets the
  // parent apply blur-time semantics (e.g. drop an empty bullet).
  onBlur?: (text: string) => void;
  // Start in edit mode with focus — used for freshly-added items/bullets.
  autoEdit?: boolean;
  // Tailwind classes for the rendered text / textarea.
  className?: string;
}

/**
 * A single field that renders as linkified, read-only text until
 * clicked, then becomes an auto-growing textarea. Edits autosave on a
 * debounce (no save button) and again on blur/Enter. Escape reverts.
 */
export function EditableText({
  value,
  placeholder,
  onSave,
  onBlur,
  autoEdit = false,
  className = "",
}: Props) {
  const [editing, setEditing] = useState(autoEdit);
  const [draft, setDraft] = useState(value);
  const [committed, setCommitted] = useState(value);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Adopt a fresher external value only when the user isn't mid-edit,
  // so a background refetch can't clobber in-flight keystrokes.
  const previousValue = useRef(value);
  useEffect(() => {
    if (previousValue.current === value) return;
    previousValue.current = value;
    if (!editing && draft === committed) {
      setDraft(value);
      setCommitted(value);
    }
  }, [value, editing, draft, committed]);

  // Debounced autosave while editing.
  useEffect(() => {
    if (!editing) return;
    if (draft === committed) return;
    const handle = window.setTimeout(() => {
      setCommitted(draft);
      onSave(draft);
    }, SAVE_DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
    // onSave identity is stable enough; including it would re-arm the
    // timer needlessly.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, committed, editing]);

  // Auto-grow + focus-to-end when entering edit mode / typing.
  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [draft, editing]);

  useEffect(() => {
    if (!editing) return;
    const el = textareaRef.current;
    if (!el) return;
    el.focus();
    const end = el.value.length;
    el.setSelectionRange(end, end);
  }, [editing]);

  function commit() {
    if (draft !== committed) {
      setCommitted(draft);
      onSave(draft);
    }
    setEditing(false);
    onBlur?.(draft);
  }

  if (!editing) {
    const isEmpty = value.trim() === "";
    return (
      <button
        type="button"
        onClick={() => {
          setDraft(value);
          setCommitted(value);
          setEditing(true);
        }}
        className={
          "block w-full cursor-text whitespace-pre-wrap break-words text-left " +
          (isEmpty ? "text-zinc-600 " : "text-zinc-200 ") +
          className
        }
      >
        {isEmpty ? placeholder : linkify(value)}
      </button>
    );
  }

  return (
    <textarea
      ref={textareaRef}
      value={draft}
      rows={1}
      placeholder={placeholder}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          // Enter commits; Shift+Enter falls through to insert a newline
          // so an item can hold multiple lines.
          e.preventDefault();
          e.currentTarget.blur();
        } else if (e.key === "Escape") {
          setDraft(committed);
          setEditing(false);
        }
      }}
      className={
        "block w-full resize-none overflow-hidden rounded border " +
        "border-indigo-700 bg-zinc-950/60 px-2 py-1 text-zinc-200 " +
        "placeholder:text-zinc-600 focus:outline-none " +
        className
      }
    />
  );
}
