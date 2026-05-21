import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import {
  addBookmark,
  deleteBookmark,
  listBookmarks,
} from "../api/bookmarks";
import type { BookmarkPr, BookmarkState, JiraConfig } from "../api/types";
import { BookmarkNotes } from "./BookmarkNotes";
import { Tooltip } from "./Tooltip";

const STATE_STYLE: Record<BookmarkState, { label: string; cls: string }> = {
  open: {
    label: "open",
    cls: "border-emerald-800 bg-emerald-900/40 text-emerald-300",
  },
  merged: {
    label: "merged",
    cls: "border-purple-800 bg-purple-900/40 text-purple-300",
  },
  closed: {
    label: "closed",
    cls: "border-zinc-700 bg-zinc-800 text-zinc-400",
  },
};

interface Props {
  jira: JiraConfig | null;
  // Render-only for testing.
  bookmarksOverride?: BookmarkPr[];
}

export function BookmarkList({ jira, bookmarksOverride }: Props) {
  const bookmarksQuery = useQuery({
    queryKey: ["bookmarks"],
    queryFn: listBookmarks,
    refetchInterval: 60_000,
    enabled: bookmarksOverride === undefined,
  });

  const bookmarks = bookmarksOverride ?? bookmarksQuery.data?.bookmarks;

  if (bookmarks === undefined) {
    return null;
  }

  return (
    <section>
      <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
        Bookmarks
        <span className="ml-2 text-zinc-600">· {bookmarks.length}</span>
      </h2>
      <AddBookmarkForm />
      {bookmarks.length === 0 ? (
        <div className="mt-3 rounded-lg border border-dashed border-zinc-700 p-6 text-center">
          <p className="text-sm text-zinc-400">No bookmarks yet.</p>
          <p className="mt-1 text-xs text-zinc-500">
            Paste a GitHub PR URL above to track a PR that isn't already
            in your inbox.
          </p>
        </div>
      ) : (
        <ul className="mt-3 space-y-2">
          {bookmarks.map((b) => (
            <BookmarkRow
              key={`${b.pr_repo}#${b.pr_number}`}
              bookmark={b}
              jira={jira}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function AddBookmarkForm() {
  const [url, setUrl] = useState("");
  const queryClient = useQueryClient();
  const addMutation = useMutation({
    mutationFn: (u: string) => addBookmark(u),
    onSuccess: () => {
      setUrl("");
      queryClient.invalidateQueries({ queryKey: ["bookmarks"] });
    },
  });

  const errorDetail = addMutation.error
    ? addMutation.error instanceof ApiError
      ? addMutation.error.detail
      : String(addMutation.error)
    : null;

  return (
    <form
      className="mt-3 flex gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        if (!url.trim()) return;
        addMutation.mutate(url.trim());
      }}
    >
      <input
        type="text"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        placeholder="Paste a GitHub PR URL"
        className={
          "min-w-0 flex-1 rounded border border-zinc-800 bg-zinc-950/40 " +
          "px-3 py-1.5 text-xs text-zinc-200 placeholder:text-zinc-600 " +
          "hover:border-zinc-700 focus:border-indigo-700 focus:bg-zinc-950/60 " +
          "focus:outline-none"
        }
      />
      <button
        type="submit"
        disabled={addMutation.isPending || !url.trim()}
        className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-xs text-zinc-200 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {addMutation.isPending ? "Adding…" : "+ Bookmark PR"}
      </button>
      {errorDetail && (
        <p
          role="alert"
          className="basis-full text-right text-[10px] leading-tight text-red-400"
        >
          {errorDetail}
        </p>
      )}
    </form>
  );
}

interface RowProps {
  bookmark: BookmarkPr;
  jira: JiraConfig | null;
}

function BookmarkRow({ bookmark, jira }: RowProps) {
  const state = STATE_STYLE[bookmark.state];
  return (
    <li className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-baseline gap-2">
          <a
            href={bookmark.url}
            target="_blank"
            rel="noopener noreferrer"
            className="min-w-0 truncate font-medium text-zinc-100 hover:text-indigo-300"
            title={bookmark.title}
          >
            {bookmark.title}
          </a>
          <span className="shrink-0 font-mono text-xs text-zinc-500">
            #{bookmark.pr_number}
          </span>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-x-2 gap-y-1">
          <span
            className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] ${state.cls}`}
          >
            {state.label}
          </span>
          <span className="shrink-0 rounded border border-indigo-800 bg-indigo-900/30 px-1.5 py-0.5 text-[10px] text-indigo-300">
            bookmark
          </span>
        </div>
      </div>
      <div className="mt-2 flex items-end justify-between gap-4">
        <div className="min-w-0 flex-1 space-y-0.5 text-xs text-zinc-500">
          <div>
            @{bookmark.author_login}{" "}
            <span className="text-zinc-600">· {bookmark.pr_repo}</span>
          </div>
          {bookmark.ticket && (
            <div>
              ticket: <TicketValue ticket={bookmark.ticket} jira={jira} />
            </div>
          )}
        </div>
        <div className="flex shrink-0 items-start gap-2">
          <UnbookmarkButton bookmark={bookmark} />
        </div>
      </div>
      <div className="mt-3">
        <BookmarkNotes
          prRepo={bookmark.pr_repo}
          prNumber={bookmark.pr_number}
          notes={bookmark.notes}
        />
      </div>
    </li>
  );
}

interface TicketValueProps {
  ticket: string;
  jira: JiraConfig | null;
}

function TicketValue({ ticket, jira }: TicketValueProps) {
  if (!jira?.base_url) return <>{ticket}</>;
  const base = jira.base_url.replace(/\/+$/, "");
  return (
    <a
      href={`${base}/browse/${ticket}`}
      target="_blank"
      rel="noopener noreferrer"
      className="text-zinc-400 underline decoration-zinc-700 underline-offset-2 hover:text-indigo-300 hover:decoration-indigo-400"
    >
      {ticket}
    </a>
  );
}

interface UnbookmarkButtonProps {
  bookmark: BookmarkPr;
}

function UnbookmarkButton({ bookmark }: UnbookmarkButtonProps) {
  const queryClient = useQueryClient();
  const deleteMutation = useMutation({
    mutationFn: () => deleteBookmark(bookmark.pr_repo, bookmark.pr_number),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["bookmarks"] });
      // An unbookmarked PR may re-enter the inbox on the next poll if
      // it's still review-requested. Invalidate inbox too so the next
      // refetch picks it up without waiting.
      queryClient.invalidateQueries({ queryKey: ["inbox"] });
    },
  });

  const tooltip = deleteMutation.error
    ? deleteMutation.error instanceof ApiError
      ? deleteMutation.error.detail
      : String(deleteMutation.error)
    : "Remove this bookmark. The PR will reappear in the inbox on the next poll if you're still review-requested.";

  return (
    <Tooltip text={tooltip}>
      <button
        type="button"
        onClick={() => deleteMutation.mutate()}
        disabled={deleteMutation.isPending}
        className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {deleteMutation.isPending ? "Removing…" : "Unbookmark"}
      </button>
    </Tooltip>
  );
}
