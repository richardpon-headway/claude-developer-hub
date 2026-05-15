import { apiGet, apiPost } from "./client";
import type { InboxResponse } from "./types";

export const getInbox = () => apiGet<InboxResponse>("/api/inbox");

export const refreshInbox = () =>
  apiPost<InboxResponse>("/api/inbox/refresh", {});

export interface PullDownResponse {
  repo: string;
  name: string;
}

export const pullDownPr = (prRepo: string, prNumber: number) =>
  apiPost<PullDownResponse>(
    // pr_repo contains a slash; the FastAPI route uses {pr_repo:path}
    // so we encode the segments individually to keep the slash literal.
    `/api/inbox/${prRepo}/${prNumber}/pull-down`,
    {},
  );

export interface ConfigureAndPullDownResponse {
  session_id: string;
}

export const configureAndPullDown = (prRepo: string, prNumber: number) =>
  apiPost<ConfigureAndPullDownResponse>(
    `/api/inbox/${prRepo}/${prNumber}/configure-and-pull-down`,
    {},
  );
