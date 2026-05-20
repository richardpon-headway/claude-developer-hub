import { apiGet, apiPost, apiPut } from "./client";
import type {
  ListWorktreesResponse,
  PrUrlResponse,
  SendResponse,
  SpawnItermResponse,
  SyncResponse,
  TokenUsageResponse,
  Worktree,
  WorktreeDetail,
} from "./types";

export const listWorktrees = () =>
  apiGet<ListWorktreesResponse>("/api/worktrees");

export const getWorktree = (repo: string, name: string) =>
  apiGet<WorktreeDetail>(
    `/api/worktree/${encodeURIComponent(repo)}/${encodeURIComponent(name)}`,
  );

export const getTokenUsage = () => apiGet<TokenUsageResponse>("/api/token-usage");

export const syncWorktrees = () =>
  apiPost<SyncResponse>("/api/worktrees/sync", {});

const workspacePath = (repo: string, name: string) =>
  `/api/worktree/${encodeURIComponent(repo)}/${encodeURIComponent(name)}`;

export const spawnIterm = (repo: string, name: string) =>
  apiPost<SpawnItermResponse>(`${workspacePath(repo, name)}/spawn-iterm`, {});

export interface FocusItermResponse {
  focused: boolean;
}

export const focusIterm = (repo: string, name: string) =>
  apiPost<FocusItermResponse>(`${workspacePath(repo, name)}/focus-iterm`, {});

export const recreateWorktree = (repo: string, name: string) =>
  apiPost<Worktree>(`${workspacePath(repo, name)}/recreate`, {});

export interface OpenCursorResponse {
  opened: boolean;
}

export const openInCursor = (repo: string, name: string, file?: string) =>
  apiPost<OpenCursorResponse>(
    `${workspacePath(repo, name)}/open-cursor`,
    file ? { file } : {},
  );

export interface PrFile {
  path: string;
  additions: number;
  deletions: number;
  github_diff_anchor: string;
}

export interface PrFilesResponse {
  files: PrFile[];
}

export const getPrFiles = (repo: string, name: string) =>
  apiGet<PrFilesResponse>(`${workspacePath(repo, name)}/pr-files`);

export type FileViewLineKind =
  | "context"
  | "committed_add"
  | "committed_remove"
  | "uncommitted_add"
  | "uncommitted_remove";

export interface FileViewHunkLine {
  kind: FileViewLineKind;
  content: string;
  on_disk_lineno: number | null;
}

export interface FileViewHunk {
  on_disk_start: number;
  on_disk_end: number;
  lines: FileViewHunkLine[];
}

export interface FileViewResponse {
  path: string;
  github_diff_anchor: string;
  workspace_branch: string | null;
  pr_branch: string | null;
  branch_matches_pr: boolean;
  file_in_pr_diff: boolean;
  is_binary: boolean;
  is_large: boolean;
  is_missing: boolean;
  size_bytes: number | null;
  rename_from: string | null;
  on_disk_content: string | null;
  line_count: number | null;
  hunks: FileViewHunk[];
  is_generated_or_lockfile: boolean;
}

export const getFileView = (
  repo: string,
  name: string,
  path: string,
  loadAnyway = false,
) =>
  apiGet<FileViewResponse>(
    `${workspacePath(repo, name)}/file?path=${encodeURIComponent(path)}${
      loadAnyway ? "&load_anyway=true" : ""
    }`,
  );

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

export const getPrUrl = (repo: string, name: string) =>
  apiGet<PrUrlResponse>(`${workspacePath(repo, name)}/pr-url`);

export const refreshPrState = (repo: string, name: string) =>
  apiPost<import("./types").PrStateSummary>(
    `${workspacePath(repo, name)}/pr-state/refresh`,
    {},
  );

export interface UpdateNotesResponse {
  notes: string;
}

export const updateNotes = (repo: string, name: string, notes: string) =>
  apiPut<UpdateNotesResponse>(`${workspacePath(repo, name)}/notes`, { notes });
