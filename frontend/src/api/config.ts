import { apiGet } from "./client";
import type { JiraConfig } from "./types";

export const getJiraConfig = () => apiGet<JiraConfig>("/api/config/jira");
