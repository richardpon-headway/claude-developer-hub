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

export type WorktreeStatus =
  | "setting_up"
  | "ready"
  // `git worktree add` succeeded but a setup_step errored. Code is
  // on disk and usable; setup automation didn't fully complete.
  | "code_on_disk"
  | "failed"
  | "stale"
  | "removing";

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
  // GitHub login of the PR's author. Mirrors PrSummary.author_login
  // on the backend; surfaced here for symmetry, though the hub reads
  // ownership from Worktree.pr_author_login (also lazy-backfilled
  // from this value by the poll loop).
  author_login: string | null;
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
  // GitHub login of the PR's author when known. Captured at pull-down
  // time from the inbox row and lazy-filled by the pr_state poll for
  // worktrees that pre-date the column. Compared against
  // `ListWorktreesResponse.user_login` to decide whether the row sorts
  // into the REVIEWING tier. Null → "not yet known" → treat as owner.
  pr_author_login: string | null;
  // Free-form per-workspace notes (markdown). Null or "" means the
  // row has no notes. Edited inline on the hub row and on the
  // workspace detail page; auto-saved on debounce.
  notes: string | null;
  created_at: string;
  status: WorktreeStatus;
  has_claude_session: boolean;
  pr_state: PrStateSummary | null;
}

export interface ListWorktreesResponse {
  worktrees: Worktree[];
  // Local user's gh login when resolvable, else null. The frontend
  // compares each row's `pr_author_login` against this to decide
  // REVIEWING vs. owner. Null disables the split.
  user_login: string | null;
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
  url: string;
  is_draft: boolean;
  ci_status: InboxCiStatus;
  // Every reason the PR is in this user's inbox, priority-ordered.
  // First entry is the primary signal. Values after the persistent-
  // inbox redesign: "reviewer" | "assignee" | "mentions".
  sources: string[];
  // Free-form per-row notes. Same UX as Worktree.notes. Null/"" means
  // the row has no notes; the editor renders the empty state.
  notes: string | null;
  // Extracted ticket id (e.g. "PROJ-123") when one of the configured
  // repos' ticket_pattern matched the PR title. Drives the Jira link.
  ticket: string | null;
  // PR's updatedAt from gh; used to sort the inbox newest-first.
  pr_updated_at: string;
  // First time the inbox poller saw this PR. Never updated.
  added_at: string;
  // Most recent tick where gh search returned this PR. The auto-
  // removal sweep uses this to bound how many `gh pr view` probes
  // fire per tick.
  last_seen_at: string;
  // True when the inbox row's `pr_repo` maps to a locally-configured
  // RepoConfig. Drives "Pull down" vs "Configure repo + pull down".
  repo_configured: boolean;
}

export interface InboxResponse {
  prs: InboxPr[];
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
