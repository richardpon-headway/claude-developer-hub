import { useState } from "react";
import { createFileRoute } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { listRepos } from "../api/repos";
import { AddRepoModal } from "../components/AddRepoModal";
import { Button } from "../components/Button";
import { RepoList } from "../components/RepoList";

export const Route = createFileRoute("/")({
  component: HubPage,
});

function HubPage() {
  const [modalOpen, setModalOpen] = useState(false);
  const queryClient = useQueryClient();

  const reposQuery = useQuery({
    queryKey: ["repos"],
    queryFn: listRepos,
  });

  const repos = reposQuery.data ?? [];

  return (
    <main className="mx-auto max-w-4xl p-8">
      <header className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold">Claude Developer Hub</h1>
        <Button onClick={() => setModalOpen(true)}>Add a repo</Button>
      </header>

      <section className="mt-8">
        <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
          Repos
        </h2>
        <div className="mt-3">
          {reposQuery.isLoading && <p className="text-sm text-zinc-500">Loading…</p>}
          {reposQuery.isError && (
            <p className="text-sm text-red-400">
              Failed to load repos. Is the backend running on :47823?
            </p>
          )}
          {reposQuery.isSuccess && repos.length === 0 && (
            <div className="rounded-lg border border-dashed border-zinc-700 p-6 text-center">
              <p className="text-sm text-zinc-400">No repos configured yet.</p>
              <div className="mt-3">
                <Button onClick={() => setModalOpen(true)}>Add your first repo</Button>
              </div>
            </div>
          )}
          {reposQuery.isSuccess && repos.length > 0 && <RepoList repos={repos} />}
        </div>
      </section>

      <AddRepoModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onSaved={() => queryClient.invalidateQueries({ queryKey: ["repos"] })}
      />
    </main>
  );
}
