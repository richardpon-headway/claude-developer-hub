import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { listAuthoredPrs, pullDownAuthoredPr } from "../api/authored";
import { configureAndPullDown } from "../api/inbox";
import type { AuthoredPr, InboxCiStatus, JiraConfig } from "../api/types";
import { OpenPrLinkButton } from "./OpenPrLinkButton";
import { Tooltip } from "./Tooltip";

const CI_STYLE: Record<InboxCiStatus, { label: string; cls: string }> = {
  pass: { label: "ci ✓", cls: "border-emerald-800 bg-emerald-900/40 text-emerald-300" },
  fail: { label: "ci ✗", cls: "border-red-800 bg-red-900/40 text-red-300" },
  pending: { label: "ci ⋯", cls: "border-amber-800 bg-amber-900/40 text-amber-300" },
  none: { label: "no ci", cls: "border-zinc-700 bg-zinc-800 text-zinc-400" },
};

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
          <AuthoredPrRow
            key={`${pr.pr_repo}#${pr.pr_number}`}
            pr={pr}
            jira={jira}
          />
        ))}
      </ul>
    </section>
  );
}

interface RowProps {
  pr: AuthoredPr;
  jira: JiraConfig | null;
}

function AuthoredPrRow({ pr, jira }: RowProps) {
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
          {pr.is_draft && (
            <span className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
              draft
            </span>
          )}
          <span
            className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] ${ci.cls}`}
          >
            {ci.label}
          </span>
        </div>
      </div>
      <div className="mt-2 flex items-end justify-between gap-4">
        <div className="min-w-0 flex-1 space-y-0.5 text-xs text-zinc-500">
          <div className="text-zinc-600">{pr.pr_repo}</div>
          {pr.ticket && (
            <div>
              ticket: <TicketValue ticket={pr.ticket} jira={jira} />
            </div>
          )}
        </div>
        <div className="flex shrink-0 items-start gap-2">
          <OpenPrLinkButton url={pr.url} />
          <PullDownButton pr={pr} />
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

interface PullDownButtonProps {
  pr: AuthoredPr;
}

function PullDownButton({ pr }: PullDownButtonProps) {
  const queryClient = useQueryClient();

  const pullDownMutation = useMutation({
    mutationFn: () => pullDownAuthoredPr(pr.pr_repo, pr.pr_number),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["worktrees"] });
      queryClient.invalidateQueries({ queryKey: ["authored-prs"] });
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
