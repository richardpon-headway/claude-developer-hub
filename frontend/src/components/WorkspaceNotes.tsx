import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { updateNotes } from "../api/worktrees";

interface Props {
  repo: string;
  name: string;
  notes: string | null;
  // "compact" = hub row variant (smaller default height).
  // "full"    = detail page variant (taller default).
  // Both auto-grow with content; the variant only sets the floor.
  variant: "compact" | "full";
}

type SaveStatus = "idle" | "saving" | "saved" | "error";

// Debounce window between the last keystroke and the auto-save PUT.
// 1s is short enough to feel like every edit lands quickly, long
// enough that a sustained typing burst doesn't fire N requests.
const SAVE_DEBOUNCE_MS = 1000;

// Soft cap mirrored from the backend's max_length=10_000. We don't
// hard-block the textarea — the user might paste briefly and trim —
// but we surface the count once they're close enough to care.
const SOFT_LIMIT = 10_000;
const SOFT_LIMIT_WARN_AT = 9_500;

// Minimum rendered textarea height per variant. Content beyond this
// expands the box via the auto-grow effect below.
const MIN_HEIGHT_PX: Record<"compact" | "full", number> = {
  compact: 80,
  full: 140,
};


export function WorkspaceNotes({ repo, name, notes, variant }: Props) {
  const queryClient = useQueryClient();

  const [draft, setDraft] = useState(notes ?? "");
  const [committed, setCommitted] = useState(notes ?? "");
  const [status, setStatus] = useState<SaveStatus>("idle");
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // If the parent component receives a fresher `notes` prop (e.g., a
  // poll refetch picked up a value edited elsewhere) AND the user
  // hasn't started typing locally, adopt the new value. Treat a
  // local edit (draft !== committed) as authoritative — don't
  // clobber the user's in-flight keystrokes.
  const previousProp = useRef(notes);
  useEffect(() => {
    if (previousProp.current === notes) return;
    previousProp.current = notes;
    if (draft === committed) {
      const next = notes ?? "";
      setDraft(next);
      setCommitted(next);
    }
  }, [notes, draft, committed]);

  const saveMutation = useMutation({
    mutationFn: (text: string) => updateNotes(repo, name, text),
    onSuccess: (_, text) => {
      setCommitted(text);
      setStatus("saved");
      setErrorDetail(null);
      queryClient.invalidateQueries({ queryKey: ["worktrees"] });
      queryClient.invalidateQueries({ queryKey: ["worktree", repo, name] });
    },
    onError: (err) => {
      setStatus("error");
      setErrorDetail(err instanceof ApiError ? err.detail : String(err));
    },
  });

  // Debounce: re-arm on every keystroke; fire when settled.
  useEffect(() => {
    if (draft === committed) return; // nothing to save
    if (draft.length > SOFT_LIMIT) return; // would 422; let the count warn the user
    setStatus("saving");
    const handle = window.setTimeout(() => {
      saveMutation.mutate(draft);
    }, SAVE_DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
    // We intentionally don't depend on saveMutation — its identity is
    // stable enough and including it would re-arm the timer on every
    // status flip.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, committed]);

  // Auto-grow the textarea to fit its content. Runs synchronously
  // (useLayoutEffect) so the user never sees a frame at the wrong
  // size.
  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.max(el.scrollHeight, MIN_HEIGHT_PX[variant])}px`;
  }, [draft, variant]);

  return (
    <div className="space-y-1">
      <textarea
        ref={textareaRef}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        placeholder="+ Add note"
        className={
          "block w-full resize-none overflow-hidden rounded border " +
          "border-zinc-800 bg-zinc-950/40 px-3 py-2 text-xs text-zinc-200 " +
          "placeholder:text-zinc-600 hover:border-zinc-700 " +
          "focus:border-indigo-700 focus:bg-zinc-950/60 focus:outline-none"
        }
        style={{ minHeight: MIN_HEIGHT_PX[variant] }}
      />
      <StatusRow draft={draft} status={status} error={errorDetail} />
    </div>
  );
}

interface StatusRowProps {
  draft: string;
  status: SaveStatus;
  error: string | null;
}

function StatusRow({ draft, status, error }: StatusRowProps) {
  return (
    <div className="flex items-center justify-end gap-2 text-[10px] text-zinc-600">
      {draft.length >= SOFT_LIMIT_WARN_AT && (
        <span
          className={draft.length > SOFT_LIMIT ? "text-red-400" : "text-amber-400"}
        >
          {draft.length} / {SOFT_LIMIT}
        </span>
      )}
      <StatusLabel status={status} error={error} />
    </div>
  );
}

interface StatusLabelProps {
  status: SaveStatus;
  error: string | null;
}

function StatusLabel({ status, error }: StatusLabelProps) {
  if (status === "error") {
    return (
      <span className="text-red-400" title={error ?? undefined}>
        save failed
      </span>
    );
  }
  if (status === "saving") return <span>saving…</span>;
  if (status === "saved") return <span>saved</span>;
  return null;
}
