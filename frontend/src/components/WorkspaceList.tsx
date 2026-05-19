import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { focusIterm, getPrUrl, recreateWorktree, spawnIterm } from "../api/worktrees";
import type { JiraConfig, PrHeadline, Worktree, WorktreeStatus } from "../api/types";
import { Tooltip } from "./Tooltip";
import { WorkspaceNotes } from "./WorkspaceNotes";

interface Props {
  worktrees: Worktree[];
  jira: JiraConfig | null;
  // Local user's gh login when known. Drives the REVIEWING tier:
  // worktrees whose `pr_author_login` is set AND doesn't match this
  // value are rendered as "I'm reviewing someone else's PR locally."
  // Null disables the split (everything sorts by state-tier, the
  // pre-REVIEWING behavior).
  userLogin: string | null;
}


// Bucket headlines into action tiers so the hub answers "where does
// this worktree need attention" at a glance.
//
// REVIEWING sits at the top because the user's mental model is
// "other people's PRs go there, my work goes below." Ownership trumps
// state inside that decision: a reviewer-owned PR that's also merged
// or ci_failing still sorts into REVIEWING. Its label chip still
// renders on the row, so urgency isn't lost.
//
// Below REVIEWING, "Merged" gets its own tier because a merged PR
// with a still-extant worktree is a pure cleanup task. "Closed" stays
// in "Needs your action" since closed-not-merged is rare and often
// needs investigation.
type Tier =
  | "reviewing"
  | "merged"
  | "ready_to_merge"
  | "needs_action"
  | "in_progress"
  | "no_pr";

const TIER_FOR_HEADLINE: Record<PrHeadline, Tier> = {
  merged: "merged",
  ci_failing: "needs_action",
  merge_conflicts: "needs_action",
  unresolved_comments: "needs_action",
  human_comment: "needs_action",
  review_requested: "needs_action",
  closed: "needs_action",
  ready_to_merge: "ready_to_merge",
  in_merge_queue: "ready_to_merge",
  checks_running: "in_progress",
  waiting_on_others: "in_progress",
  draft: "in_progress",
  no_pr: "no_pr",
};

// Display order on the hub, top to bottom:
// 1. Reviewing — other people's PRs you pulled down locally.
// 2. Merged — easiest to clear (delete worktree, done).
// 3. Ready to merge — one merge click and it's gone.
// 4. Needs your action — real code work.
// 5. In progress — wait state.
// 6. No PR yet — branch hasn't been pushed.
const TIER_ORDER: Tier[] = [
  "reviewing",
  "merged",
  "ready_to_merge",
  "needs_action",
  "in_progress",
  "no_pr",
];

const TIER_LABEL: Record<Tier, string> = {
  reviewing: "Reviewing",
  merged: "Merged",
  needs_action: "Needs your action",
  ready_to_merge: "Ready to merge",
  in_progress: "In progress",
  no_pr: "No PR yet",
};

const TIER_EMPTY_COPY: Record<Tier, string> = {
  reviewing: "no PRs being reviewed locally",
  merged: "no worktrees in this tier",
  needs_action: "no worktrees in this tier",
  ready_to_merge: "no worktrees in this tier",
  in_progress: "no worktrees in this tier",
  no_pr: "no worktrees in this tier",
};

// Per-label chip styling. Each chip is rendered inline on its row;
// the color family carries over from what used to be the headline-group
// container so the visual language stays consistent. Label text is
// short (lower-case) to fit inline next to the workspace name.
// ``tooltip`` is rendered on hover (~150ms delay via the Tooltip
// component) and answers "what does this chip mean?" — useful when
// chip text is ambiguous (`review` vs `unaddressed_comments`) or
// terse (`queued`, `waiting`).
const LABEL_CHIP_STYLE: Record<
  PrHeadline,
  { label: string; chip: string; tooltip: string }
