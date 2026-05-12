import { Link } from "@tanstack/react-router";

import type { Worktree, WorktreeStatus } from "../api/types";

interface Props {
  worktrees: Worktree[];
}

const statusStyle: Record<WorktreeStatus, string> = {
  ready: "bg-emerald-900/40 text-emerald-300 border-emerald-800",
  setting_up: "bg-amber-900/40 text-amber-300 border-amber-800",
  failed: "bg-red-900/40 text-red-300 border-red-800",
  stale: "bg-zinc-800 text-zinc-400 border-zinc-700",
  removing: "bg-zinc-800 text-zinc-400 border-zinc-700",
};

function groupByRepo(worktrees: Worktree[]): Record<string, Worktree[]> {
  const out: Record<string, Worktree[]> = {};
  for (const w of worktrees) {
    if (!out[w.repo]) out[w.repo] = [];
    out[w.repo].push(w);
  }
  return out;
}

export function WorkspaceList({ worktrees }: Props) {
  if (worktrees.length === 0) return null;

  const grouped = groupByRepo(worktrees);
  const repoNames = Object.keys(grouped).sort();

  return (
    <div className="space-y-4">
      {repoNames.map((repo) => (
        <section key={repo}>
          <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-500">
            {repo}
          </h3>
          <ul className="divide-y divide-zinc-800 rounded-lg border border-zinc-800 bg-zinc-900/50">
            {grouped[repo].map((w) => (
              <li key={`${w.repo}/${w.name}`} className="px-4 py-3">
                <div className="flex items-center justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <Link
                        to="/workspace/$repo/$name"
                        params={{ repo: w.repo, name: w.name }}
                        className="font-medium text-zinc-100 hover:text-indigo-300"
                      >
                        {w.name}
                      </Link>
                      <span
                        className={`rounded border px-1.5 py-0.5 text-[10px] ${statusStyle[w.status]}`}
                      >
                        {w.status}
                      </span>
                      {w.has_claude_session && (
                        <span
                          title="Claude session is open in iTerm2"
                          className="rounded border border-emerald-800 bg-emerald-900/40 px-1.5 py-0.5 text-[10px] text-emerald-300"
                        >
                          claude ●
                        </span>
                      )}
                    </div>
                    <div className="mt-1 flex flex-wrap gap-x-4 text-xs text-zinc-500">
                      <span>branch: {w.branch}</span>
                      {w.ticket && <span>ticket: {w.ticket}</span>}
                      <span className="truncate font-mono text-zinc-600" title={w.path}>
                        {w.path}
                      </span>
                    </div>
                  </div>
                  <Link
                    to="/workspace/$repo/$name"
                    params={{ repo: w.repo, name: w.name }}
                    className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-200 hover:bg-zinc-700"
                  >
                    Open
                  </Link>
                </div>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
