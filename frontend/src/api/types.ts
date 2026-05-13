// Hand-mirrored from backend/app/config/schema.py and backend/app/routes/repos.py.
// When the OpenAPI codegen slice lands these will be generated from /openapi.json
// and this file goes away.

export interface SetupStep {
  cmd: string;
  cwd: string;
}

export interface JiraConfig {
  tool: "acli" | "jira-cli" | "none";
  base_url: string | null;
  list_jql: string | null;
}

export interface RepoConfig {
  name: string;
  path: string;
  default_branch: string;
  branch_prefix: string;
  worktree_path_template: string;
  setup_steps: SetupStep[];
  ticket_pattern: string | null;
}

export type OnboardState = "pending" | "saved" | "error";

export interface OnboardResponse {
  session_id: string;
  prompt: string;
}

export interface OnboardStatus {
  session_id: string;
  state: OnboardState;
  proposed_entry: RepoConfig | null;
  error: string | null;
}

export interface OnboardCompleteRequest {
  session_id: string;
  proposed_entry: Partial<RepoConfig> & { name: string; path: string };
}

export interface OnboardCompleteResponse {
  state: "saved";
  saved_entry: RepoConfig;
}

export interface RepoCandidate {
  path: string;
  name: string;
  already_configured: boolean;
}

export type WorktreeStatus = "setting_up" | "ready" | "failed" | "stale" | "removing";

export interface Worktree {
  repo: string;
  name: string;
  path: string;
  branch: string;
  ticket: string | null;
  pr_number: number | null;
  pr_repo: string | null;
  created_at: string;
  status: WorktreeStatus;
  has_claude_session: boolean;
}

export interface WorktreeDetail {
  row: Worktree;
  log: string[];
}

export interface TokenUsageRow {
  topic_id: string;
  sessions: number;
  output: number;
  input: number;
  messages: number;
  last_at: string | null;
  label: string | null;
  summary: string | null;
}

export interface TokenUsageResponse {
  offline: boolean;
  today_output: number;
  today_input: number;
  today_messages: number;
  rows: TokenUsageRow[];
}

export interface SpawnItermResponse {
  window_id: string;
  claude_session_id: string;
  shell_session_id: string;
  claude_session_uuid: string | null;
  sidecar_path: string | null;
}

export interface SendResponse {
  sent: boolean;
}

export interface ImportedWorktree {
  repo: string;
  name: string;
  path: string;
  branch: string;
  ticket: string | null;
}

export interface SkippedWorktree {
  repo: string;
  path: string;
  reason: string;
}

export interface DiscoverResponse {
  imported: ImportedWorktree[];
  skipped: SkippedWorktree[];
}

export interface PrUrlResponse {
  url: string;
}
