import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { getPrUrl, spawnIterm } from "../api/worktrees";
import type { JiraConfig, PrHeadline, Worktree, WorktreeStatus } from "../api/types";
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

const statusTooltip: Record<WorktreeStatus, string> = {
  ready: "Worktree setup completed; usable now",
  setting_up: "git worktree add + setup_steps[] running",
  failed: "A setup step exited non-zero. Check the setup log on Manage.",
  stale: "Tracked in DB but the worktree path is missing on disk.",
  removing: "Deletion in progress.",
};

// Bucket headlines into action tiers so the hub answers "where does
// this worktree need attention" at a glance. Merged/closed live in
// "Needs your action" because a finished PR with a still-extant
// worktree is itself a cleanup task (delete the branch, prune the
// worktree, close the ticket).
type Tier = "needs_action" | "ready_to_merge" | "in_progress" | "no_pr";

const TIER_FOR_HEADLINE: Record<PrHeadline, Tier> = {
  ci_failing: "needs_action",
  merge_conflicts: "needs_action",
  human_comment: "needs_action",
  review_requested: "needs_action",
  merged: "needs_action",
  closed: "needs_action",
  ready_to_merge: "ready_to_merge",
  in_merge_queue: "ready_to_merge",
  checks_running: "in_progress",
  waiting_on_others: "in_progress",
  draft: "in_progress",
  no_pr: "no_pr",
};

// Display order on the hub. "Ready to merge" leads because a PR
// that's approved + green is the cheapest action item: one button
// click and it's done. "Needs your action" is the next-loudest:
// real code work required.
const TIER_ORDER: Tier[] = ["ready_to_merge", "needs_action", "in_progress", "no_pr"];

const TIER_LABEL: Record<Tier, string> = {
  needs_action: "Needs your action",
  ready_to_merge: "Ready to merge",
  in_progress: "In progress",
  no_pr: "No PR yet",
};

// Per-label chip styling. Each chip is rendered inline on its row;
// the color family carries over from what used to be the headline-group
// container so the visual language stays consistent. Label text is
// short (lower-case) to fit inline next to the workspace name.
const LABEL_CHIP_STYLE: Record<PrHeadline, { label: string; chip: string }> = {
  ci_failing:        { label: "ci fail",  chip: "border-red-800 bg-red-900/40 text-red-300" },
  merge_conflicts:   { label: "conflict", chip: "border-red-800 bg-red-900/40 text-red-300" },
  human_comment:     { label: "review",   chip: "border-amber-800 bg-amber-900/40 text-amber-300" },
  review_requested:  { label: "re-rev",   chip: "border-amber-800 bg-amber-900/40 text-amber-300" },
  merged:            { label: "merged",   chip: "border-purple-800 bg-purple-900/40 text-purple-300" },
  closed:            { label: "closed",   chip: "border-zinc-700 bg-zinc-800 text-zinc-400" },
  ready_to_merge:    { label: "Approved - Ready to Merge", chip: "border-emerald-800 bg-emerald-900/40 text-emerald-300" },
  in_merge_queue:    { label: "queued",   chip: "border-indigo-800 bg-indigo-900/40 text-indigo-300" },
  checks_running:    { label: "checks",   chip: "border-amber-800 bg-amber-900/40 text-amber-300" },
  waiting_on_others: { label: "waiting",  chip: "border-zinc-700 bg-zinc-800 text-zinc-400" },
  draft:             { label: "draft",    chip: "border-zinc-700 bg-zinc-800 text-zinc-400" },
  no_pr:             { label: "no PR",    chip: "border-zinc-700 bg-zinc-800/60 text-zinc-500" },
};

function labelsForWorktree(w: Worktree): PrHeadline[] {
  // Newer backend payloads carry an explicit list; older rows persisted
  // before the multi-label refactor only have `headline`. Fall back to
  // a one-element list so the row still renders at least one chip.
  if (w.pr_state?.labels && w.pr_state.labels.length > 0) {
    return w.pr_state.labels;
  }
  if (w.pr_state?.headline) {
    return [w.pr_state.headline];
  }
  return ["no_pr"];
}

function tierForWorktree(w: Worktree): Tier {
  // labels[0] is the highest-priority signal; the original first-match
  // classifier's priority order is preserved on the backend.
  return TIER_FOR_HEADLINE[labelsForWorktree(w)[0]];
}

function compareByRepoName(a: Worktree, b: Worktree): number {
  if (a.repo !== b.repo) return a.repo < b.repo ? -1 : 1;
  return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
}

function groupByTier(worktrees: Worktree[]): Record<Tier, Worktree[]> {
  const out: Record<Tier, Worktree[]> = {
    needs_action: [],
    ready_to_merge: [],
    in_progress: [],
    no_pr: [],
  };
  for (const w of worktrees) {
    out[tierForWorktree(w)].push(w);
  }
  for (const tier of TIER_ORDER) {
    out[tier].sort(compareByRepoName);
  }
  return out;
}

