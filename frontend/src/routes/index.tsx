import { useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getJiraConfig } from "../api/config";
import { listRepos } from "../api/repos";
import { discoverWorktrees, listWorktrees } from "../api/worktrees";
import { AddRepoModal } from "../components/AddRepoModal";
import { Button } from "../components/Button";
import { GlobalSkillsTile } from "../components/GlobalSkillsTile";
import { RepoList } from "../components/RepoList";
import { TokenUsageTile } from "../components/TokenUsageTile";
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

  const discover = useMutation({
    mutationFn: discoverWorktrees,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["worktrees"] }),
  });

  return (
    <main className="mx-auto max-w-5xl p-8">
      <header className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold">Claude Developer Hub</h1>
        <Button onClick={() => setModalOpen(true)}>Add a repo</Button>
      </header>

      <div className="mt-8 grid grid-cols-3 gap-6">
        <div className="col-span-2 space-y-8">
          <section>
            <div className="flex items-baseline justify-between">
              <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
                Workspaces
              </h2>
              {repos.length > 0 && (
                <Button
                  variant="secondary"
                  onClick={() => discover.mutate()}
                  disabled={discover.isPending}
                >
                  {discover.isPending ? "Discovering…" : "Discover worktrees"}
                </Button>
              )}
            </div>
            {discover.isSuccess && discover.data && (
              <p className="mt-2 text-xs text-zinc-500">
                Imported {discover.data.imported.length} · skipped{" "}
                {discover.data.skipped.length}
                {discover.data.skipped.length > 0 && (
                  <>
                    {" "}
                    <span className="text-zinc-600">
                      ({Array.from(new Set(discover.data.skipped.map((s) => s.reason))).join(", ")})
                    </span>
                  </>
                )}
              </p>
            )}
            {discover.isError && (
              <p className="mt-2 text-xs text-red-400">
                discover failed: {String(discover.error)}
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

        <aside className="space-y-6">
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
