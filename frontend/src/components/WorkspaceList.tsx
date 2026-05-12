import { useState } from "react";
import { Link } from "@tanstack/react-router";

import { ApiError } from "../api/client";
import { getPrUrl } from "../api/worktrees";
import type { JiraConfig, Worktree, WorktreeStatus } from "../api/types";
import { Tooltip } from "./Tooltip";

interface Props {
  worktrees: Worktree[];
  jira: JiraConfig | null;
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

export function WorkspaceList({ worktrees, jira }: Props) {
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
              <WorkspaceRow key={`${w.repo}/${w.name}`} w={w} jira={jira} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

interface RowProps {
  w: Worktree;
  jira: JiraConfig | null;
}

function WorkspaceRow({ w, jira }: RowProps) {
  return (
    <li className="px-4 py-3">
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
              <Tooltip text="Claude session is open in iTerm2">
                <span
                  className="rounded border border-emerald-800 bg-emerald-900/40 px-1.5 py-0.5 text-[10px] text-emerald-300"
                >
                  claude ●
                </span>
              </Tooltip>
            )}
          </div>
          <div className="mt-1 space-y-0.5 text-xs text-zinc-500">
            <div>branch: {w.branch}</div>
            {w.ticket && (
              <div>
                ticket: <TicketValue ticket={w.ticket} jira={jira} />
              </div>
            )}
            <div className="truncate font-mono text-zinc-600" title={w.path}>
              {w.path}
            </div>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <PrButton repo={w.repo} name={w.name} />
          <Tooltip text="Workspace actions: open in iTerm2, run skills, send text">
            <Link
              to="/workspace/$repo/$name"
              params={{ repo: w.repo, name: w.name }}
              className="rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-200 hover:bg-zinc-700"
            >
              Manage
            </Link>
          </Tooltip>
        </div>
      </div>
    </li>
  );
}

interface TicketValueProps {
  ticket: string;
  jira: JiraConfig | null;
}

function TicketValue({ ticket, jira }: TicketValueProps) {
  if (!jira?.base_url) return <>{ticket}</>;
  const base = jira.base_url.replace(/\/+$/, "");
  return (
    <a
      href={`${base}/browse/${ticket}`}
      target="_blank"
      rel="noopener noreferrer"
      className="text-zinc-400 underline decoration-zinc-700 underline-offset-2 hover:text-indigo-300 hover:decoration-indigo-400"
    >
      {ticket}
    </a>
  );
}

interface PrButtonProps {
  repo: string;
  name: string;
}

type PrState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "missing" }
  | { kind: "error"; message: string };

function PrButton({ repo, name }: PrButtonProps) {
  const [state, setState] = useState<PrState>({ kind: "idle" });

  const onClick = async () => {
    setState({ kind: "loading" });
    try {
      const { url } = await getPrUrl(repo, name);
      window.open(url, "_blank", "noopener,noreferrer");
      setState({ kind: "idle" });
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setState({ kind: "missing" });
        return;
      }
      setState({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const label =
    state.kind === "loading"
      ? "Looking up…"
      : state.kind === "missing"
        ? "No PR"
        : state.kind === "error"
          ? "PR failed"
          : "PR";

  const tooltip =
    state.kind === "error"
      ? state.message
      : state.kind === "missing"
        ? "gh pr view found no PR for this branch yet"
        : "Open the GitHub PR for this branch";

  return (
    <Tooltip text={tooltip}>
      <button
        type="button"
        onClick={onClick}
        disabled={state.kind === "loading" || state.kind === "missing"}
        className="rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-200 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {label}
      </button>
    </Tooltip>
  );
}
