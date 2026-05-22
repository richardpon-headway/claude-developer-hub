import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { addBookmark, listBookmarks } from "../api/bookmarks";
import type { BookmarkPr, JiraConfig } from "../api/types";
import { PrCard, useBookmarkedKeys } from "./PrCard";

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
  // Bookmark rows are themselves bookmarked; pass through so PrCard
  // doesn't show the (redundant) "Bookmark this" button on them.
  const bookmarked = useBookmarkedKeys();

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
            <PrCard
              key={`${b.pr_repo}#${b.pr_number}`}
              data={{ kind: "bookmark", row: b }}
              jira={jira}
              bookmarked={bookmarked}
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
