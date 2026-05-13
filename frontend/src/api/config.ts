import { apiGet, apiPost } from "./client";
import type {
  GlobalSkill,
  GlobalSkillResponse,
  JiraConfig,
  WorkspaceSkill,
} from "./types";

export const getJiraConfig = () => apiGet<JiraConfig>("/api/config/jira");

export const getGlobalSkills = () =>
  apiGet<GlobalSkill[]>("/api/config/skills");

export const getWorkspaceSkills = () =>
  apiGet<WorkspaceSkill[]>("/api/config/workspace-skills");

export const runGlobalSkill = (skill: string) =>
  apiPost<GlobalSkillResponse>("/api/skills/global", { skill });
