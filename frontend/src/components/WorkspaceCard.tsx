import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { pullDownAuthoredPr, updateAuthoredPrNotes } from "../api/authored";
import {
  bookmarkPr,
  deleteBookmark,
  pullDownBookmark,
  updateBookmarkNotes,
} from "../api/bookmarks";
import {
  getPrUrl,
  recreateWorktree,
  spawnIterm,
  updateNotes,
} from "../api/worktrees";
import { useTerminalInfo } from "../api/terminal";
import type {
  BookmarkState,
  JiraConfig,
  PrHeadline,
  WorkspaceEntity,
} from "../api/types";
import { NotesEditor } from "./NotesEditor";
import { Tooltip } from "./Tooltip";

// Every mutation on a workspace card refreshes the one unified query.
const WORKSPACES_KEY = ["workspaces"];

// --- chip styling -------------------------------------------------------

const CI_STYLE: Record<string, { label: string; cls: string }> = {
  pass: { label: "ci ✓", cls: "border-emerald-800 bg-emerald-900/40 text-emerald-300" },
  fail: { label: "ci ✗", cls: "border-red-800 bg-red-900/40 text-red-300" },
  pending: { label: "ci ⋯", cls: "border-amber-800 bg-amber-900/40 text-amber-300" },
};

const STATE_STYLE: Record<BookmarkState, { label: string; cls: string }> = {
  open: { label: "open", cls: "border-emerald-800 bg-emerald-900/40 text-emerald-300" },
  merged: { label: "merged", cls: "border-purple-800 bg-purple-900/40 text-purple-300" },
  closed: { label: "closed", cls: "border-zinc-700 bg-zinc-800 text-zinc-400" },
};

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

// --- component ---------------------------------------------------------

interface Props {
  entity: WorkspaceEntity;
  jira: JiraConfig | null;
  // Local user's gh login (for the "you're reviewing @x's PR" chip).
  userLogin: string | null;
}

export function WorkspaceCard({ entity, jira, userLogin }: Props) {
  const isLocal = entity.worktree != null;
  const hasPr = entity.pr_number != null;
  const isReviewing =
    userLogin != null &&
    entity.author_login != null &&
    entity.author_login !== userLogin;

  return (
    <li className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
      <div className="flex items-start justify-between gap-4">
        <Title entity={entity} isLocal={isLocal} hasPr={hasPr} isReviewing={isReviewing} />
        <ChipBar entity={entity} isLocal={isLocal} hasPr={hasPr} />
      </div>
      <div className="mt-2 flex items-end justify-between gap-4">
        <Body entity={entity} isLocal={isLocal} jira={jira} />
        <div className="flex shrink-0 flex-wrap items-start justify-end gap-2">
          <ActionBar entity={entity} isLocal={isLocal} />
        </div>
      </div>
      <div className="mt-3">
        <EntityNotes entity={entity} isLocal={isLocal} />
      </div>
    </li>
  );
}

interface CardChildProps {
  entity: WorkspaceEntity;
  isLocal: boolean;
}

function Title({
  entity,
  isLocal,
  hasPr,
  isReviewing,
}: CardChildProps & { hasPr: boolean; isReviewing: boolean }) {
  const className = "min-w-0 truncate font-medium text-zinc-100 hover:text-indigo-300";
  const titleNode = entity.url ? (
    <a href={entity.url} target="_blank" rel="noopener noreferrer" className={className} title={entity.title}>
      {entity.title}
    </a>
  ) : isLocal && entity.worktree ? (
    <Link
      to="/workspace/$repo/$name"
      params={{ repo: entity.worktree.repo, name: entity.worktree.name }}
      className={className}
    >
      {entity.title}
    </Link>
  ) : (
    <span className={className}>{entity.title}</span>
  );

  return (
    <div className="flex min-w-0 items-baseline gap-2">
      {titleNode}
      {hasPr && (
        <span className="shrink-0 font-mono text-xs text-zinc-500">#{entity.pr_number}</span>
      )}
      {isReviewing && (
        <Tooltip text={`PR opened by @${entity.author_login} — you're reviewing it.`}>
          <span className="shrink-0 rounded border border-indigo-800 bg-indigo-900/30 px-1.5 py-0.5 text-[10px] text-indigo-300">
            @{entity.author_login}
          </span>
        </Tooltip>
      )}
    </div>
  );
}

