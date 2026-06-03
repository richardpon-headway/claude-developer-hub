import { apiDelete, apiGet, apiPost, apiPut } from "./client";
import type { BookmarkListResponse, BookmarkPr } from "./types";

export const listBookmarks = () =>
  apiGet<BookmarkListResponse>("/api/bookmarks");

export const addBookmark = (url: string) =>
  apiPost<BookmarkPr>("/api/bookmarks", { url });

export interface DeleteBookmarkResponse {
  deleted: true;
}

export const deleteBookmark = (prRepo: string, prNumber: number) =>
  apiDelete<DeleteBookmarkResponse>(`/api/bookmarks/${prRepo}/${prNumber}`);

export interface PullDownResponse {
  repo: string;
  name: string;
}

export const pullDownBookmark = (prRepo: string, prNumber: number) =>
  apiPost<PullDownResponse>(
    `/api/bookmarks/${prRepo}/${prNumber}/pull-down`,
    {},
  );

// Helper for the cross-surface "Bookmark this" button on authored /
// worktree rows. The existing `POST /api/bookmarks {url}`
// already handles parsing the URL + fetching gh pr view; we just
// construct the URL from the PR identifiers we already have.
export const bookmarkPr = (prRepo: string, prNumber: number) =>
  addBookmark(`https://github.com/${prRepo}/pull/${prNumber}`);

export interface UpdateNotesResponse {
  notes: string;
}

export const updateBookmarkNotes = (
  prRepo: string,
  prNumber: number,
  notes: string,
) =>
  apiPut<UpdateNotesResponse>(`/api/bookmarks/${prRepo}/${prNumber}/notes`, {
    notes,
  });
