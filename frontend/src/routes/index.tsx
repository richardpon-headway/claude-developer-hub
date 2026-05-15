import { useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getJiraConfig } from "../api/config";
import { listRepos } from "../api/repos";
import { listWorktrees, syncWorktrees } from "../api/worktrees";
import { AddRepoModal } from "../components/AddRepoModal";
import { Button } from "../components/Button";
import { GlobalSkillsTile } from "../components/GlobalSkillsTile";
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
  const worktrees = worktreesQuery.data ?? [];
  const jira = jiraQuery.data ?? null;

  const sync = useMutation({
    mutationFn: syncWorktrees,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["worktrees"] }),
  });

  return (
    <main className="mx-auto max-w-5xl p-8">
      <header className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold">Claude Developer Hub</h1>
        <Button onClick={() => setModalOpen(true)}>Add a repo</Button>
      </header>

      {/* 5-column grid gives the aside 2/5 of the row (40%) instead of
          the previous 1/3 — enough to keep the tokens tile + global
          skills + freeform input comfortable without squeezing the
          workspace cards. */}
      <div className="mt-8 grid grid-cols-5 gap-6">
        <div className="col-span-3 space-y-8">
          <InboxList />
          <section>
            <div className="flex items-baseline justify-between">
              <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
                Workspaces
              </h2>
              {repos.length > 0 && (
                <Tooltip text="Reconcile workspaces with `git worktree list`: import new ones, drop rows whose worktree was removed outside CDH.">
                  <Button
                    variant="secondary"
                    onClick={() => sync.mutate()}
                    disabled={sync.isPending}
                  >
                    {sync.isPending ? "Syncing…" : "Sync worktrees"}
                  </Button>
                </Tooltip>
              )}
            </div>
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
            <div className="mt-3">
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
                      : "No worktrees yet. Create one via POST /api/worktree."}
                  </p>
                </div>
              )}
              {worktreesQuery.isSuccess && worktrees.length > 0 && (
                <WorkspaceList worktrees={worktrees} jira={jira} />
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

        <aside className="col-span-2 space-y-6">
          <TokenUsageTile />
          <GlobalSkillsTile />
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
