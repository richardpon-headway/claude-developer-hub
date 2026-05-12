import { apiGet } from "./client";
import type { TokenUsageResponse, Worktree, WorktreeDetail } from "./types";

export const listWorktrees = () => apiGet<Worktree[]>("/api/worktrees");

export const getWorktree = (repo: string, name: string) =>
  apiGet<WorktreeDetail>(
    `/api/worktree/${encodeURIComponent(repo)}/${encodeURIComponent(name)}`,
  );

export const getTokenUsage = () => apiGet<TokenUsageResponse>("/api/token-usage");
