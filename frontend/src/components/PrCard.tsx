import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { pullDownAuthoredPr } from "../api/authored";
import {
  bookmarkPr,
  deleteBookmark,
  listBookmarks,
  pullDownBookmark,
} from "../api/bookmarks";
import {
  archiveInboxPr,
  configureAndPullDown,
  pullDownPr,
} from "../api/inbox";
import {
  focusIterm,
  getPrUrl,
  recreateWorktree,
  spawnIterm,
} from "../api/worktrees";
import type {
  AuthoredPr,
  BookmarkPr,
  BookmarkState,
  InboxCiStatus,
  InboxPr,
  JiraConfig,
  PrHeadline,
  Worktree,
  WorktreeStatus,
} from "../api/types";
import { AuthoredPrNotes } from "./AuthoredPrNotes";
import { BookmarkNotes } from "./BookmarkNotes";
import { InboxNotes } from "./InboxNotes";
import { Tooltip } from "./Tooltip";
import { WorkspaceNotes } from "./WorkspaceNotes";

// --- discriminated-union input ------------------------------------------

type PrCardData =
  | { kind: "inbox"; row: InboxPr }
  | { kind: "bookmark"; row: BookmarkPr }
  | { kind: "authored"; row: AuthoredPr }
  | { kind: "worktree"; row: Worktree; userLogin: string | null };

interface Props {
  data: PrCardData;
  jira: JiraConfig | null;
  // Set of `${pr_repo}#${pr_number}` keys already bookmarked. Used to
  // hide the cross-surface "Bookmark this" button when the PR is
  // already in the bookmark surface. Always provided by the parent;
  // pass an empty set when no bookmark data is available.
  bookmarked: Set<string>;
}

// --- chip styling -------------------------------------------------------

const CI_STYLE: Record<InboxCiStatus, { label: string; cls: string }> = {
  pass: { label: "ci ✓", cls: "border-emerald-800 bg-emerald-900/40 text-emerald-300" },
  fail: { label: "ci ✗", cls: "border-red-800 bg-red-900/40 text-red-300" },
  pending: { label: "ci ⋯", cls: "border-amber-800 bg-amber-900/40 text-amber-300" },
  none: { label: "no ci", cls: "border-zinc-700 bg-zinc-800 text-zinc-400" },
};

const BOOKMARK_STATE_STYLE: Record<BookmarkState, { label: string; cls: string }> = {
  open: { label: "open", cls: "border-emerald-800 bg-emerald-900/40 text-emerald-300" },
  merged: { label: "merged", cls: "border-purple-800 bg-purple-900/40 text-purple-300" },
  closed: { label: "closed", cls: "border-zinc-700 bg-zinc-800 text-zinc-400" },
};

// Reused from the old WorkspaceList tier-row code.
const LABEL_CHIP_STYLE: Record<
  PrHeadline,
  { label: string; chip: string; tooltip: string }
