import { useQuery } from "@tanstack/react-query";

import { getTerminalInfo } from "./config";
import type { TerminalInfo } from "./types";

// Fallback used while the /api/config/terminal request is in-flight or
// has failed. We default to iTerm2 because that's also the schema's
// default and the original CDH baseline; Ghostty users see a brief
// "iTerm2" flash on first paint, then the real name once the query
// resolves (single roundtrip; cached for the session via staleTime).
const FALLBACK: TerminalInfo = { kind: "iterm2", display_name: "iTerm2" };

/**
 * Read the active terminal's display name (e.g. "iTerm2" or "Ghostty")
 * so UI strings can avoid hardcoding a terminal. Cached across the app
 * — the underlying value only changes on a backend restart.
 *
 * Returns a stable fallback ("Terminal") while the request is in flight
 * or on error, so callers don't have to handle a loading state for what
 * is effectively a static config value.
 */
export function useTerminalInfo(): TerminalInfo {
  const q = useQuery({
    queryKey: ["terminal-info"],
    queryFn: getTerminalInfo,
    staleTime: Infinity,
    gcTime: Infinity,
  });
  return q.data ?? FALLBACK;
}
