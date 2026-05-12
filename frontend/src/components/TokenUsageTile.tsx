import { useQuery } from "@tanstack/react-query";

import { getTokenUsage } from "../api/worktrees";
import type { TokenUsageRow } from "../api/types";

function formatNumber(n: number): string {
  return new Intl.NumberFormat("en-US").format(n);
}

function topThreeBy(field: keyof Pick<TokenUsageRow, "output" | "input">, rows: TokenUsageRow[]) {
  return [...rows].sort((a, b) => b[field] - a[field]).slice(0, 3);
}

export function TokenUsageTile() {
  const query = useQuery({
    queryKey: ["token-usage"],
    queryFn: getTokenUsage,
    // Token usage drifts slowly enough that aggressive refetching is wasteful.
    staleTime: 30_000,
  });

  if (query.isLoading) {
    return (
      <section className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
        <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
          Tokens today
        </h3>
        <p className="mt-2 text-sm text-zinc-500">Loading…</p>
      </section>
    );
  }

  if (query.isError || !query.data) {
    return (
      <section className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
        <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
          Tokens today
        </h3>
        <p className="mt-2 text-sm text-zinc-500">
          Could not reach the token-monitor proxy.
        </p>
      </section>
    );
  }

  if (query.data.offline) {
    return (
      <section className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
            Tokens today
          </h3>
          <span className="rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
            monitor offline
          </span>
        </div>
        <p className="mt-2 text-sm text-zinc-500">
          claude-token-monitor isn't running on :47821.
        </p>
      </section>
    );
  }

  const rows = query.data.rows;
  const totalOutput = rows.reduce((acc, r) => acc + r.output, 0);
  const totalSessions = rows.reduce((acc, r) => acc + r.sessions, 0);
  const top = topThreeBy("output", rows);

  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
      <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
        Tokens today
      </h3>
      <div className="mt-2 flex items-baseline gap-4">
        <div>
          <div className="text-2xl font-semibold text-zinc-100">
            {formatNumber(totalOutput)}
          </div>
          <div className="text-xs text-zinc-500">output</div>
        </div>
        <div className="text-xs text-zinc-500">
          across {totalSessions} session{totalSessions === 1 ? "" : "s"}
        </div>
      </div>
      {top.length > 0 && (
        <ul className="mt-3 space-y-1 text-xs">
          {top.map((row) => (
            <li
              key={row.topic_id}
              className="flex items-baseline justify-between gap-2 text-zinc-400"
            >
              <span className="truncate" title={row.summary ?? undefined}>
                {row.label ?? row.topic_id}
              </span>
              <span className="shrink-0 font-mono text-zinc-500">
                {formatNumber(row.output)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
