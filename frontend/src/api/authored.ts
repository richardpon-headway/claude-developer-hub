import { apiGet, apiPost, apiPut } from "./client";
import type { AuthoredPrListResponse } from "./types";

export const listAuthoredPrs = () =>
  apiGet<AuthoredPrListResponse>("/api/authored-prs");

export interface PullDownResponse {
  repo: string;
  name: string;
}

export const pullDownAuthoredPr = (prRepo: string, prNumber: number) =>
  apiPost<PullDownResponse>(
    `/api/authored-prs/${prRepo}/${prNumber}/pull-down`,
    {},
  );

export interface UpdateNotesResponse {
  notes: string;
}

export const updateAuthoredPrNotes = (
  prRepo: string,
  prNumber: number,
  notes: string,
) =>
  apiPut<UpdateNotesResponse>(
    `/api/authored-prs/${prRepo}/${prNumber}/notes`,
    { notes },
  );
