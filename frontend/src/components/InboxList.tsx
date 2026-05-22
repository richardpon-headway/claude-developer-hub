import { useQuery } from "@tanstack/react-query";

import { getInbox } from "../api/inbox";
import type { InboxPr, JiraConfig } from "../api/types";
import { PrCard, useBookmarkedKeys } from "./PrCard";

interface Props {
  jira: JiraConfig | null;
  // Render-only for testing.
  inboxOverride?: { prs: InboxPr[] };
}

export function InboxList({ jira, inboxOverride }: Props) {
  const inboxQuery = useQuery({
    queryKey: ["inbox"],
    queryFn: getInbox,
    refetchInterval: 30_000,
    enabled: inboxOverride === undefined,
  });

  const data = inboxOverride ?? inboxQuery.data;
  const bookmarked = useBookmarkedKeys();

  if (!data) {
    return null;
  }

  const prs = data.prs;

  return (
    <section>
      <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
        Inbox
        <span className="ml-2 text-zinc-600">· {prs.length}</span>
      </h2>
      {prs.length === 0 ? (
        <div className="mt-3 rounded-lg border border-dashed border-zinc-700 p-6 text-center">
          <p className="text-sm text-zinc-400">
            No PRs need your attention.
          </p>
          <p className="mt-1 text-xs text-zinc-500">
            PRs where you're directly review-requested, assigned, or
            @-mentioned (and that don't already have a local worktree)
            appear here. Reviewed PRs stay until they close, merge, or
            you remove them.
          </p>
        </div>
      ) : (
        <ul className="mt-3 space-y-2">
          {prs.map((pr) => (
            <PrCard
              key={`${pr.pr_repo}#${pr.pr_number}`}
              data={{ kind: "inbox", row: pr }}
              jira={jira}
              bookmarked={bookmarked}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
