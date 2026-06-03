import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { addBookmark } from "../api/bookmarks";

interface Props {
  // Query keys to invalidate after a successful add. Defaults to the
  // bookmark list; the unified hub passes ["workspaces"].
  invalidateKeys?: string[][];
}

// Paste-a-GitHub-PR-URL intake. Extracted from BookmarkList so it can
// live at the top of the unified hub independent of any one list.
export function BookmarkIntake({ invalidateKeys = [["bookmarks"]] }: Props) {
  const [url, setUrl] = useState("");
  const queryClient = useQueryClient();
  const addMutation = useMutation({
    mutationFn: (u: string) => addBookmark(u),
    onSuccess: () => {
      setUrl("");
      for (const key of invalidateKeys) {
        queryClient.invalidateQueries({ queryKey: key });
      }
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
