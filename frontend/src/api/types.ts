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

export type PrHeadline =
  | "no_pr"
  | "merged"
  | "closed"
  | "ci_failing"
  | "merge_conflicts"
  | "in_merge_queue"
  | "ready_to_merge"
  | "unresolved_comments"
  | "human_comment"
  | "review_requested"
  | "checks_running"
  | "waiting_on_others"
  | "draft";

export interface PrChecks {
  passed: number;
  fail: number;
  pending: number;
  total: number;
}

export interface PrComments {
  human: number;
  bot: number;
  total: number;
}

export interface PrStateSummary {
  headline: PrHeadline;
  // Every signal that applies, priority-ordered (so labels[0] === headline).
  // Older pr_state rows persisted before the multi-label change may
  // legitimately omit this — the read path back-fills it from headline.
  labels: PrHeadline[];
  pr_number: number | null;
  url: string | null;
  title: string | null;
  is_draft: boolean;
  mergeable: string | null;
  merge_state_status: string | null;
  review_decision: string | null;
  checks: PrChecks;
  comments: PrComments;
  base_ref: string | null;
  head_ref: string | null;
  updated_at: string | null;
  checked_at: string;
  // Count of unresolved + non-outdated PR review threads.
  // Drives the `unresolved_comments` label.
  unresolved_threads: number;
}

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
  pr_state: PrStateSummary | null;
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

export interface SpawnRepoItermResponse {
  window_id: string;
  claude_session_id: string;
  shell_session_id: string;
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

export interface RemovedWorktree {
  repo: string;
  name: string;
  path: string;
  reason: string;
}

export interface SkippedWorktree {
  repo: string;
  path: string;
  reason: string;
}

export interface SyncResponse {
  imported: ImportedWorktree[];
  removed: RemovedWorktree[];
  skipped: SkippedWorktree[];
}

export interface PrUrlResponse {
  url: string;
}

export type InboxCiStatus = "pass" | "fail" | "pending" | "none";

export interface InboxPr {
  pr_repo: string;
  pr_number: number;
  title: string;
  author_login: string;
  head_ref: string;
  base_ref: string;
  is_draft: boolean;
  url: string;
  updated_at: string;
  ci_status: InboxCiStatus;
  // Every reason the PR is in this user's inbox, priority-ordered.
  // First entry is the primary signal (used for subsection placement).
  // Values: "author" | "reviewer" | "team:<owner/slug>"
  sources: string[];
  stack_top_pr_number: number | null;
  stack_size: number;
  // 1 = bottom of stack (closest to main); stack_size = top of stack
  stack_position: number;
  repo_configured: boolean;
}

export interface InboxResponse {
  prs: InboxPr[];
  checked_at: string | null;
}

export interface GlobalSkill {
  name: string;
  label: string;
  description: string | null;
  cwd: string;
}

export interface WorkspaceSkill {
  name: string;
  label: string;
  description: string | null;
}

export interface GlobalSkillResponse {
  window_id: string;
  claude_session_id: string;
}
