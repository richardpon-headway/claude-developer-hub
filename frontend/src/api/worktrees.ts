import { apiGet, apiPost } from "./client";
import type {
  SendResponse,
  SpawnItermResponse,
  TokenUsageResponse,
  Worktree,
  WorktreeDetail,
} from "./types";

export const listWorktrees = () => apiGet<Worktree[]>("/api/worktrees");

export const getWorktree = (repo: string, name: string) =>
  apiGet<WorktreeDetail>(
    `/api/worktree/${encodeURIComponent(repo)}/${encodeURIComponent(name)}`,
  );

export const getTokenUsage = () => apiGet<TokenUsageResponse>("/api/token-usage");

const workspacePath = (repo: string, name: string) =>
  `/api/worktree/${encodeURIComponent(repo)}/${encodeURIComponent(name)}`;

export const spawnIterm = (repo: string, name: string) =>
  apiPost<SpawnItermResponse>(`${workspacePath(repo, name)}/spawn-iterm`, {});

export const sendText = (
  repo: string,
  name: string,
  text: string,
  pressEnter = true,
) =>
  apiPost<SendResponse>(`${workspacePath(repo, name)}/send-text`, {
    text,
    press_enter: pressEnter,
  });

export const runSkill = (repo: string, name: string, skillName: string) =>
  apiPost<SendResponse>(`${workspacePath(repo, name)}/run-skill`, {
    skill_name: skillName,
  });