> = {
  ci_failing: { label: "ci fail", chip: "border-red-800 bg-red-900/40 text-red-300",
    tooltip: "At least one CI check failed. Open the PR to see which." },
  merge_conflicts: { label: "conflict", chip: "border-red-800 bg-red-900/40 text-red-300",
    tooltip: "The branch has merge conflicts against its base." },
  unresolved_comments: { label: "unaddressed_comments", chip: "border-amber-800 bg-amber-900/40 text-amber-300",
    tooltip: "Per-line review threads are open on this PR." },
  human_comment: { label: "review", chip: "border-amber-800 bg-amber-900/40 text-amber-300",
    tooltip: "A human commented on the PR's Conversation tab and the PR isn't approved yet." },
  review_requested: { label: "re-rev", chip: "border-amber-800 bg-amber-900/40 text-amber-300",
    tooltip: "Reviewer was re-requested." },
  merged: { label: "merged", chip: "border-purple-800 bg-purple-900/40 text-purple-300",
    tooltip: "GitHub PR is merged. Cleanup task." },
  closed: { label: "closed", chip: "border-zinc-700 bg-zinc-800 text-zinc-400",
    tooltip: "PR was closed without being merged." },
  ready_to_merge: { label: "Approved - Ready to Merge", chip: "border-emerald-800 bg-emerald-900/40 text-emerald-300",
    tooltip: "Approved and CI is green." },
  in_merge_queue: { label: "queued", chip: "border-indigo-800 bg-indigo-900/40 text-indigo-300",
    tooltip: "GitHub merge queue is processing this PR." },
  checks_running: { label: "checks", chip: "border-amber-800 bg-amber-900/40 text-amber-300",
    tooltip: "Status checks are still running." },
  waiting_on_others: { label: "waiting", chip: "border-zinc-700 bg-zinc-800 text-zinc-400",
    tooltip: "PR exists but no other label applies. Usually waiting on reviewer action." },
  draft: { label: "draft", chip: "border-zinc-700 bg-zinc-800 text-zinc-400",
    tooltip: "PR is marked as a draft. Not ready for review yet." },
  no_pr: { label: "no PR", chip: "border-zinc-700 bg-zinc-800/60 text-zinc-500",
    tooltip: "No PR exists for this branch. Push and open one." },
};

function sourceChipLabel(source: string): string {
  if (source === "reviewer") return "reviewer";
  if (source === "assignee") return "assignee";
  if (source === "mentions") return "mention";
  return source;
}

function sourceChipTooltip(source: string): string {
  if (source === "reviewer") return "You were directly added as a reviewer.";
  if (source === "assignee") return "You're an assignee on this PR.";
  if (source === "mentions") return "The PR body or comments mention `@you` directly.";
  return source;
}

// --- projection helpers -------------------------------------------------

interface Common {
  prRepo: string;            // GitHub owner/name
  prNumber: number;
  title: string;
  // Where the title link points. Inbox / bookmark / authored / worktree
  // all point at the GitHub PR for uniform behavior (per plan-50).
  // Worktree rows previously linked to the detail page; the "Details"
  // button retains that affordance separately.
  url: string;
  authorLogin?: string;
  ticket: string | null;
  notes: string | null;
  repoLine: string;          // `owner/name` for non-worktree, configured-repo for worktree
}

function project(data: PrCardData): Common {
  switch (data.kind) {
    case "inbox":
      return {
        prRepo: data.row.pr_repo,
        prNumber: data.row.pr_number,
        title: data.row.title,
        url: data.row.url,
        authorLogin: data.row.author_login,
        ticket: data.row.ticket,
        notes: data.row.notes,
        repoLine: data.row.pr_repo,
      };
    case "bookmark":
      return {
        prRepo: data.row.pr_repo,
        prNumber: data.row.pr_number,
        title: data.row.title,
        url: data.row.url,
        authorLogin: data.row.author_login,
        ticket: data.row.ticket,
        notes: data.row.notes,
        repoLine: data.row.pr_repo,
      };
    case "authored":
      return {
        prRepo: data.row.pr_repo,
        prNumber: data.row.pr_number,
        title: data.row.title,
        url: data.row.url,
        authorLogin: undefined,
        ticket: data.row.ticket,
        notes: data.row.notes,
        repoLine: data.row.pr_repo,
      };
    case "worktree": {
      const w = data.row;
      // Title link target: GitHub PR if we know it. The Title
      // component handles the "no PR yet" fallback by rendering a
      // TSR Link to the workspace detail page; the URL it would have
      // gone to (for external rendering) is set to an empty string
      // and Title checks the row directly.
      const url = w.pr_number != null && w.pr_repo
        ? `https://github.com/${w.pr_repo}/pull/${w.pr_number}`
        : "";
      const prRepo = w.pr_repo ?? w.repo;
      return {
        prRepo,
        prNumber: w.pr_number ?? 0,
        title: w.name,
        url,
        authorLogin: w.pr_author_login ?? undefined,
        ticket: w.ticket,
        notes: w.notes,
        repoLine: w.repo,
      };
    }
  }
}

