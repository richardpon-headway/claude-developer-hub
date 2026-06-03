import { apiGet } from "./client";
import type { GetWorkspacesResponse } from "./types";

export const getWorkspaces = () =>
  apiGet<GetWorkspacesResponse>("/api/workspaces");
