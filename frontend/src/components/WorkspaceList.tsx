import type { JiraConfig, PrHeadline, Worktree } from "../api/types";
import { PrCard, useBookmarkedKeys } from "./PrCard";

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

function labelsForWorktree(w: Worktree): PrHeadline[] {
  if (w.pr_state?.labels && w.pr_state.labels.length > 0) {
    return w.pr_state.labels;
  }
  if (w.pr_state?.headline) {
    return [w.pr_state.headline];
  }
  return ["no_pr"];
}

function isReviewerOwned(w: Worktree, userLogin: string | null): boolean {
  return (
    userLogin != null &&
    w.pr_author_login != null &&
    w.pr_author_login !== userLogin
  );
}

function tierForWorktree(w: Worktree, userLogin: string | null): Tier {
  if (isReviewerOwned(w, userLogin)) {
    return "reviewing";
  }
  return TIER_FOR_HEADLINE[labelsForWorktree(w)[0]];
}

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
  const bookmarked = useBookmarkedKeys();
  if (worktrees.length === 0) return null;

  const grouped = groupByTier(worktrees, userLogin);

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
                {TIER_EMPTY_COPY[tier]}
              </p>
            ) : (
              <ul className="space-y-2">
                {rows.map((w) => (
                  <PrCard
                    key={`${w.repo}/${w.name}`}
                    data={{ kind: "worktree", row: w, userLogin }}
                    jira={jira}
                    bookmarked={bookmarked}
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