// --- component ---------------------------------------------------------

export function PrCard({ data, jira, bookmarked }: Props) {
  const common = project(data);
  const isBookmarked = bookmarked.has(`${common.prRepo}#${common.prNumber}`);

  return (
    <li className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
      <div className="flex items-start justify-between gap-4">
        <Title data={data} common={common} />
        <ChipBar data={data} />
      </div>
      <div className="mt-2 flex items-end justify-between gap-4">
        <Body data={data} common={common} jira={jira} />
        <div className="flex shrink-0 flex-wrap items-start justify-end gap-2">
          <ActionBar data={data} isBookmarked={isBookmarked} />
        </div>
      </div>
      <NotesSlot data={data} common={common} />
    </li>
  );
}

// --- subcomponents -----------------------------------------------------

interface ChildProps {
  data: PrCardData;
  common: Common;
}

function Title({ data, common }: ChildProps) {
  const className = "min-w-0 truncate font-medium text-zinc-100 hover:text-indigo-300";
  // External link when we have a real http URL (any surface with a
  // known PR). For worktree rows without a known PR yet, fall back
  // to a TSR Link to the workspace detail page — the row still
  // needs a clickable title even before pr_state has resolved.
  const titleNode = common.url
    ? (
      <a
        href={common.url}
        target="_blank"
        rel="noopener noreferrer"
        className={className}
        title={common.title}
      >
        {common.title}
      </a>
    ) : data.kind === "worktree" ? (
      <Link
        to="/workspace/$repo/$name"
        params={{ repo: data.row.repo, name: data.row.name }}
        className={className}
      >
        {common.title}
      </Link>
    ) : (
      <span className={className}>{common.title}</span>
    );

  return (
    <div className="flex min-w-0 items-baseline gap-2">
      {titleNode}
      {common.prNumber > 0 && (
        <span className="shrink-0 font-mono text-xs text-zinc-500">
          #{common.prNumber}
        </span>
      )}
      {data.kind === "worktree" && data.userLogin && data.row.pr_author_login
        && data.row.pr_author_login !== data.userLogin && (
        <Tooltip text={`PR opened by @${data.row.pr_author_login} — you're reviewing it locally.`}>
          <span className="shrink-0 rounded border border-indigo-800 bg-indigo-900/30 px-1.5 py-0.5 text-[10px] text-indigo-300">
            @{data.row.pr_author_login}
          </span>
        </Tooltip>
      )}
    </div>
  );
}

