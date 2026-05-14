import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { configureAndPullDown, getInbox, pullDownPr } from "../api/inbox";
import type { InboxCiStatus, InboxPr } from "../api/types";
import { Tooltip } from "./Tooltip";

const CI_STYLE: Record<InboxCiStatus, { label: string; cls: string }> = {
  pass: { label: "ci ✓", cls: "border-emerald-800 bg-emerald-900/40 text-emerald-300" },
  fail: { label: "ci ✗", cls: "border-red-800 bg-red-900/40 text-red-300" },
  pending: { label: "ci ⋯", cls: "border-amber-800 bg-amber-900/40 text-amber-300" },
  none: { label: "no ci", cls: "border-zinc-700 bg-zinc-800 text-zinc-400" },
};

// Map source tag → short chip label. "author" gets "me" (matches the
// authored subsection header so the chip stays informative; per-row
// chips otherwise just duplicate the section). "reviewer" stays "me"
// since the chip's job is "why is this here for me" — and "me" answers
// that whether direct or team-routed reviews land alongside.
function sourceChipLabel(source: string): string {
  if (source === "author" || source === "reviewer") return "me";
  if (source.startsWith("team:")) {
    const slug = source.slice(5);
    // Show just the team half — e.g. "acme/corrections" → "corrections"
    const parts = slug.split("/", 2);
    return parts.length === 2 ? parts[1] : slug;
  }
  return source;
}

// graphite.com renders the whole stack regardless of which PR in the
// stack the URL points at; we pick the top of stack since it's the
// "current" / "newest" PR.
function graphiteStackUrl(pr: InboxPr): string {
  const top = pr.stack_top_pr_number ?? pr.pr_number;
  return `https://app.graphite.com/github/pr/${pr.pr_repo}/${top}`;
}

interface Props {
  // Render-only for testing.
  inboxOverride?: { prs: InboxPr[]; checked_at: string | null };
}

export function InboxList({ inboxOverride }: Props = {}) {
  const inboxQuery = useQuery({
    queryKey: ["inbox"],
    queryFn: getInbox,
    refetchInterval: 30_000,
    enabled: inboxOverride === undefined,
  });

  const data = inboxOverride ?? inboxQuery.data;

  if (!data) {
    // First load, no cached payload yet — render nothing rather than a
    // jumpy spinner. The poll cadence is 60s on the backend; this
    // resolves quickly enough that an empty placeholder is fine.
    return null;
  }

  const prs = data.prs;
  if (prs.length === 0) return null;

  const authored = prs.filter((p) => p.source === "author");
  const reviewer = prs.filter((p) => p.source !== "author");

  return (
    <section>
      <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
        Inbox
        <span className="ml-2 text-zinc-600">· {prs.length}</span>
      </h2>
      <div className="mt-3 space-y-6">
        {authored.length > 0 && (
          <Subsection label="You authored" prs={authored} />
        )}
        {reviewer.length > 0 && (
          <Subsection label="Reviewer" prs={reviewer} />
        )}
      </div>
    </section>
  );
}

interface SubsectionProps {
  label: string;
  prs: InboxPr[];
}

function Subsection({ label, prs }: SubsectionProps) {
  // Group consecutive stack members so the box renders once per stack.
  // Stack identity = (pr_repo, stack_top_pr_number). Single PRs (no
  // stack) have stack_top_pr_number = null → each gets its own
  // "group of 1" and renders as a plain row.
  const groups = groupByStack(prs);

  return (
    <div>
      <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-500">
        [{label.toUpperCase()}]
      </h3>
      <div className="space-y-3">
        {groups.map((group) =>
          group.isStack ? (
            <StackGroup key={group.key} prs={group.prs} />
          ) : (
            <PrRow key={group.key} pr={group.prs[0]} />
          ),
        )}
      </div>
    </div>
  );
}

interface PrGroup {
  key: string;
  isStack: boolean;
  prs: InboxPr[];
}

