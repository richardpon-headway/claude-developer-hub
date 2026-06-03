import { apiPost, apiPut } from "./client";

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