function ChipBar({ data }: { data: PrCardData }) {
  return (
    <div className="flex flex-wrap items-center justify-end gap-x-2 gap-y-1">
      {data.kind === "inbox" && (
        <>
          <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] ${CI_STYLE[data.row.ci_status].cls}`}>
            {CI_STYLE[data.row.ci_status].label}
          </span>
          {data.row.sources.map((s) => (
            <Tooltip key={s} text={sourceChipTooltip(s)}>
              <span className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
                {sourceChipLabel(s)}
              </span>
            </Tooltip>
          ))}
        </>
      )}
      {data.kind === "bookmark" && (
        <>
          <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] ${BOOKMARK_STATE_STYLE[data.row.state].cls}`}>
            {BOOKMARK_STATE_STYLE[data.row.state].label}
          </span>
          <span className="shrink-0 rounded border border-indigo-800 bg-indigo-900/30 px-1.5 py-0.5 text-[10px] text-indigo-300">
            bookmark
          </span>
        </>
      )}
      {data.kind === "authored" && (
        <>
          {data.row.is_draft && (
            <span className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
              draft
            </span>
          )}
          <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] ${CI_STYLE[data.row.ci_status].cls}`}>
            {CI_STYLE[data.row.ci_status].label}
          </span>
        </>
      )}
      {data.kind === "worktree" && (
        <>
          {data.row.status === "code_on_disk" && (
            <Tooltip text="Worktree was created, but a setup_step errored. Code is on disk — open in iTerm2/Cursor and re-run the failing step.">
              <span className="rounded border border-amber-800 bg-amber-900/40 px-1.5 py-0.5 text-[10px] text-amber-300">
                setup incomplete
              </span>
            </Tooltip>
          )}
          {(data.row.pr_state?.labels?.length
            ? data.row.pr_state.labels
            : data.row.pr_state?.headline
              ? [data.row.pr_state.headline]
              : ["no_pr" as PrHeadline]
          ).map((label) => {
            const style = LABEL_CHIP_STYLE[label];
            return (
              <Tooltip key={label} text={style.tooltip}>
                <span className={`rounded border px-1.5 py-0.5 text-[10px] ${style.chip}`}>
                  {style.label}
                </span>
              </Tooltip>
            );
          })}
        </>
      )}
    </div>
  );
}

function Body({ data, common, jira }: ChildProps & { jira: JiraConfig | null }) {
  return (
    <div className="min-w-0 flex-1 space-y-0.5 text-xs text-zinc-500">
      {data.kind !== "worktree" ? (
        <div>
          {common.authorLogin && <>@{common.authorLogin} </>}
          <span className="text-zinc-600">{common.authorLogin && "· "}{common.repoLine}</span>
        </div>
      ) : (
        <>
          <div>branch: {data.row.branch}</div>
          {common.ticket && (
            <div>ticket: <TicketValue ticket={common.ticket} jira={jira} /></div>
          )}
          <div className="truncate font-mono text-zinc-600" title={data.row.path}>{data.row.path}</div>
        </>
      )}
      {data.kind !== "worktree" && common.ticket && (
        <div>ticket: <TicketValue ticket={common.ticket} jira={jira} /></div>
      )}
    </div>
  );
}

function NotesSlot({ data, common }: ChildProps) {
  switch (data.kind) {
    case "inbox":
      return <div className="mt-3"><InboxNotes prRepo={common.prRepo} prNumber={common.prNumber} notes={common.notes} /></div>;
    case "bookmark":
      return <div className="mt-3"><BookmarkNotes prRepo={common.prRepo} prNumber={common.prNumber} notes={common.notes} /></div>;
    case "authored":
      return <div className="mt-3"><AuthoredPrNotes prRepo={common.prRepo} prNumber={common.prNumber} notes={common.notes} /></div>;
    case "worktree":
      return <div className="mt-3"><WorkspaceNotes repo={data.row.repo} name={data.row.name} notes={common.notes} /></div>;
  }
}

// --- action bar --------------------------------------------------------

interface ActionProps {
  data: PrCardData;
  isBookmarked: boolean;
}

function ActionBar({ data, isBookmarked }: ActionProps) {
  switch (data.kind) {
    case "inbox":
      return (
        <>
          <OpenPrButton url={data.row.url} />
          {!isBookmarked && <BookmarkThisButton prRepo={data.row.pr_repo} prNumber={data.row.pr_number} />}
          <InboxPullDownButton pr={data.row} />
          <InboxArchiveButton pr={data.row} />
        </>
      );
    case "bookmark":
      return (
        <>
          <OpenPrButton url={data.row.url} />
          <BookmarkPullDownButton row={data.row} />
          <UnbookmarkButton row={data.row} />
        </>
      );
    case "authored":
      return (
        <>
          <OpenPrButton url={data.row.url} />
          {!isBookmarked && <BookmarkThisButton prRepo={data.row.pr_repo} prNumber={data.row.pr_number} />}
          <AuthoredPullDownButton pr={data.row} />
        </>
      );
    case "worktree":
      return (
        <>
          <WorktreeActionButton row={data.row} />
          <WorktreePrButton repo={data.row.repo} name={data.row.name} prUrl={data.row.pr_number != null && data.row.pr_repo
            ? `https://github.com/${data.row.pr_repo}/pull/${data.row.pr_number}` : null} />
          {!isBookmarked && data.row.pr_repo && data.row.pr_number != null && (
            <BookmarkThisButton prRepo={data.row.pr_repo} prNumber={data.row.pr_number} />
          )}
          <DetailsLink repo={data.row.repo} name={data.row.name} />
        </>
      );
  }
}

