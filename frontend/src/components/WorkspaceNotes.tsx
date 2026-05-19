import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { ApiError } from "../api/client";
import { updateNotes } from "../api/worktrees";

interface Props {
  repo: string;
  name: string;
  notes: string | null;
  // "compact" = hub row variant (single-line collapsed textarea
  // until focused, smaller markdown preview).
  // "full"    = detail page variant (larger textarea, always-on
  // preview when content present).
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


export function WorkspaceNotes({ repo, name, notes, variant }: Props) {
  const queryClient = useQueryClient();

  // `committed` tracks the last value confirmed-saved to the server.
  // The mutation flips it on success; the debounce effect compares
  // `draft` against it to decide whether a save is needed.
  const [draft, setDraft] = useState(notes ?? "");
  const [committed, setCommitted] = useState(notes ?? "");
  const [status, setStatus] = useState<SaveStatus>("idle");
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [focused, setFocused] = useState(false);

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
      // Invalidate so other surfaces (hub row ↔ detail page) refetch
      // and stay in sync. Light cost since the response is small.
      queryClient.invalidateQueries({ queryKey: ["worktrees"] });
      queryClient.invalidateQueries({ queryKey: ["worktree", repo, name] });
    },
    onError: (err) => {
      setStatus("error");
      setErrorDetail(err instanceof ApiError ? err.detail : String(err));
    },
  });

  // Debounce: re-arm the timer on every keystroke; fire when settled.
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

  const isCompact = variant === "compact";
  const showPreview = !focused && committed.length > 0;

  return (
    <div className="space-y-1">
      <textarea
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        placeholder={isCompact ? "+ Add note" : "Notes (markdown)…"}
        rows={isCompact ? 1 : 4}
        className={
          "w-full resize-y rounded border border-zinc-800 bg-zinc-950/60 px-2 py-1 " +
          "font-mono text-xs text-zinc-200 placeholder:text-zinc-600 " +
          "focus:border-indigo-700 focus:outline-none " +
          (isCompact ? "min-h-[1.75rem]" : "min-h-[6rem]")
        }
      />
      {showPreview && (
        <div
          className={
            "prose prose-invert max-w-none rounded border border-zinc-900 " +
            "bg-zinc-950/40 px-2 py-1 text-xs text-zinc-300 " +
            (isCompact ? "" : "prose-sm")
          }
        >
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            // Open every link in a new tab — the hub is the user's
            // workflow surface; nothing should pull focus away from it.
            components={{
              a: ({ node, ...rest }) => (
                <a {...rest} target="_blank" rel="noopener noreferrer" />
              ),
            }}
          >
            {committed}
          </ReactMarkdown>
        </div>
      )}
      <div className="flex items-center justify-end gap-2 text-[10px] text-zinc-600">
        {draft.length >= SOFT_LIMIT_WARN_AT && (
          <span
            className={
              draft.length > SOFT_LIMIT ? "text-red-400" : "text-amber-400"
            }
          >
            {draft.length} / {SOFT_LIMIT}
          </span>
        )}
        <StatusLabel status={status} error={errorDetail} />
      </div>
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
