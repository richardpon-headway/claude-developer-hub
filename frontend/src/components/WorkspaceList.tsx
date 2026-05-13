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

const TIER_ORDER: Tier[] = ["needs_action", "ready_to_merge", "in_progress", "no_pr"];

const TIER_LABEL: Record<Tier, string> = {
  needs_action: "Needs your action",
  ready_to_merge: "Ready to merge",
  in_progress: "In progress",
  no_pr: "No PR yet",
};

// Within each tier, the order headlines appear in. Drives the order
// of the colored sub-group containers. "Needs your action" leads with
// the loudest code-action signals (red CI fails / conflicts), then
// review feedback, then cleanup-only items.
const HEADLINE_ORDER: Record<Tier, PrHeadline[]> = {
  needs_action: [
    "ci_failing",
    "merge_conflicts",
    "human_comment",
    "review_requested",
    "merged",
    "closed",
  ],
  ready_to_merge: ["ready_to_merge", "in_merge_queue"],
  in_progress: ["checks_running", "waiting_on_others", "draft"],
  no_pr: ["no_pr"],
};

// Visual classes for the colored group container that surrounds all
// workspaces sharing a headline. The container uses the same color
// family as the previous per-row tall badge so the visual language
// carries over; the small label at the top-right of each container
// names the headline.
const HEADLINE_GROUP_STYLE: Record<PrHeadline, { label: string; container: string; label_text: string }> = {
  ci_failing:        { label: "PR CI fail",  container: "border-red-800/70 bg-red-950/20",         label_text: "text-red-300" },
  merge_conflicts:   { label: "PR conflict", container: "border-red-800/70 bg-red-950/20",         label_text: "text-red-300" },
  human_comment:     { label: "PR review",   container: "border-amber-800/70 bg-amber-950/20",     label_text: "text-amber-300" },
  review_requested:  { label: "PR re-rev",   container: "border-amber-800/70 bg-amber-950/20",     label_text: "text-amber-300" },
  merged:            { label: "PR merged",   container: "border-purple-800/70 bg-purple-950/20",   label_text: "text-purple-300" },
  closed:            { label: "PR closed",   container: "border-zinc-700 bg-zinc-900/40",          label_text: "text-zinc-400" },
  ready_to_merge:    { label: "PR ready",    container: "border-emerald-800/70 bg-emerald-950/20", label_text: "text-emerald-300" },
  in_merge_queue:    { label: "PR queued",   container: "border-indigo-800/70 bg-indigo-950/20",   label_text: "text-indigo-300" },
  checks_running:    { label: "PR checks",   container: "border-amber-800/70 bg-amber-950/20",     label_text: "text-amber-300" },
  waiting_on_others: { label: "PR waiting",  container: "border-zinc-700 bg-zinc-900/40",          label_text: "text-zinc-400" },
  draft:             { label: "PR draft",    container: "border-zinc-700 bg-zinc-900/40",          label_text: "text-zinc-400" },
  no_pr:             { label: "No PR yet",   container: "border-zinc-800 bg-zinc-900/30",          label_text: "text-zinc-500" },
};

function headlineForWorktree(w: Worktree): PrHeadline {
  return w.pr_state?.headline ?? "no_pr";
}

function tierForWorktree(w: Worktree): Tier {
  return TIER_FOR_HEADLINE[headlineForWorktree(w)];
}

function compareByRepoName(a: Worktree, b: Worktree): number {
  if (a.repo !== b.repo) return a.repo < b.repo ? -1 : 1;
  return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
}

function groupByTierAndHeadline(
  worktrees: Worktree[],
): Record<Tier, Map<PrHeadline, Worktree[]>> {
  const out: Record<Tier, Map<PrHeadline, Worktree[]>> = {
    needs_action: new Map(),
    ready_to_merge: new Map(),
    in_progress: new Map(),
    no_pr: new Map(),
  };
  for (const w of worktrees) {
    const tier = tierForWorktree(w);
    const headline = headlineForWorktree(w);
    if (!out[tier].has(headline)) out[tier].set(headline, []);
    out[tier].get(headline)!.push(w);
  }
  for (const tier of TIER_ORDER) {
    for (const rows of out[tier].values()) {
      rows.sort(compareByRepoName);
    }
  }
  return out;
}

export function WorkspaceList({ worktrees, jira }: Props) {
  if (worktrees.length === 0) return null;

  const grouped = groupByTierAndHeadline(worktrees);

  return (
    <div className="space-y-4">
      {TIER_ORDER.map((tier) => {
        const headlineGroups = grouped[tier];
        const total = Array.from(headlineGroups.values()).reduce(
          (acc, r) => acc + r.length,
          0,
        );
        const isEmpty = total === 0;
        return (
          <section key={tier} className={isEmpty ? "opacity-50" : undefined}>
            <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-500">
              {TIER_LABEL[tier]}
              <span className="ml-2 text-zinc-600">· {total}</span>
            </h3>
            {isEmpty ? (
              <p className="rounded-lg border border-dashed border-zinc-800 px-4 py-3 text-xs italic text-zinc-600">
                no worktrees in this tier
              </p>
            ) : (
              <div className="space-y-6">
                {HEADLINE_ORDER[tier].map((headline) => {
                  const rows = headlineGroups.get(headline);
                  if (!rows || rows.length === 0) return null;
                  return (
                    <HeadlineGroup
                      key={headline}
                      headline={headline}
                      worktrees={rows}
                      jira={jira}
                    />
                  );
                })}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}

interface HeadlineGroupProps {
  headline: PrHeadline;
  worktrees: Worktree[];
  jira: JiraConfig | null;
}

function HeadlineGroup({ headline, worktrees, jira }: HeadlineGroupProps) {
  const style = HEADLINE_GROUP_STYLE[headline];
  return (
    <div className={`relative rounded-lg border-2 ${style.container} px-3 pb-3 pt-6`}>
      {/* Top-right "header" label naming the headline, as a small tab
          that visually overlaps the top edge of the colored container.
          pt-6 on the container gives the label breathing room above the
          first workspace card. */}
      <span
        className={`absolute -top-3 right-3 rounded border-2 bg-zinc-950 px-2.5 py-0.5 text-sm font-medium uppercase tracking-wide ${style.container} ${style.label_text}`}
      >
        {style.label}
      </span>
      <ul className="space-y-2">
        {worktrees.map((w) => (
          <WorkspaceRow key={`${w.repo}/${w.name}`} w={w} jira={jira} />
        ))}
      </ul>
    </div>
  );
}

interface RowProps {
  w: Worktree;
  jira: JiraConfig | null;
}

function WorkspaceRow({ w, jira }: RowProps) {
  return (
    <li className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
      <div className="flex items-start justify-between gap-4">
          <Link
            to="/workspace/$repo/$name"
            params={{ repo: w.repo, name: w.name }}
            className="min-w-0 truncate font-medium text-zinc-100 hover:text-indigo-300"
          >
            {w.name}
          </Link>
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
          <div className="flex shrink-0 items-center gap-2">
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
  });

  const tooltip = !ready
    ? `worktree status is ${status}; nothing to spawn into`
    : mutation.error
      ? mutation.error instanceof ApiError
        ? mutation.error.detail
        : String(mutation.error)
      : "Open this workspace in a new iTerm2 window (multiple windows are fine)";

  return (
    <Tooltip text={tooltip}>
      <button
        type="button"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending || !ready}
        className="rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-200 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {mutation.isPending ? "Opening…" : "iTerm2"}
      </button>
    </Tooltip>
  );
}