// --- shared / cross-surface buttons ------------------------------------

function OpenPrButton({ url }: { url: string }) {
  return (
    <Tooltip text="Open the GitHub PR in a new tab.">
      <a
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex shrink-0 items-center rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-200 hover:bg-zinc-700"
      >
        PR
      </a>
    </Tooltip>
  );
}

function BookmarkThisButton({ prRepo, prNumber }: { prRepo: string; prNumber: number }) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => bookmarkPr(prRepo, prNumber),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["bookmarks"] });
      queryClient.invalidateQueries({ queryKey: ["inbox"] });
      queryClient.invalidateQueries({ queryKey: ["authored-prs"] });
    },
  });
  const tooltip = mutation.error
    ? mutation.error instanceof ApiError ? mutation.error.detail : String(mutation.error)
    : "Add this PR to your Bookmarks. Sticky — survives close/merge until you unbookmark.";
  return (
    <Tooltip text={tooltip}>
      <button
        type="button"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending || mutation.isSuccess}
        className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-200 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {mutation.isPending ? "Bookmarking…" : mutation.isSuccess ? "Bookmarked" : "Bookmark"}
      </button>
    </Tooltip>
  );
}

interface PullDownProps {
  prRepo: string;
  prNumber: number;
  repoConfigured: boolean;
  // The actual pull-down call. Differs per surface.
  pullDownFn: () => Promise<unknown>;
  invalidateKeys: string[][];
}

function PullDownAffordance({ prRepo, prNumber, repoConfigured, pullDownFn, invalidateKeys }: PullDownProps) {
  const queryClient = useQueryClient();
  const pullDownMutation = useMutation({
    mutationFn: pullDownFn,
    onSuccess: () => {
      for (const key of invalidateKeys) queryClient.invalidateQueries({ queryKey: key });
    },
  });
  const configureMutation = useMutation({
    mutationFn: () => configureAndPullDown(prRepo, prNumber),
  });
  const isConfigureFlow = !repoConfigured;
  const mutation = isConfigureFlow ? configureMutation : pullDownMutation;
  const disabled = mutation.isPending || mutation.isSuccess;

  const label = isConfigureFlow
    ? configureMutation.isPending ? "Spawning…"
      : configureMutation.isSuccess ? "Claude opened"
      : "Configure repo + pull down"
    : pullDownMutation.isPending ? "Pulling…"
      : pullDownMutation.isSuccess ? "Pulled"
      : "Pull down";
  const tooltip = mutation.error
    ? mutation.error instanceof ApiError ? mutation.error.detail : String(mutation.error)
    : isConfigureFlow
      ? `Opens Claude in your development_root to onboard ${prRepo}, then automatically pulls this PR into a worktree once onboarding completes.`
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

function InboxPullDownButton({ pr }: { pr: InboxPr }) {
  return (
    <PullDownAffordance
      prRepo={pr.pr_repo}
      prNumber={pr.pr_number}
      repoConfigured={pr.repo_configured}
      pullDownFn={() => pullDownPr(pr.pr_repo, pr.pr_number)}
      invalidateKeys={[["worktrees"], ["inbox"]]}
    />
  );
}

function AuthoredPullDownButton({ pr }: { pr: AuthoredPr }) {
  return (
    <PullDownAffordance
      prRepo={pr.pr_repo}
      prNumber={pr.pr_number}
      repoConfigured={pr.repo_configured}
      pullDownFn={() => pullDownAuthoredPr(pr.pr_repo, pr.pr_number)}
      invalidateKeys={[["worktrees"], ["authored-prs"]]}
    />
  );
}

function BookmarkPullDownButton({ row }: { row: BookmarkPr }) {
  // Bookmark rows don't carry a `repo_configured` flag from the
  // backend; assume true and let the API surface 400 if not. Cost
  // is one extra round-trip on a misclick — acceptable given the
  // alternative is plumbing config checks into every bookmark
  // payload. Future enhancement: include the flag on bookmark rows.
  return (
    <PullDownAffordance
      prRepo={row.pr_repo}
      prNumber={row.pr_number}
      repoConfigured={true}
      pullDownFn={() => pullDownBookmark(row.pr_repo, row.pr_number)}
      invalidateKeys={[["worktrees"], ["bookmarks"]]}
    />
  );
}

function InboxArchiveButton({ pr }: { pr: InboxPr }) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => archiveInboxPr(pr.pr_repo, pr.pr_number),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["inbox"] }),
  });
  const tooltip = mutation.error
    ? mutation.error instanceof ApiError ? mutation.error.detail : String(mutation.error)
    : "Remove this PR from the inbox. Sticky — won't reappear from gh search.";
  return (
    <Tooltip text={tooltip}>
      <button
        type="button"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending}
        className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {mutation.isPending ? "Removing…" : "Remove"}
      </button>
    </Tooltip>
  );
}

