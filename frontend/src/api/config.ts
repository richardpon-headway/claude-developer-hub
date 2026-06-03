import { apiGet, apiPost } from "./client";
import type {
  GlobalSkillResponse,
  JiraConfig,
  TerminalInfo,
} from "./types";

export interface DiffConfig {
  default_context_lines: number;
  expand_all_threshold: number;
}

export const getTerminalInfo = () =>
  apiGet<TerminalInfo>("/api/config/terminal");

export const getJiraConfig = () => apiGet<JiraConfig>("/api/config/jira");

export const getDiffConfig = () => apiGet<DiffConfig>("/api/config/diff");

export const runGlobalFreeform = (prompt: string) =>
  apiPost<GlobalSkillResponse>("/api/skills/global/freeform", { prompt });

export const openGlobalClaude = () =>
  apiPost<GlobalSkillResponse>("/api/skills/global/open", {});
