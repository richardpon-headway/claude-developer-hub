import { apiGet } from "./client";
import type { InboxResponse } from "./types";

export const getInbox = () => apiGet<InboxResponse>("/api/inbox");