function UnbookmarkButton({ row }: { row: BookmarkPr }) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => deleteBookmark(row.pr_repo, row.pr_number),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["bookmarks"] });
      // Unbookmarking re-exposes the PR to inbox/authored auto-watch.
      queryClient.invalidateQueries({ queryKey: ["inbox"] });
      queryClient.invalidateQueries({ queryKey: ["authored-prs"] });
    },
  });
  const tooltip = mutation.error
    ? mutation.error instanceof ApiError ? mutation.error.detail : String(mutation.error)
    : "Remove this bookmark.";
  return (
    <Tooltip text={tooltip}>
      <button
        type="button"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending}
        className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {mutation.isPending ? "Removing…" : "Unbookmark"}
      </button>
    </Tooltip>
  );
}

function DetailsLink({ repo, name }: { repo: string; name: string }) {
  return (
    <Tooltip text="Workspace details: run skills, send text, view setup log, delete worktree">
      <Link
        to="/workspace/$repo/$name"
        params={{ repo, name }}
        className="rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-200 hover:bg-zinc-700"
      >
        Details
      </Link>
    </Tooltip>
  );
}

// --- worktree-specific action button (iTerm2 / Focus / Recreate) -------

interface WorktreeActionButtonProps {
  row: Worktree;
}

function WorktreeActionButton({ row }: WorktreeActionButtonProps) {
  const queryClient = useQueryClient();
  const spawnMutation = useMutation({
    mutationFn: () => spawnIterm(row.repo, row.name),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["worktrees"] }),
  });
  const focusMutation = useMutation({
    mutationFn: () => focusIterm(row.repo, row.name),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["worktrees"] }),
  });
  const recreateMutation = useMutation({
    mutationFn: () => recreateWorktree(row.repo, row.name),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["worktrees"] }),
  });

  const status: WorktreeStatus = row.status;

  if (status === "failed") {
    return (
      <Tooltip text="Setup didn't complete. Click for the setup log on Details.">
        <Link
          to="/workspace/$repo/$name"
          params={{ repo: row.repo, name: row.name }}
          className="rounded border border-red-700 bg-red-950/40 px-3 py-1 text-xs text-red-300 hover:bg-red-900/40"
        >
          Setup failed
        </Link>
      </Tooltip>
    );
  }
  if (status === "setting_up") {
    return (
      <Tooltip text="Setup in progress. Check Details for the live log.">
        <button type="button" disabled className="rounded border border-amber-800 bg-amber-950/40 px-3 py-1 text-xs text-amber-300 disabled:cursor-not-allowed disabled:opacity-70">
          Configuring…
        </button>
      </Tooltip>
    );
  }
  const itermBtn = (() => {
    if (row.has_claude_session) {
      const err = mutationError(focusMutation.error);
      return (
        <ButtonWithError
          tooltip={err ?? "Bring this worktree's open Claude session in iTerm2 to the front."}
          errorDetail={err}
          onClick={() => focusMutation.mutate()}
          pending={focusMutation.isPending}
          pendingLabel="Focusing…"
          idleLabel="Focus iTerm2"
        />
      );
    }
    const err = mutationError(spawnMutation.error);
    return (
      <ButtonWithError
        tooltip={err ?? "Open this workspace in a new iTerm2 window."}
        errorDetail={err}
        onClick={() => spawnMutation.mutate()}
        pending={spawnMutation.isPending}
        pendingLabel="Opening…"
        idleLabel="iTerm2"
      />
    );
  })();

  if (status === "stale" || status === "code_on_disk") {
    const err = mutationError(recreateMutation.error);
    const tooltip =
      status === "stale"
        ? "On-disk directory is gone. Click to re-run git worktree add + setup_steps."
        : "Setup didn't finish. Click to wipe + re-run setup_steps.";
    const recreateBtn = (
      <ButtonWithError
        tooltip={err ?? tooltip}
        errorDetail={err}
        onClick={() => recreateMutation.mutate()}
        pending={recreateMutation.isPending}
        pendingLabel="Recreating…"
        idleLabel="Recreate workspace"
      />
    );
    if (status === "stale") return recreateBtn;
    return (
      <>
        {itermBtn}
        {recreateBtn}
      </>
    );
  }

  return itermBtn;
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