> = {
  ci_failing: {
    label: "ci fail",
    chip: "border-red-800 bg-red-900/40 text-red-300",
    tooltip: "At least one CI check failed. Open the PR to see which.",
  },
  merge_conflicts: {
    label: "conflict",
    chip: "border-red-800 bg-red-900/40 text-red-300",
    tooltip:
      "The branch has merge conflicts against its base. Resolve before this can merge.",
  },
  unresolved_comments: {
    label: "unaddressed_comments",
    chip: "border-amber-800 bg-amber-900/40 text-amber-300",
    tooltip:
      "Per-line review threads are open on this PR (not resolved, not outdated by a force-push).",
  },
  human_comment: {
    label: "review",
    chip: "border-amber-800 bg-amber-900/40 text-amber-300",
    tooltip:
      "A human commented on the PR's Conversation tab and the PR isn't approved yet.",
  },
  review_requested: {
    label: "re-rev",
    chip: "border-amber-800 bg-amber-900/40 text-amber-300",
    tooltip:
      "Reviewer was re-requested. (Placeholder — currently behaves like `review`.)",
  },
  merged: {
    label: "merged",
    chip: "border-purple-800 bg-purple-900/40 text-purple-300",
    tooltip:
      "GitHub PR is merged. Cleanup task — delete the worktree + prune the branch.",
  },
  closed: {
    label: "closed",
    chip: "border-zinc-700 bg-zinc-800 text-zinc-400",
    tooltip:
      "GitHub PR was closed without being merged. Often abandoned work; investigate before cleaning up.",
  },
  ready_to_merge: {
    label: "Approved - Ready to Merge",
    chip: "border-emerald-800 bg-emerald-900/40 text-emerald-300",
    tooltip: "Approved and CI is green. One merge click and it's done.",
  },
  in_merge_queue: {
    label: "queued",
    chip: "border-indigo-800 bg-indigo-900/40 text-indigo-300",
    tooltip:
      "Reserved — currently never emitted; GitHub merge queue isn't exposed by `gh pr view`.",
  },
  checks_running: {
    label: "checks",
    chip: "border-amber-800 bg-amber-900/40 text-amber-300",
    tooltip: "Status checks are still running. Nothing to do yet.",
  },
  waiting_on_others: {
    label: "waiting",
    chip: "border-zinc-700 bg-zinc-800 text-zinc-400",
    tooltip:
      "PR exists but no other label applies. Usually waiting on reviewer action.",
  },
  draft: {
    label: "draft",
    chip: "border-zinc-700 bg-zinc-800 text-zinc-400",
    tooltip: "PR is marked as a draft. Not ready for review yet.",
  },
  no_pr: {
    label: "no PR",
    chip: "border-zinc-700 bg-zinc-800/60 text-zinc-500",
    tooltip: "No PR exists for this branch. Push and open one.",
  },
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

function isReviewerOwned(w: Worktree, userLogin: string | null): boolean {
  // The REVIEWING tier triggers only when we know BOTH logins AND
  // they disagree. Unknown user_login (gh missing) and unknown
  // pr_author_login (never captured) both fall through to state-tier
  // — matches the pre-REVIEWING behavior and avoids surprising
  // legacy rows.
  return (
    userLogin != null &&
    w.pr_author_login != null &&
    w.pr_author_login !== userLogin
  );
}

function tierForWorktree(w: Worktree, userLogin: string | null): Tier {
  // Ownership trumps state: a reviewer-owned PR sorts into REVIEWING
  // even if its labels would otherwise put it in MERGED / NEEDS
  // ACTION / etc. The state chips still render on the row, so the
  // tier-vs-label semantics are: tier = whose work, chip = what
  // state.
  if (isReviewerOwned(w, userLogin)) {
    return "reviewing";
  }
  return TIER_FOR_HEADLINE[labelsForWorktree(w)[0]];
}

// Within-tier sort: approval-ready rows lead, alphabetical as the
// fallback. A workspace whose labels include ``ready_to_merge`` is
// "one button away from done" — even if it's currently bucketed
// into Needs-your-action because of an open ``unresolved_comments``
// or ``review_requested`` blocker, it's still strictly closer to
// merge than a row with only the blocker. Promoting approved rows
// to the top of the tier surfaces the cheapest wins first.
function compareWithinTier(a: Worktree, b: Worktree): number {
  const aReady = labelsForWorktree(a).includes("ready_to_merge");
  const bReady = labelsForWorktree(b).includes("ready_to_merge");
  if (aReady !== bReady) return aReady ? -1 : 1;
  if (a.repo !== b.repo) return a.repo < b.repo ? -1 : 1;
  return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
}

function groupByTier(
  worktrees: Worktree[],
  userLogin: string | null,
): Record<Tier, Worktree[]> {
  const out: Record<Tier, Worktree[]> = {
    reviewing: [],
    merged: [],
    ready_to_merge: [],
    needs_action: [],
    in_progress: [],
    no_pr: [],
  };
  for (const w of worktrees) {
    out[tierForWorktree(w, userLogin)].push(w);
  }
  for (const tier of TIER_ORDER) {
    out[tier].sort(compareWithinTier);
  }
  return out;
}

export function WorkspaceList({ worktrees, jira, userLogin }: Props) {
  if (worktrees.length === 0) return null;

  const grouped = groupByTier(worktrees, userLogin);

  return (
    <div className="space-y-4">
      {TIER_ORDER.map((tier) => {
        const rows = grouped[tier];
        const isEmpty = rows.length === 0;
        const isReviewing = tier === "reviewing";
        return (
          <section key={tier} className={isEmpty ? "opacity-50" : undefined}>
            <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-500">
              {TIER_LABEL[tier]}
              <span className="ml-2 text-zinc-600">· {rows.length}</span>
            </h3>
            {isEmpty ? (
              <p className="rounded-lg border border-dashed border-zinc-800 px-4 py-3 text-xs italic text-zinc-600">
                {TIER_EMPTY_COPY[tier]}
              </p>
            ) : (
              <ul className="space-y-2">
                {rows.map((w) => (
                  <WorkspaceRow
                    key={`${w.repo}/${w.name}`}
                    w={w}
                    jira={jira}
                    isReviewing={isReviewing}
                  />
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
  // True when the row is rendered inside the REVIEWING tier — drives
  // the `@author` chip. Passed down rather than recomputed so the
  // row doesn't need to know about userLogin or ownership rules.
  isReviewing: boolean;
}

function WorkspaceRow({ w, jira, isReviewing }: RowProps) {
  const labels = labelsForWorktree(w);
  const showAuthor = isReviewing && w.pr_author_login;
  return (
    <li className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
      <div className="flex items-start justify-between gap-4">
          <div className="flex min-w-0 items-baseline gap-2">
            <Link
              to="/workspace/$repo/$name"
              params={{ repo: w.repo, name: w.name }}
              className="min-w-0 truncate font-medium text-zinc-100 hover:text-indigo-300"
            >
              {w.name}
            </Link>
            {showAuthor && (
              <Tooltip
                text={`PR opened by @${w.pr_author_login} — you're reviewing it locally.`}
              >
                <span className="shrink-0 rounded border border-indigo-800 bg-indigo-900/30 px-1.5 py-0.5 text-[10px] text-indigo-300">
                  @{w.pr_author_login}
                </span>
              </Tooltip>
            )}
          </div>
          <div className="flex flex-wrap items-center justify-end gap-x-2 gap-y-1">
            {w.status === "code_on_disk" && (
              <Tooltip text="Worktree was created, but a setup_step (`make install`, etc.) errored. The code is on disk — you can open it in iTerm2 / Cursor and re-run the failing step. The setup log on the Manage page has details.">
                <span className="rounded border border-amber-800 bg-amber-900/40 px-1.5 py-0.5 text-[10px] text-amber-300">
                  setup incomplete
                </span>
              </Tooltip>
            )}
            {labels.map((label) => {
              const style = LABEL_CHIP_STYLE[label];
              return (
                <Tooltip key={label} text={style.tooltip}>
                  <span
                    className={`rounded border px-1.5 py-0.5 text-[10px] ${style.chip}`}
                  >
                    {style.label}
                  </span>
                </Tooltip>
              );
            })}
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
            <WorkspaceActionButton
              repo={w.repo}
              name={w.name}
              status={w.status}
              hasClaudeSession={w.has_claude_session}
            />
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
        <div className="mt-3">
          <WorkspaceNotes
            repo={w.repo}
            name={w.name}
            notes={w.notes}
            variant="compact"
          />
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

interface WorkspaceActionButtonProps {
  repo: string;
  name: string;
  status: WorktreeStatus;
  hasClaudeSession: boolean;
}

// State-aware primary action for a workspace row. Replaces the prior
// (status chip + claude chip + iTerm2 button) trio with a single
// button whose label + behavior reflect (status, hasClaudeSession):
//
//   ready + claude session  → Focus iTerm2 (focus-iterm endpoint)
//   ready + no claude       → iTerm2 (spawn-iterm endpoint, unchanged)
//   setting_up              → Configuring… (disabled)
//   failed                  → Setup failed (link to Manage page)
//   stale                   → Recreate workspace (recreate endpoint)
//
// Inline error + red ✗ label apply only to the three mutation states
// (Focus / iTerm2 / Recreate). Disabled and Link states can't fail.
function WorkspaceActionButton({
  repo,
  name,
  status,
  hasClaudeSession,
}: WorkspaceActionButtonProps) {
  const queryClient = useQueryClient();

  const spawnMutation = useMutation({
    mutationFn: () => spawnIterm(repo, name),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["worktrees"] });
    },
  });

  const focusMutation = useMutation({
    mutationFn: () => focusIterm(repo, name),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["worktrees"] });
    },
  });

  const recreateMutation = useMutation({
    mutationFn: () => recreateWorktree(repo, name),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["worktrees"] });
    },
  });

  // failed/setting_up route to navigation/disabled — no mutation.
  if (status === "failed") {
    return (
      <Tooltip text="Setup didn't complete. Click to view the setup log on the Manage page.">
        <Link
          to="/workspace/$repo/$name"
          params={{ repo, name }}
          className="rounded border border-red-700 bg-red-950/40 px-3 py-1 text-xs text-red-300 hover:bg-red-900/40"
        >
          Setup failed
        </Link>
      </Tooltip>
    );
  }

  if (status === "setting_up") {
    return (
      <Tooltip text="Worktree setup is in progress (git worktree add + setup_steps). Check Manage for the live log.">
        <button
          type="button"
          disabled
          className="rounded border border-amber-800 bg-amber-950/40 px-3 py-1 text-xs text-amber-300 disabled:cursor-not-allowed disabled:opacity-70"
        >
          Configuring…
        </button>
      </Tooltip>
    );
  }

  if (status === "stale") {
    const errorDetail = mutationError(recreateMutation.error);
    const tooltip = errorDetail
      ? errorDetail
      : "The on-disk directory is gone. Click to re-run git worktree add + setup_steps[] against the same branch.";
    return (
      <ButtonWithError
        tooltip={tooltip}
        errorDetail={errorDetail}
        onClick={() => recreateMutation.mutate()}
        pending={recreateMutation.isPending}
        pendingLabel="Recreating…"
        idleLabel="Recreate workspace"
      />
    );
  }

  // status === "ready" or "code_on_disk" past here. Both are
  // usable for the action-button purpose (code is on disk;
  // iTerm2 / focus works). The `code_on_disk` distinction is
  // surfaced separately via an amber chip next to the workspace
  // name, not by disabling the action button.
  if (hasClaudeSession) {
    const errorDetail = mutationError(focusMutation.error);
    const tooltip = errorDetail
      ? errorDetail
      : "Bring this worktree's open Claude session in iTerm2 to the front.";
    return (
      <ButtonWithError
        tooltip={tooltip}
        errorDetail={errorDetail}
        onClick={() => focusMutation.mutate()}
        pending={focusMutation.isPending}
        pendingLabel="Focusing…"
        idleLabel="Focus iTerm2"
      />
    );
  }

  // status === "ready" / "code_on_disk" + no claude session.
  const errorDetail = mutationError(spawnMutation.error);
  const tooltip = errorDetail
    ? errorDetail
    : "Open this workspace in a new iTerm2 window (multiple windows are fine).";
  return (
    <ButtonWithError
      tooltip={tooltip}
      errorDetail={errorDetail}
      onClick={() => spawnMutation.mutate()}
      pending={spawnMutation.isPending}
      pendingLabel="Opening…"
      idleLabel="iTerm2"
    />
  );
}

function mutationError(err: unknown): string | null {
  if (!err) return null;
  return err instanceof ApiError ? err.detail : String(err);
}

interface ButtonWithErrorProps {
  tooltip: string;
  errorDetail: string | null;
  onClick: () => void;
  pending: boolean;
  pendingLabel: string;
  idleLabel: string;
}

function ButtonWithError({
  tooltip,
  errorDetail,
  onClick,
  pending,
  pendingLabel,
  idleLabel,
}: ButtonWithErrorProps) {
  return (
    <div className="flex flex-col items-end gap-1">
      <Tooltip text={tooltip}>
        <button
          type="button"
          onClick={onClick}
          disabled={pending}
          className={`rounded border px-3 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-50 ${
            errorDetail
              ? "border-red-700 bg-red-950/40 text-red-300 hover:bg-red-900/40"
              : "border-zinc-700 bg-zinc-800 text-zinc-200 hover:bg-zinc-700"
          }`}
        >
          {pending ? pendingLabel : errorDetail ? `${idleLabel} ✗` : idleLabel}
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
