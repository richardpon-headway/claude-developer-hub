import { useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getJiraConfig } from "../api/config";
import { listRepos } from "../api/repos";
import { syncWorktrees } from "../api/worktrees";
import { AddRepoModal } from "../components/AddRepoModal";
import { Button } from "../components/Button";
import { AskClaudeTile } from "../components/AskClaudeTile";
import { OpenClaudeTile } from "../components/OpenClaudeTile";
import { RepoList } from "../components/RepoList";
import { TokenUsageTile } from "../components/TokenUsageTile";
import { Tooltip } from "../components/Tooltip";
import { WorkspaceBuckets } from "../components/WorkspaceBuckets";
import { TodoWidget } from "../widgets/todo/TodoWidget";

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

  const jiraQuery = useQuery({
    queryKey: ["config", "jira"],
    queryFn: getJiraConfig,
  });

  const repos = reposQuery.data ?? [];
  const jira = jiraQuery.data ?? null;

  const sync = useMutation({
    // Reconcile local worktrees against `git worktree list` (import
    // new ones, drop removed ones).
    mutationFn: syncWorktrees,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
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
              <Tooltip text="Refresh everything: discover new authored PRs, reconcile worktrees with `git worktree list`, and re-check every PR's status.">
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
          {sync.isSuccess && sync.data && (
            <p className="text-xs text-zinc-500">
              Imported {sync.data.imported.length} · removed{" "}
              {sync.data.removed.length} · re-linked{" "}
              {sync.data.relinked.length} · skipped{" "}
              {sync.data.skipped.length} · re-checked{" "}
              {sync.data.refreshed} PR{sync.data.refreshed === 1 ? "" : "s"}
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
            <p className="text-xs text-red-400">
              sync failed: {String(sync.error)}
            </p>
          )}
          <WorkspaceBuckets jira={jira} />

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
          <TodoWidget />
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