function ButtonWithError({ tooltip, errorDetail, onClick, pending, pendingLabel, idleLabel }: ButtonWithErrorProps) {
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
        <p role="alert" className="max-w-[220px] text-right text-[10px] leading-tight text-red-400" title={errorDetail}>
          {errorDetail}
        </p>
      )}
    </div>
  );
}

// --- worktree PR button (lazy lookup) ----------------------------------

interface WorktreePrButtonProps {
  repo: string;
  name: string;
  // When the worktree row already knows its PR URL (pr_number + pr_repo
  // set), use it directly — no API round-trip. When null, fall back to
  // the lazy lookup that hits `/api/worktree/.../pr-url` and opens.
  prUrl: string | null;
}

function WorktreePrButton({ repo, name, prUrl }: WorktreePrButtonProps) {
  if (prUrl) {
    return <OpenPrButton url={prUrl} />;
  }
  return <LazyWorktreePrButton repo={repo} name={name} />;
}

type LazyState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "missing" }
  | { kind: "error"; message: string };

function LazyWorktreePrButton({ repo, name }: { repo: string; name: string }) {
  const [state, setState] = useState<LazyState>({ kind: "idle" });
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
      setState({ kind: "error", message: err instanceof Error ? err.message : String(err) });
    }
  };
  const label = state.kind === "loading" ? "Looking up…"
    : state.kind === "missing" ? "No PR"
    : state.kind === "error" ? "PR failed" : "PR";
  const tooltip = state.kind === "error" ? state.message
    : state.kind === "missing" ? "gh pr view found no PR for this branch yet"
    : "Look up the GitHub PR for this branch and open it.";
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

// --- ticket / Jira link ------------------------------------------------

function TicketValue({ ticket, jira }: { ticket: string; jira: JiraConfig | null }) {
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

// --- query helper for parent components --------------------------------

/**
 * Hook for parent components: returns the set of `${pr_repo}#${pr_number}`
 * keys for currently-bookmarked PRs. Used to hide the "Bookmark this"
 * button on inbox/authored/worktree rows that are already in the
 * bookmark surface.
 *
 * Uses the shared ``["bookmarks"]`` query key — multiple callers
 * dedup through react-query's cache, so this never causes extra
 * network fetches beyond the one ``BookmarkList`` already runs.
 */
export function useBookmarkedKeys(): Set<string> {
  const q = useQuery({
    queryKey: ["bookmarks"],
    queryFn: listBookmarks,
    refetchInterval: 60_000,
  });
  const list = q.data?.bookmarks ?? [];
  return new Set(list.map((b) => `${b.pr_repo}#${b.pr_number}`));
}