function ChipBar({
  entity,
  isLocal,
  hasPr,
}: CardChildProps & { hasPr: boolean }) {
  const terminal = useTerminalInfo();
  const status = entity.worktree?.status;
  // Prefer the rich pr_state labels; fall back to the synchronously-
  // written scalars so a chip shows before the enrichment poll runs.
  const labels: PrHeadline[] | null =
    entity.pr_state?.labels && entity.pr_state.labels.length > 0
      ? entity.pr_state.labels
      : entity.pr_state?.headline
        ? [entity.pr_state.headline]
        : null;

  return (
    <div className="flex flex-wrap items-center justify-end gap-x-2 gap-y-1">
      {isLocal && status === "setting_up" && (
        <Tooltip text="Background setup in progress. Click Details for the live log.">
          <span className="rounded border border-amber-800 bg-amber-900/40 px-1.5 py-0.5 text-[10px] text-amber-300">
            setting up…
          </span>
        </Tooltip>
      )}
      {isLocal && status === "code_on_disk" && (
        <Tooltip text={`Worktree was created, but a setup_step errored. Code is on disk — open in ${terminal.display_name}/Cursor and re-run the failing step.`}>
          <span className="rounded border border-amber-800 bg-amber-900/40 px-1.5 py-0.5 text-[10px] text-amber-300">
            setup incomplete
          </span>
        </Tooltip>
      )}
      {labels ? (
        labels.map((label) => {
          const style = LABEL_CHIP_STYLE[label];
          return (
            <Tooltip key={label} text={style.tooltip}>
              <span className={`rounded border px-1.5 py-0.5 text-[10px] ${style.chip}`}>
                {style.label}
              </span>
            </Tooltip>
          );
        })
      ) : hasPr ? (
        <>
          <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] ${STATE_STYLE[entity.state ?? "open"].cls}`}>
            {STATE_STYLE[entity.state ?? "open"].label}
          </span>
          {entity.ci_status && entity.ci_status !== "none" && (
            <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] ${CI_STYLE[entity.ci_status].cls}`}>
              {CI_STYLE[entity.ci_status].label}
            </span>
          )}
          {entity.is_draft && (
            <span className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
              draft
            </span>
          )}
        </>
      ) : (
        <Tooltip text={LABEL_CHIP_STYLE.no_pr.tooltip}>
          <span className={`rounded border px-1.5 py-0.5 text-[10px] ${LABEL_CHIP_STYLE.no_pr.chip}`}>
            {LABEL_CHIP_STYLE.no_pr.label}
          </span>
        </Tooltip>
      )}
      {entity.is_bookmarked && (
        <Tooltip text="Bookmarked — you're tracking this PR.">
          <span className="shrink-0 rounded border border-indigo-800 bg-indigo-900/30 px-1.5 py-0.5 text-[10px] text-indigo-300">
            ★
          </span>
        </Tooltip>
      )}
    </div>
  );
}

function Body({
  entity,
  isLocal,
  jira,
}: CardChildProps & { jira: JiraConfig | null }) {
  return (
    <div className="min-w-0 flex-1 space-y-0.5 text-xs text-zinc-500">
      {isLocal && entity.worktree ? (
        <>
          <div>branch: {entity.worktree.branch}</div>
          {entity.ticket && (
            <div>ticket: <TicketValue ticket={entity.ticket} jira={jira} /></div>
          )}
          <div className="truncate font-mono text-zinc-600" title={entity.worktree.path}>
            {entity.worktree.path}
          </div>
        </>
      ) : (
        <>
          <div>
            {entity.author_login && <>@{entity.author_login} </>}
            <span className="text-zinc-600">
              {entity.author_login && "· "}{entity.pr_repo}
            </span>
          </div>
          {entity.ticket && (
            <div>ticket: <TicketValue ticket={entity.ticket} jira={jira} /></div>
          )}
        </>
      )}
    </div>
  );
}

function ActionBar({ entity, isLocal }: CardChildProps) {
  const canBookmark = entity.pr_repo != null && entity.pr_number != null;
  return (
    <>
      {isLocal && entity.worktree && <WorktreeActionButton entity={entity} />}
      <OpenPrButton entity={entity} isLocal={isLocal} />
      {!isLocal && canBookmark && <PullDownButton entity={entity} />}
      {canBookmark &&
        (entity.is_bookmarked ? (
          <UnbookmarkButton prRepo={entity.pr_repo!} prNumber={entity.pr_number!} />
        ) : (
          <BookmarkButton prRepo={entity.pr_repo!} prNumber={entity.pr_number!} />
        ))}
      {isLocal && entity.worktree && (
        <DetailsLink repo={entity.worktree.repo} name={entity.worktree.name} />
      )}
    </>
  );
}

// --- buttons -----------------------------------------------------------

function OpenPrButton({ entity, isLocal }: CardChildProps) {
  if (entity.url) {
    return (
      <Tooltip text="Open the GitHub PR in a new tab.">
        <a
          href={entity.url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex shrink-0 items-center rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-200 hover:bg-zinc-700"
        >
          PR
        </a>
      </Tooltip>
    );
  }
  // A local branch whose PR we don't know yet — look it up on click.
  if (isLocal && entity.worktree) {
    return <LazyWorktreePrButton repo={entity.worktree.repo} name={entity.worktree.name} />;
  }
  return null;
}

function BookmarkButton({ prRepo, prNumber }: { prRepo: string; prNumber: number }) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => bookmarkPr(prRepo, prNumber),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: WORKSPACES_KEY }),
  });
  const tooltip = mutation.error
    ? mutation.error instanceof ApiError ? mutation.error.detail : String(mutation.error)
    : "Add this PR to your tracked list. Sticky — survives close/merge until you unbookmark.";
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

