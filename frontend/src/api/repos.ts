import { apiGet, apiPost } from "./client";
import type {
  OnboardCompleteRequest,
  OnboardCompleteResponse,
  OnboardResponse,
  OnboardStatus,
  RepoConfig,
} from "./types";

export const listRepos = () => apiGet<RepoConfig[]>("/api/repos");

export const onboardRepo = (path: string) =>
  apiPost<OnboardResponse>("/api/repos/onboard", { path });

export const getOnboardStatus = (sessionId: string) =>
  apiGet<OnboardStatus>(`/api/repos/onboard/${encodeURIComponent(sessionId)}`);

export const completeOnboard = (req: OnboardCompleteRequest) =>
  apiPost<OnboardCompleteResponse>("/api/repos/onboard/complete", req);
