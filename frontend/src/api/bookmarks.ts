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
