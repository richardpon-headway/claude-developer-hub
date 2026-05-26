import { apiGet, apiPost } from "./client";
import type {
  GlobalSkill,
  GlobalSkillResponse,
  JiraConfig,
  TerminalInfo,
  WorkspaceSkill,
} from "./types";

export interface DiffConfig {
  default_context_lines: number;
  expand_all_threshold: number;
}

export const getTerminalInfo = () =>
  apiGet<TerminalInfo>("/api/config/terminal");

export const getJiraConfig = () => apiGet<JiraConfig>("/api/config/jira");

export const getDiffConfig = () => apiGet<DiffConfig>("/api/config/diff");

export const getGlobalSkills = () =>
  apiGet<GlobalSkill[]>("/api/config/skills");

export const getWorkspaceSkills = () =>
  apiGet<WorkspaceSkill[]>("/api/config/workspace-skills");

export const runGlobalSkill = (skill: string) =>
  apiPost<GlobalSkillResponse>("/api/skills/global", { skill });

export const runGlobalFreeform = (prompt: string) =>
  apiPost<GlobalSkillResponse>("/api/skills/global/freeform", { prompt });
