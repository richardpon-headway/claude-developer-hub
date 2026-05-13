import { useState } from "react";
import * as RadixPopover from "@radix-ui/react-popover";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { refreshPrState } from "../api/worktrees";
import type { PrHeadline, PrStateSummary } from "../api/types";

interface Props {
  repo: string;
  name: string;
  state: PrStateSummary;
  /**
   * - "inline" (default): small pill, sits among other badges in a
   *   horizontal cluster (e.g., next to the worktree status pill).
   * - "tall": full-height vertical bar designed to sit alongside the
   *   workspace card, matching its height via flex items-stretch.
   */
  variant?: "inline" | "tall";
}

interface HeadlineStyle {
  label: string;
  className: string;
}

const HEADLINE_STYLE: Record<Exclude<PrHeadline, "no_pr">, HeadlineStyle> = {
  merged: {
    label: "PR merged",
    // GitHub's own merged color is purple; mirror that so the state is
    // unambiguous next to the green "ready_to_merge".
    className: "bg-purple-900/40 text-purple-300 border-purple-800",
  },
  closed: {
    label: "PR closed",
    className: "bg-zinc-800 text-zinc-500 border-zinc-700",
  },
  ci_failing: {
    label: "PR CI fail",
    className: "bg-red-900/40 text-red-300 border-red-800",
  },
  merge_conflicts: {
    label: "PR conflict",
    className: "bg-red-900/40 text-red-300 border-red-800",
  },
  in_merge_queue: {
    label: "PR queued",
    className: "bg-indigo-900/40 text-indigo-300 border-indigo-800",
  },
  ready_to_merge: {
    label: "PR ready",
    className: "bg-emerald-900/40 text-emerald-300 border-emerald-800",
  },
  human_comment: {
    label: "PR review",
    className: "bg-amber-900/40 text-amber-300 border-amber-800",
  },
  review_requested: {
    label: "PR re-rev",
    className: "bg-amber-900/40 text-amber-300 border-amber-800",
  },
  checks_running: {
    label: "PR checks",
    className: "bg-amber-900/40 text-amber-300 border-amber-800",
  },
  waiting_on_others: {
    label: "PR waiting",
    className: "bg-zinc-800 text-zinc-400 border-zinc-700",
  },
  draft: {
    label: "PR draft",
    className: "bg-zinc-800 text-zinc-400 border-zinc-700",
  },
};

export function PrStateBadge({ repo, name, state, variant = "inline" }: Props) {
  if (state.headline === "no_pr") return null;

  const style = HEADLINE_STYLE[state.headline];

  const triggerClass =
    variant === "tall"
      ? // Full-height bar to the right of the workspace card.
        // min-w-28 (= 7rem ≈ 112px) is wide enough for the longest
        // label ("PR conflict") so every bar's right edge lines up.
        // Pairs with `min-w-0` on the card so the row doesn't push
        // past the column boundary on narrower viewports.
        `flex min-w-28 items-center justify-center whitespace-nowrap rounded-lg border px-3 text-xs font-medium hover:brightness-125 ${style.className}`
      : `rounded border px-1.5 py-0.5 text-[10px] hover:brightness-125 ${style.className}`;

  return (
    <RadixPopover.Root>
      <RadixPopover.Trigger asChild>
        <button type="button" className={triggerClass}>
          {style.label}
        </button>
      </RadixPopover.Trigger>
      <RadixPopover.Portal>
        <RadixPopover.Content
          side="bottom"
          align="end"
          sideOffset={4}
          collisionPadding={8}
          className="z-50 w-80 rounded border border-zinc-700 bg-zinc-900 p-3 text-xs text-zinc-200 shadow-lg"
        >
          <PrStateDetail repo={repo} name={name} state={state} />
          <RadixPopover.Arrow className="fill-zinc-700" />
        </RadixPopover.Content>
      </RadixPopover.Portal>
    </RadixPopover.Root>
  );
}

interface DetailProps {
  repo: string;
  name: string;
  state: PrStateSummary;
}

function PrStateDetail({ repo, name, state: initial }: DetailProps) {
  const queryClient = useQueryClient();
  // Keep a local copy so "Refresh now" can replace the rendered state
  // without waiting for the next /api/worktrees poll to land.
  const [state, setState] = useState(initial);

  const refresh = useMutation({
    mutationFn: () => refreshPrState(repo, name),
    onSuccess: (fresh) => {
      setState(fresh);
      queryClient.invalidateQueries({ queryKey: ["worktrees"] });
    },
  });

  return (
    <div className="space-y-2">
      <div className="font-medium text-zinc-100">
        {state.pr_number != null ? `PR #${state.pr_number}` : "PR"}
        {state.title ? ` — ${state.title}` : ""}
      </div>

      <div className="text-zinc-400">
        {checksLine(state)} {dotSeparator()} {reviewLine(state)}
      </div>

      <div className="text-zinc-400">
        {state.comments.human} human {dotSeparator()} {state.comments.bot} bot comments
      </div>

      <div className="text-zinc-400">
        {mergeableLine(state)}
        {state.base_ref && state.head_ref
          ? ` — ${state.base_ref} ← ${state.head_ref}`
          : ""}
      </div>

      <div className="text-zinc-500">
        updated {relativeTime(state.updated_at)}
        {" "} {dotSeparator()} {" "}
        checked {relativeTime(state.checked_at)}
      </div>

      <div className="flex gap-2 pt-1">
        <button
          type="button"
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          className="rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-700 disabled:opacity-50"
        >
          {refresh.isPending ? "Refreshing…" : "Refresh now"}
        </button>
        {state.url && (
          <a
            href={state.url}
            target="_blank"
            rel="noopener noreferrer"
            className="rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-700"
          >
            Open on GitHub
          </a>
        )}
      </div>

      {refresh.error && (
        <p role="alert" className="text-red-400">
          refresh failed: {String(refresh.error)}
        </p>
      )}
    </div>
  );
}

function dotSeparator() {
  return <span className="mx-1 text-zinc-600">·</span>;
}

function checksLine(state: PrStateSummary): string {
  const { passed, fail, pending, total } = state.checks;
  if (total === 0) return "no checks";
  if (fail > 0) return `${fail} failing / ${total}`;
  if (pending > 0) return `${pending} pending / ${total}`;
  return `${passed}/${total} ✓`;
}

function reviewLine(state: PrStateSummary): string {
  switch (state.review_decision) {
    case "APPROVED":
      return "approved";
    case "CHANGES_REQUESTED":
      return "changes requested";
    case "REVIEW_REQUIRED":
      return "review required";
    default:
      return "no review yet";
  }
}

function mergeableLine(state: PrStateSummary): string {
  const m = (state.mergeable || "").toUpperCase();
  if (m === "MERGEABLE") return "mergeable";
  if (m === "CONFLICTING") return "conflicting";
  if (m) return m.toLowerCase();
  return "merge state unknown";
}

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const diffSec = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.round(diffHr / 24);
  return `${diffDay}d ago`;
}
