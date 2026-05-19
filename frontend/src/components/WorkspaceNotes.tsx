import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { ApiError } from "../api/client";
import { updateNotes } from "../api/worktrees";

interface Props {
  repo: string;
  name: string;
  notes: string | null;
  // "compact" = hub row variant (smaller default height).
  // "full"    = detail page variant (taller default).
  // Both auto-grow with content; the variant just sets the floor.
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

// Minimum rendered height for the view/edit container. Variants set
// a floor so an empty / short note still has visual weight; content
// past the floor expands the container.
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
  // Single mode toggle for the whole component. ``view`` renders the
  // rendered markdown (or a "+ Add note" placeholder when empty);
  // ``edit`` renders the textarea. Clicking the view container
  // switches to edit + focuses the textarea; blurring the textarea
  // switches back to view. Auto-save fires on debounce regardless.
  const [mode, setMode] = useState<"view" | "edit">("view");
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

  // Auto-resize the textarea to fit its content. Runs synchronously
  // (useLayoutEffect) so the user never sees a frame at the wrong
  // size, especially right after switching from view → edit.
  useLayoutEffect(() => {
    if (mode !== "edit") return;
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    const minHeight = MIN_HEIGHT_PX[variant];
    el.style.height = `${Math.max(el.scrollHeight, minHeight)}px`;
  }, [draft, mode, variant]);

  const enterEdit = () => {
    setMode("edit");
    // Defer focus so the textarea exists in the DOM by the time we
    // call focus(). useLayoutEffect could also do this but the
    // post-render cycle is simpler with a setTimeout(0).
    window.setTimeout(() => {
      const el = textareaRef.current;
      if (el) {
        el.focus();
        // Place cursor at end so the user can keep typing without
        // having to re-position.
        el.setSelectionRange(el.value.length, el.value.length);
      }
    }, 0);
  };

  const minHeightPx = MIN_HEIGHT_PX[variant];

  if (mode === "edit") {
    return (
      <div className="space-y-1">
        <textarea
          ref={textareaRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => setMode("view")}
          onKeyDown={(e) => {
            // Escape exits edit mode without losing focus. Doesn't
            // discard the draft — the debounce already saved (or is
            // about to save) the current value.
            if (e.key === "Escape") {
              e.preventDefault();
              setMode("view");
            }
          }}
          placeholder="Notes (markdown — links, **bold**, `code`, lists)…"
          className={
            "block w-full resize-none overflow-hidden rounded border " +
            "border-indigo-700 bg-zinc-950/60 px-3 py-2 font-mono " +
            "text-xs text-zinc-200 placeholder:text-zinc-600 " +
            "focus:border-indigo-500 focus:outline-none"
          }
          style={{ minHeight: minHeightPx }}
        />
        <StatusRow draft={draft} status={status} error={errorDetail} />
      </div>
    );
  }

  // mode === "view"
  const hasContent = draft.length > 0;
  return (
    <div className="space-y-1">
      <div
        role="button"
        tabIndex={0}
        onClick={enterEdit}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            enterEdit();
          }
        }}
        className={
          "block w-full cursor-text rounded border border-zinc-800 " +
          "bg-zinc-950/40 px-3 py-2 transition-colors " +
          "hover:border-zinc-700 hover:bg-zinc-900/60 " +
          "focus-visible:border-indigo-700 focus-visible:outline-none"
        }
        style={{ minHeight: minHeightPx }}
        aria-label={
          hasContent ? "Notes — click to edit" : "Add note"
        }
      >
        {hasContent ? (
          <div className="prose prose-invert prose-sm max-w-none text-xs text-zinc-200">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                a: ({ ...rest }) => (
                  <a
                    {...rest}
                    target="_blank"
                    rel="noopener noreferrer"
                    // Stop propagation so clicking a link inside the
                    // preview opens the link instead of toggling
                    // into edit mode.
                    onClick={(e) => e.stopPropagation()}
                  />
                ),
              }}
            >
              {draft}
            </ReactMarkdown>
          </div>
        ) : (
          <span className="text-xs text-zinc-600">+ Add note</span>
        )}
      </div>
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
