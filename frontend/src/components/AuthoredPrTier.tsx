import { useQuery } from "@tanstack/react-query";

import { listAuthoredPrs } from "../api/authored";
import type { AuthoredPr, JiraConfig } from "../api/types";
import { PrCard, useBookmarkedKeys } from "./PrCard";

interface Props {
  jira: JiraConfig | null;
  // Render-only for testing.
  authoredOverride?: AuthoredPr[];
}

export function AuthoredPrTier({ jira, authoredOverride }: Props) {
  const query = useQuery({
    queryKey: ["authored-prs"],
    queryFn: listAuthoredPrs,
    refetchInterval: 60_000,
    enabled: authoredOverride === undefined,
  });

  const rows = authoredOverride ?? query.data?.authored_prs;
  const bookmarked = useBookmarkedKeys();

  if (rows === undefined) {
    return null;
  }

  if (rows.length === 0) {
    // Empty state hidden: clutter on first run vs. signal value of
    // showing the heading. Lean on "if you have authored PRs without
    // a worktree they'd show here" being obvious from context.
    return null;
  }

  return (
    <section>
      <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-500">
        My PRs (no worktree)
        <span className="ml-2 text-zinc-600">· {rows.length}</span>
      </h3>
      <ul className="space-y-2">
        {rows.map((pr) => (
          <PrCard
            key={`${pr.pr_repo}#${pr.pr_number}`}
            data={{ kind: "authored", row: pr }}
            jira={jira}
            bookmarked={bookmarked}
          />
        ))}
      </ul>
    </section>
  );
}
