import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import {
  archiveInboxPr,
  configureAndPullDown,
  getInbox,
  pullDownPr,
} from "../api/inbox";
import type { InboxCiStatus, InboxPr, JiraConfig } from "../api/types";
import { InboxNotes } from "./InboxNotes";
import { Tooltip } from "./Tooltip";

const CI_STYLE: Record<InboxCiStatus, { label: string; cls: string }> = {
  pass: { label: "ci ✓", cls: "border-emerald-800 bg-emerald-900/40 text-emerald-300" },
  fail: { label: "ci ✗", cls: "border-red-800 bg-red-900/40 text-red-300" },
  pending: { label: "ci ⋯", cls: "border-amber-800 bg-amber-900/40 text-amber-300" },
  none: { label: "no ci", cls: "border-zinc-700 bg-zinc-800 text-zinc-400" },
};

function sourceChipLabel(source: string): string {
  if (source === "reviewer") return "reviewer";
  if (source === "assignee") return "assignee";
  if (source === "mentions") return "mention";
  return source;
}

function sourceChipTooltip(source: string): string {
  if (source === "reviewer") {
    return (
      "You were directly added as a reviewer (team-mediated review " +
      "requests are post-filtered out)."
    );
  }
  if (source === "assignee") return "You're an assignee on this PR.";
  if (source === "mentions") {
    return "The PR body or comments mention `@you` directly.";
  }
  return source;
}

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
            you archive them.
          </p>
        </div>
      ) : (
        <ul className="mt-3 space-y-2">
          {prs.map((pr) => (
            <PrRow
              key={`${pr.pr_repo}#${pr.pr_number}`}
              pr={pr}
              jira={jira}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

interface RowProps {
  pr: InboxPr;
  jira: JiraConfig | null;
}

function PrRow({ pr, jira }: RowProps) {
  const ci = CI_STYLE[pr.ci_status];
  return (
    <li className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-baseline gap-2">
          <a
            href={pr.url}
            target="_blank"
            rel="noopener noreferrer"
            className="min-w-0 truncate font-medium text-zinc-100 hover:text-indigo-300"
            title={pr.title}
          >
            {pr.title}
          </a>
          <span className="shrink-0 font-mono text-xs text-zinc-500">
            #{pr.pr_number}
          </span>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-x-2 gap-y-1">
          <span
            className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] ${ci.cls}`}
          >
            {ci.label}
          </span>
          {pr.sources.map((source) => (
            <Tooltip key={source} text={sourceChipTooltip(source)}>
              <span className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
                {sourceChipLabel(source)}
              </span>
            </Tooltip>
          ))}
        </div>
      </div>
      <div className="mt-2 flex items-end justify-between gap-4">
        <div className="min-w-0 flex-1 space-y-0.5 text-xs text-zinc-500">
          <div>
            @{pr.author_login}{" "}
            <span className="text-zinc-600">· {pr.pr_repo}</span>
          </div>
          {pr.ticket && (
            <div>
              ticket: <TicketValue ticket={pr.ticket} jira={jira} />
            </div>
          )}
        </div>
        <div className="flex shrink-0 items-start gap-2">
          <PullDownButton pr={pr} />
          <ArchiveButton pr={pr} />
        </div>
      </div>
      <div className="mt-3">
        <InboxNotes
          prRepo={pr.pr_repo}
          prNumber={pr.pr_number}
          notes={pr.notes}
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

interface PullDownButtonProps {
  pr: InboxPr;
}

function PullDownButton({ pr }: PullDownButtonProps) {
  const queryClient = useQueryClient();

  const pullDownMutation = useMutation({
    mutationFn: () => pullDownPr(pr.pr_repo, pr.pr_number),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["worktrees"] });
      queryClient.invalidateQueries({ queryKey: ["inbox"] });
    },
  });

  const configureMutation = useMutation({
    mutationFn: () => configureAndPullDown(pr.pr_repo, pr.pr_number),
  });

  const isConfigureFlow = !pr.repo_configured;
  const mutation = isConfigureFlow ? configureMutation : pullDownMutation;
  const disabled = mutation.isPending || mutation.isSuccess;

  const label = isConfigureFlow
    ? configureMutation.isPending
      ? "Spawning…"
      : configureMutation.isSuccess
        ? "Claude opened"
        : "Configure repo + pull down"
    : pullDownMutation.isPending
      ? "Pulling…"
      : pullDownMutation.isSuccess
        ? "Pulled"
        : "Pull down";

  const tooltip = mutation.error
    ? mutation.error instanceof ApiError
      ? mutation.error.detail
      : String(mutation.error)
    : isConfigureFlow
      ? `Opens Claude in your development_root to onboard ${pr.pr_repo}, then automatically pulls this PR into a worktree once onboarding completes.`
      : "Fetch this PR's branch and create a local worktree.";

  return (
    <Tooltip text={tooltip}>
      <button
        type="button"
        onClick={() => mutation.mutate()}
        disabled={disabled}
        className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-200 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {label}
      </button>
    </Tooltip>
  );
}

interface ArchiveButtonProps {
  pr: InboxPr;
}

function ArchiveButton({ pr }: ArchiveButtonProps) {
  const queryClient = useQueryClient();
  const archiveMutation = useMutation({
    mutationFn: () => archiveInboxPr(pr.pr_repo, pr.pr_number),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["inbox"] });
    },
  });

  const tooltip = archiveMutation.error
    ? archiveMutation.error instanceof ApiError
      ? archiveMutation.error.detail
      : String(archiveMutation.error)
    : (
      "Remove this PR from the inbox. Sticky — it won't reappear " +
      "even if GitHub keeps including it in search results."
    );

  return (
    <Tooltip text={tooltip}>
      <button
        type="button"
        onClick={() => archiveMutation.mutate()}
        disabled={archiveMutation.isPending}
        className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {archiveMutation.isPending ? "Removing…" : "Remove"}
      </button>
    </Tooltip>
  );
}
