// Lifecycle-tier helpers for the unified hub. These operate on a
// `pr_state` (from any workspace entity) so both buckets — My Work and
// Reviewing — group rows the same way. The old "reviewing" tier is gone:
// reviewing is now a bucket (by authorship), not a tier.
import type { PrHeadline, PrStateSummary } from "../api/types";

export type Tier =
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

export const TIER_ORDER: Tier[] = [
  "needs_action",
  "ready_to_merge",
  "in_progress",
  "merged",
  "no_pr",
];

export const TIER_LABEL: Record<Tier, string> = {
  needs_action: "Needs your action",
  ready_to_merge: "Ready to merge",
  in_progress: "In progress",
  merged: "Merged",
  no_pr: "No PR yet",
};

// The chip labels for a row: the rich multi-label set when pr_state is
// present, else a single fallback. Callers needing the scalar fallback
// (no pr_state yet) handle that separately — this returns labels only
// when pr_state exists.
export function labelsFor(prState: PrStateSummary | null): PrHeadline[] {
  if (prState?.labels && prState.labels.length > 0) {
    return prState.labels;
  }
  if (prState?.headline) {
    return [prState.headline];
  }
  return ["no_pr"];
}

export function tierFor(prState: PrStateSummary | null): Tier {
  return TIER_FOR_HEADLINE[labelsFor(prState)[0]];
}
