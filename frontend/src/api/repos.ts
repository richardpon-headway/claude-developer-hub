import { apiGet, apiPost } from "./client";
import type {
  OnboardCompleteRequest,
  OnboardCompleteResponse,
  OnboardResponse,
  OnboardStatus,
  RepoCandidate,
  RepoConfig,
} from "./types";

export const listRepos = () => apiGet<RepoConfig[]>("/api/repos");

export const listRepoCandidates = () =>
  apiGet<RepoCandidate[]>("/api/repos/candidates");

export const onboardRepo = (path: string) =>
  apiPost<OnboardResponse>("/api/repos/onboard", { path });

export const getOnboardStatus = (sessionId: string) =>
  apiGet<OnboardStatus>(`/api/repos/onboard/${encodeURIComponent(sessionId)}`);

export const completeOnboard = (req: OnboardCompleteRequest) =>
  apiPost<OnboardCompleteResponse>("/api/repos/onboard/complete", req);
