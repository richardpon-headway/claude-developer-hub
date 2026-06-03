import { useQuery } from "@tanstack/react-query";

import { listBookmarks } from "../api/bookmarks";
import type { BookmarkPr, JiraConfig } from "../api/types";
import { useBookmarkedKeys } from "../api/useBookmarkedKeys";
import { BookmarkIntake } from "./BookmarkIntake";
import { PrCard } from "./PrCard";

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
      <BookmarkIntake />
      {bookmarks.length === 0 ? (
        <div className="mt-3 rounded-lg border border-dashed border-zinc-700 p-6 text-center">
          <p className="text-sm text-zinc-400">No bookmarks yet.</p>
          <p className="mt-1 text-xs text-zinc-500">
            Paste a GitHub PR URL above to track a PR. Its repo must be
            configured first via "Add a repo".
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