function groupByStack(prs: InboxPr[]): PrGroup[] {
  const stacks = new Map<string, InboxPr[]>();
  const singles: InboxPr[] = [];

  for (const pr of prs) {
    if (pr.stack_top_pr_number !== null && pr.stack_size > 1) {
      const k = `${pr.pr_repo}#${pr.stack_top_pr_number}`;
      if (!stacks.has(k)) stacks.set(k, []);
      stacks.get(k)!.push(pr);
    } else {
      singles.push(pr);
    }
  }

  const groups: PrGroup[] = [];
  // Stacks first (newest stack-top by updated_at across members), then
  // singles by updated_at desc.
  const stackEntries = Array.from(stacks.entries()).sort(([, a], [, b]) => {
    const aLatest = a.reduce((m, p) => (p.updated_at > m ? p.updated_at : m), "");
    const bLatest = b.reduce((m, p) => (p.updated_at > m ? p.updated_at : m), "");
    return bLatest.localeCompare(aLatest);
  });

  for (const [key, members] of stackEntries) {
    // Display order: bottom (closest to main) first inside the box,
    // matching the spec where the top of stack reads at the top
    // visually. stack_position 1 = bottom; sort DESC so the top
    // (highest stack_position) renders first.
    const sorted = [...members].sort((a, b) => b.stack_position - a.stack_position);
    groups.push({ key, isStack: true, prs: sorted });
  }

  singles.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  for (const pr of singles) {
    groups.push({ key: `${pr.pr_repo}#${pr.pr_number}`, isStack: false, prs: [pr] });
  }

  return groups;
}

function StackGroup({ prs }: { prs: InboxPr[] }) {
  const top = prs[0]; // highest stack_position by the sort above
  const graphiteUrl = graphiteStackUrl(top);
  return (
    <div className="relative rounded-md border border-zinc-700 bg-zinc-900/40 px-3 pb-2 pt-5">
      <a
        href={graphiteUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="absolute -top-3 left-3 rounded border border-zinc-700 bg-zinc-950 px-2 py-0.5 text-[11px] font-medium text-zinc-300 hover:text-indigo-300"
      >
        ↗ Graphite · {prs.length}-PR stack
      </a>
      <ul className="space-y-1">
        {prs.map((pr) => (
          <li key={`${pr.pr_repo}#${pr.pr_number}`}>
            <PrRow pr={pr} inStack />
          </li>
        ))}
      </ul>
    </div>
  );
}

interface PrRowProps {
  pr: InboxPr;
  inStack?: boolean;
}

function PrRow({ pr, inStack = false }: PrRowProps) {
  const ci = CI_STYLE[pr.ci_status];
  return (
    <div
      className={
        inStack
          ? "flex items-center gap-3 py-1"
          : "flex items-center gap-3 rounded-md border border-zinc-800 bg-zinc-900/40 px-3 py-2"
      }
    >
      <a
        href={pr.url}
        target="_blank"
        rel="noopener noreferrer"
        className="min-w-0 flex-1 truncate text-sm text-zinc-100 hover:text-indigo-300"
        title={pr.title}
      >
        {pr.title}
      </a>
      <span className="shrink-0 font-mono text-xs text-zinc-500">
        #{pr.pr_number}
      </span>
      <span
        className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] ${ci.cls}`}
      >
        {ci.label}
      </span>
      <span className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
        {sourceChipLabel(pr.source)}
      </span>
      <PullDownButton pr={pr} />
    </div>
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
      // The new worktree's pr_number/pr_repo dedup hides this row on
      // the next poll, but invalidate both queries now for snappy UX.
      queryClient.invalidateQueries({ queryKey: ["worktrees"] });
      queryClient.invalidateQueries({ queryKey: ["inbox"] });
    },
  });

  const configureMutation = useMutation({
    mutationFn: () => configureAndPullDown(pr.pr_repo, pr.pr_number),
    // Note: success here means iTerm2 has been spawned and an onboard
    // session minted. The worktree appears later, after Claude POSTs
    // the proposed_entry back and the auto-fired pull-down runs. The
    // inbox + worktrees queries refetch on their poll cadence; no
    // optimistic invalidation here.
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
      : "Fetch this PR's branch and create a local worktree";

  return (
    <Tooltip text={tooltip}>
      <button
        type="button"
        onClick={() => mutation.mutate()}
        disabled={disabled}
        className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-2.5 py-0.5 text-[11px] text-zinc-200 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {label}
      </button>
    </Tooltip>
  );
}
