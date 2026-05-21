import { apiGet, apiPost } from "./client";
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
