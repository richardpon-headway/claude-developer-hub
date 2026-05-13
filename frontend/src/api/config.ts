import { apiGet, apiPost } from "./client";
import type { GlobalSkill, GlobalSkillResponse, JiraConfig } from "./types";

export const getJiraConfig = () => apiGet<JiraConfig>("/api/config/jira");

export const getGlobalSkills = () =>
  apiGet<GlobalSkill[]>("/api/config/skills");

export const runGlobalSkill = (skill: string) =>
  apiPost<GlobalSkillResponse>("/api/skills/global", { skill });
