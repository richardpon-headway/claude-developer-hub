// Hand-mirrored from backend/app/config/schema.py and backend/app/routes/repos.py.
// When the OpenAPI codegen slice lands these will be generated from /openapi.json
// and this file goes away.

export interface SetupStep {
  cmd: string;
  cwd: string;
}

export interface JiraConfig {
  tool: "acli" | "jira-cli" | "none";
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
  jira: JiraConfig;
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
