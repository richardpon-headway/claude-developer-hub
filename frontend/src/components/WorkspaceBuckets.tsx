import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { getWorkspaces } from "../api/workspaces";
import type { JiraConfig, WorkspaceEntity } from "../api/types";
import { TIER_LABEL, TIER_ORDER, type Tier, tierFor } from "../lib/tiers";
import { BookmarkIntake } from "./BookmarkIntake";
import { WorkspaceCard } from "./WorkspaceCard";

interface Props {
  jira: JiraConfig | null;
}

// A workspace is "Reviewing" when it's someone else's PR. Null/unknown
// author (a no-PR branch, or a not-yet-enriched row) defaults to mine.
function isReviewing(e: WorkspaceEntity, userLogin: string | null): boolean {
  return (
    userLogin != null &&
    e.author_login != null &&
    e.author_login !== userLogin
  );
}

function sortWithinTier(a: WorkspaceEntity, b: WorkspaceEntity): number {
  // Ready-to-merge floats up; then stable by title.
  const aReady = (a.pr_state?.labels ?? []).includes("ready_to_merge");
  const bReady = (b.pr_state?.labels ?? []).includes("ready_to_merge");
  if (aReady !== bReady) return aReady ? -1 : 1;
  return a.title < b.title ? -1 : a.title > b.title ? 1 : 0;
}

export function WorkspaceBuckets({ jira }: Props) {
  const query = useQuery({
    queryKey: ["workspaces"],
    queryFn: getWorkspaces,
    refetchInterval: 5_000,
  });
  const [bookmarkedOnly, setBookmarkedOnly] = useState(false);

  if (query.isLoading) {
    return <p className="text-sm text-zinc-500">Loading…</p>;
  }
  if (query.isError) {
    return <p className="text-sm text-red-400">Failed to load workspaces.</p>;
  }

  const workspaces = query.data?.workspaces ?? [];
  const userLogin = query.data?.user_login ?? null;

  const mine = workspaces.filter((e) => !isReviewing(e, userLogin));
  let reviewing = workspaces.filter((e) => isReviewing(e, userLogin));
  if (bookmarkedOnly) {
    reviewing = reviewing.filter((e) => e.is_bookmarked);
  }

  return (
    <div className="space-y-8">
      <BookmarkIntake invalidateKeys={[["workspaces"]]} />

      <Bucket title="My Work" entities={mine} jira={jira} userLogin={userLogin} />
      <Bucket
        title="Reviewing"
        entities={reviewing}
        jira={jira}
        userLogin={userLogin}
        headerExtra={
          <label className="flex items-center gap-1 text-xs font-normal normal-case text-zinc-500">
            <input
              type="checkbox"
              checked={bookmarkedOnly}
              onChange={(e) => setBookmarkedOnly(e.target.checked)}
              className="accent-indigo-500"
            />
            bookmarked only
          </label>
        }
      />
    </div>
  );
}

interface BucketProps {
  title: string;
  entities: WorkspaceEntity[];
  jira: JiraConfig | null;
  userLogin: string | null;
  headerExtra?: React.ReactNode;
}

function Bucket({ title, entities, jira, userLogin, headerExtra }: BucketProps) {
  if (entities.length === 0) return null;

  const grouped: Record<Tier, WorkspaceEntity[]> = {
    needs_action: [],
    ready_to_merge: [],
    in_progress: [],
    merged: [],
    no_pr: [],
  };
  for (const e of entities) {
    grouped[tierFor(e.pr_state)].push(e);
  }

  return (
    <section>
      <h2 className="flex items-center justify-between text-sm font-medium uppercase tracking-wide text-zinc-500">
        <span>
          {title}
          <span className="ml-2 text-zinc-600">· {entities.length}</span>
        </span>
        {headerExtra}
      </h2>
      <div className="mt-3 space-y-4">
        {TIER_ORDER.map((tier) => {
          const rows = grouped[tier];
          if (rows.length === 0) return null;
          rows.sort(sortWithinTier);
          return (
            <section key={tier}>
              <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-600">
                {TIER_LABEL[tier]}
                <span className="ml-2 text-zinc-700">· {rows.length}</span>
              </h3>
              <ul className="space-y-2">
                {rows.map((e) => (
                  <WorkspaceCard
                    key={
                      e.worktree
                        ? `wt:${e.worktree.repo}/${e.worktree.name}`
                        : `pr:${e.pr_repo}#${e.pr_number}`
                    }
                    entity={e}
                    jira={jira}
                    userLogin={userLogin}
                  />
                ))}
              </ul>
            </section>
          );
        })}
      </div>
    </section>
  );
}