export function WorkspaceList({ worktrees, jira }: Props) {
  if (worktrees.length === 0) return null;

  const grouped = groupByTier(worktrees);

  return (
    <div className="space-y-4">
      {TIER_ORDER.map((tier) => {
        const rows = grouped[tier];
        const isEmpty = rows.length === 0;
        return (
          <section key={tier} className={isEmpty ? "opacity-50" : undefined}>
            <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-500">
              {TIER_LABEL[tier]}
              <span className="ml-2 text-zinc-600">· {rows.length}</span>
            </h3>
            {isEmpty ? (
              <p className="rounded-lg border border-dashed border-zinc-800 px-4 py-3 text-xs italic text-zinc-600">
                no worktrees in this tier
              </p>
            ) : (
              <ul className="space-y-2">
                {rows.map((w) => (
                  <WorkspaceRow key={`${w.repo}/${w.name}`} w={w} jira={jira} />
                ))}
              </ul>
            )}
          </section>
        );
      })}
    </div>
  );
}

interface RowProps {
  w: Worktree;
  jira: JiraConfig | null;
}

function WorkspaceRow({ w, jira }: RowProps) {
  const labels = labelsForWorktree(w);
  return (
    <li className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
      <div className="flex items-start justify-between gap-4">
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <Link
              to="/workspace/$repo/$name"
              params={{ repo: w.repo, name: w.name }}
              className="truncate font-medium text-zinc-100 hover:text-indigo-300"
            >
              {w.name}
            </Link>
            {labels.map((label) => {
              const style = LABEL_CHIP_STYLE[label];
              return (
                <span
                  key={label}
                  className={`rounded border px-1.5 py-0.5 text-[10px] ${style.chip}`}
                >
                  {style.label}
                </span>
              );
            })}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Tooltip text={statusTooltip[w.status]}>
              <span
                className={`rounded border px-1.5 py-0.5 text-[10px] ${statusStyle[w.status]}`}
              >
                {w.status}
              </span>
            </Tooltip>
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
        </div>
        <div className="mt-2 flex items-end justify-between gap-4">
          <div className="min-w-0 flex-1 space-y-0.5 text-xs text-zinc-500">
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
          <div className="flex shrink-0 items-start gap-2">
            <OpenItermButton repo={w.repo} name={w.name} ready={w.status === "ready"} status={w.status} />
            <PrButton repo={w.repo} name={w.name} />
            <Tooltip text="Workspace actions: run skills, send text, view setup log">
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

interface OpenItermButtonProps {
  repo: string;
  name: string;
  ready: boolean;
  status: WorktreeStatus;
}

function OpenItermButton({ repo, name, ready, status }: OpenItermButtonProps) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => spawnIterm(repo, name),
    onSuccess: () => {
      // Pop the claude ● badge as soon as the spawn returns rather than
      // waiting the full 5s for the workspaces query to re-poll.
      queryClient.invalidateQueries({ queryKey: ["worktrees"] });
    },
    onError: () => {
      // A common failure mode here is "worktree path missing on disk"
      // (user removed the directory outside CDH). Kick the worktrees
      // query so the next poll re-runs and Sync-worktrees-style cleanup
      // surfaces — the row should disappear or flip to a stale status
      // shortly after.
      queryClient.invalidateQueries({ queryKey: ["worktrees"] });
    },
  });

  const errorDetail = mutation.error
    ? mutation.error instanceof ApiError
      ? mutation.error.detail
      : String(mutation.error)
    : null;

  const tooltip = !ready
    ? `worktree status is ${status}; nothing to spawn into`
    : errorDetail
      ? errorDetail
      : "Open this workspace in a new iTerm2 window (multiple windows are fine)";

  return (
    <div className="flex flex-col items-end gap-1">
      <Tooltip text={tooltip}>
        <button
          type="button"
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending || !ready}
          className={`rounded border px-3 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-50 ${
            errorDetail
              ? "border-red-700 bg-red-950/40 text-red-300 hover:bg-red-900/40"
              : "border-zinc-700 bg-zinc-800 text-zinc-200 hover:bg-zinc-700"
          }`}
        >
          {mutation.isPending
            ? "Opening…"
            : errorDetail
              ? "iTerm2 ✗"
              : "iTerm2"}
        </button>
      </Tooltip>
      {errorDetail && (
        <p
          role="alert"
          className="max-w-[220px] text-right text-[10px] leading-tight text-red-400"
          title={errorDetail}
        >
          {errorDetail}
        </p>
      )}
    </div>
  );
}
