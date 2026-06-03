import { useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getJiraConfig } from "../api/config";
import { refreshInbox } from "../api/inbox";
import { listRepos } from "../api/repos";
import { listWorktrees, syncWorktrees } from "../api/worktrees";
import { AddRepoModal } from "../components/AddRepoModal";
import { AuthoredPrTier } from "../components/AuthoredPrTier";
import { BookmarkList } from "../components/BookmarkList";
import { Button } from "../components/Button";
import { AskClaudeTile } from "../components/AskClaudeTile";
import { OpenClaudeTile } from "../components/OpenClaudeTile";
import { InboxList } from "../components/InboxList";
import { RepoList } from "../components/RepoList";
import { TokenUsageTile } from "../components/TokenUsageTile";
import { Tooltip } from "../components/Tooltip";
import { WorkspaceList } from "../components/WorkspaceList";

export const Route = createFileRoute("/")({
  component: HubPage,
});

export function HubPage() {
  const [modalOpen, setModalOpen] = useState(false);
  const queryClient = useQueryClient();

  const reposQuery = useQuery({
    queryKey: ["repos"],
    queryFn: listRepos,
  });

  const worktreesQuery = useQuery({
    queryKey: ["worktrees"],
    queryFn: listWorktrees,
    refetchInterval: 5_000,
  });

  const jiraQuery = useQuery({
    queryKey: ["config", "jira"],
    queryFn: getJiraConfig,
  });

  const repos = reposQuery.data ?? [];
  const worktrees = worktreesQuery.data?.worktrees ?? [];
  const userLogin = worktreesQuery.data?.user_login ?? null;
  const jira = jiraQuery.data ?? null;

  const sync = useMutation({
    // Fire both reconcile passes in parallel: local worktrees against
    // git, and the inbox against `gh search prs`. The background inbox
    // poll keeps running independently every 60s — this just forces an
    // immediate tick so the user doesn't wait.
    mutationFn: async () => {
      const [worktreesResult] = await Promise.all([
        syncWorktrees(),
        refreshInbox(),
      ]);
      return worktreesResult;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["worktrees"] });
      queryClient.invalidateQueries({ queryKey: ["inbox"] });
    },
  });

  return (
    <main className="mx-auto max-w-7xl p-8">
      <header className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold">Claude Developer Hub</h1>
        <Button onClick={() => setModalOpen(true)}>Add a repo</Button>
      </header>

      {/* 50/50 grid at max-w-7xl: aside ends up roughly 50% wider than
          the prior 3:2 layout at max-w-5xl. Workspaces stay readable
          because the wider container offsets the narrower percentage
          share. */}
      <div className="mt-8 grid grid-cols-2 gap-6">
        <div className="space-y-8">
          {repos.length > 0 && (
            <div className="flex justify-end">
              <Tooltip text="Reconcile workspaces with `git worktree list` (import new ones, drop removed ones) AND force an inbox refresh against GitHub. The background inbox poll continues every 60s.">
                <Button
                  variant="secondary"
                  onClick={() => sync.mutate()}
                  disabled={sync.isPending}
                >
                  {sync.isPending ? "Syncing…" : "Sync"}
                </Button>
              </Tooltip>
            </div>
          )}
          <BookmarkList jira={jira} />
          <InboxList jira={jira} />
          <section>
            <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
              Workspaces
            </h2>
            {sync.isSuccess && sync.data && (
              <p className="mt-2 text-xs text-zinc-500">
                Imported {sync.data.imported.length} · removed{" "}
                {sync.data.removed.length} · skipped{" "}
                {sync.data.skipped.length}
                {sync.data.skipped.length > 0 && (
                  <>
                    {" "}
                    <span className="text-zinc-600">
                      ({Array.from(new Set(sync.data.skipped.map((s) => s.reason))).join(", ")})
                    </span>
                  </>
                )}
              </p>
            )}
            {sync.isError && (
              <p className="mt-2 text-xs text-red-400">
                sync failed: {String(sync.error)}
              </p>
            )}
            <div className="mt-3 space-y-4">
              <AuthoredPrTier jira={jira} />
              {worktreesQuery.isLoading && (
                <p className="text-sm text-zinc-500">Loading…</p>
              )}
              {worktreesQuery.isError && (
                <p className="text-sm text-red-400">Failed to load worktrees.</p>
              )}
              {worktreesQuery.isSuccess && worktrees.length === 0 && (
                <div className="rounded-lg border border-dashed border-zinc-700 p-6 text-center">
                  <p className="text-sm text-zinc-400">
                    {repos.length === 0
                      ? "Add a repo first, then create a worktree from a branch."
                      : "No worktrees yet. Pull down a bookmarked, inbox, or authored PR to create one."}
                  </p>
                </div>
              )}
              {worktreesQuery.isSuccess && worktrees.length > 0 && (
                <WorkspaceList
                  worktrees={worktrees}
                  jira={jira}
                  userLogin={userLogin}
                />
              )}
            </div>
          </section>

          <section>
            <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
              Repos
            </h2>
            <div className="mt-3">
              {reposQuery.isLoading && (
                <p className="text-sm text-zinc-500">Loading…</p>
              )}
              {reposQuery.isError && (
                <p className="text-sm text-red-400">Failed to load repos.</p>
              )}
              {reposQuery.isSuccess && repos.length === 0 && (
                <div className="rounded-lg border border-dashed border-zinc-700 p-6 text-center">
                  <p className="text-sm text-zinc-400">No repos configured yet.</p>
                  <div className="mt-3">
                    <Button onClick={() => setModalOpen(true)}>
                      Add your first repo
                    </Button>
                  </div>
                </div>
              )}
              {reposQuery.isSuccess && repos.length > 0 && <RepoList repos={repos} />}
            </div>
          </section>
        </div>

        <aside className="space-y-6">
          <TokenUsageTile />
          <AskClaudeTile />
          <OpenClaudeTile />
        </aside>
      </div>

      <AddRepoModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onSaved={() => queryClient.invalidateQueries({ queryKey: ["repos"] })}
      />
    </main>
  );
}
