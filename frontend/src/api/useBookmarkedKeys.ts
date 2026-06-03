import { useQuery } from "@tanstack/react-query";

import { listBookmarks } from "./bookmarks";

/**
 * Hook for parent components: returns the set of `${pr_repo}#${pr_number}`
 * keys for currently-bookmarked PRs. Used to hide the "Bookmark this"
 * button on authored/worktree rows that are already in the bookmark
 * surface.
 *
 * Uses the shared `["bookmarks"]` query key — multiple callers dedup
 * through react-query's cache, so this never causes extra network
 * fetches beyond the one `BookmarkList` already runs.
 */
export function useBookmarkedKeys(): Set<string> {
  const q = useQuery({
    queryKey: ["bookmarks"],
    queryFn: listBookmarks,
    refetchInterval: 60_000,
  });
  const list = q.data?.bookmarks ?? [];
  return new Set(list.map((b) => `${b.pr_repo}#${b.pr_number}`));
}