function UnbookmarkButton({ prRepo, prNumber }: { prRepo: string; prNumber: number }) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => deleteBookmark(prRepo, prNumber),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: WORKSPACES_KEY }),
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

function PullDownButton({ entity }: { entity: WorkspaceEntity }) {
  const queryClient = useQueryClient();
  const prRepo = entity.pr_repo!;
  const prNumber = entity.pr_number!;
  // Discriminate on bookmark first: a bookmarked PR goes through the
  // bookmark route, otherwise it's one of your own authored PRs.
  const pullDownFn = entity.is_bookmarked
    ? () => pullDownBookmark(prRepo, prNumber)
    : () => pullDownAuthoredPr(prRepo, prNumber);
  const mutation = useMutation({
    mutationFn: pullDownFn,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: WORKSPACES_KEY }),
  });

  if (mutation.isSuccess && mutation.data) {
    const { repo, name } = mutation.data;
    return (
      <Tooltip text="Open the workspace to watch the live setup log.">
        <Link
          to="/workspace/$repo/$name"
          params={{ repo, name }}
          className="shrink-0 rounded border border-emerald-800 bg-emerald-950/40 px-3 py-1 text-xs text-emerald-300 hover:bg-emerald-900/40"
        >
          Pulled
        </Link>
      </Tooltip>
    );
  }

  const tooltip = mutation.error
    ? mutation.error instanceof ApiError ? mutation.error.detail : String(mutation.error)
    : "Fetch this PR's branch and create a local worktree.";
  return (
    <Tooltip text={tooltip}>
      <button
        type="button"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending || mutation.isSuccess}
        className="shrink-0 rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-200 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {mutation.isPending ? "Pulling…" : "Pull down"}
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

function WorktreeActionButton({ entity }: { entity: WorkspaceEntity }) {
  const wt = entity.worktree!;
  const queryClient = useQueryClient();
  const terminal = useTerminalInfo();
  const spawnMutation = useMutation({
    mutationFn: () => spawnIterm(wt.repo, wt.name),
    onSettled: () => queryClient.invalidateQueries({ queryKey: WORKSPACES_KEY }),
  });
  const recreateMutation = useMutation({
    mutationFn: () => recreateWorktree(wt.repo, wt.name),
    onSettled: () => queryClient.invalidateQueries({ queryKey: WORKSPACES_KEY }),
  });

  if (wt.status === "failed") {
    return (
      <Tooltip text="Setup didn't complete. Click for the setup log on Details.">
        <Link
          to="/workspace/$repo/$name"
          params={{ repo: wt.repo, name: wt.name }}
          className="rounded border border-red-700 bg-red-950/40 px-3 py-1 text-xs text-red-300 hover:bg-red-900/40"
        >
          Setup failed
        </Link>
      </Tooltip>
    );
  }
  if (wt.status === "setting_up") {
    return (
      <Tooltip text="Setup in progress. Check Details for the live log.">
        <button type="button" disabled className="rounded border border-amber-800 bg-amber-950/40 px-3 py-1 text-xs text-amber-300 disabled:cursor-not-allowed disabled:opacity-70">
          Configuring…
        </button>
      </Tooltip>
    );
  }
  const spawnErr = mutationError(spawnMutation.error);
  const itermBtn = (
    <ButtonWithError
      tooltip={spawnErr ?? `Open this workspace in a new ${terminal.display_name} window.`}
      errorDetail={spawnErr}
      onClick={() => spawnMutation.mutate()}
      pending={spawnMutation.isPending}
      pendingLabel="Opening…"
      idleLabel={terminal.display_name}
    />
  );

  if (wt.status === "stale" || wt.status === "code_on_disk") {
    const err = mutationError(recreateMutation.error);
    const tooltip =
      wt.status === "stale"
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
    if (wt.status === "stale") return recreateBtn;
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

// --- notes (dispatches save target off the entity) ---------------------

function EntityNotes({ entity, isLocal }: CardChildProps) {
  const queryClient = useQueryClient();
  const wt = entity.worktree;
  const prRepo = entity.pr_repo;
  const prNumber = entity.pr_number;

  const saveFn = (text: string): Promise<unknown> => {
    if (isLocal && wt) return updateNotes(wt.repo, wt.name, text);
    if (prRepo != null && prNumber != null) {
      return entity.is_bookmarked
        ? updateBookmarkNotes(prRepo, prNumber, text)
        : updateAuthoredPrNotes(prRepo, prNumber, text);
    }
    return Promise.resolve();
  };

  return (
    <NotesEditor
      notes={entity.notes}
      saveFn={saveFn}
      onSaved={() => {
        queryClient.invalidateQueries({ queryKey: WORKSPACES_KEY });
        if (isLocal && wt) {
          queryClient.invalidateQueries({ queryKey: ["worktree", wt.repo, wt.name] });
        }
      }}
    />
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
