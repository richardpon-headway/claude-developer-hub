import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";

import { getWorktree } from "../api/worktrees";

export const Route = createFileRoute("/workspace/$repo/$name")({
  component: WorkspacePage,
});

function WorkspacePage() {
  const { repo, name } = Route.useParams();
  const query = useQuery({
    queryKey: ["worktree", repo, name],
    queryFn: () => getWorktree(repo, name),
    refetchInterval: 5_000,
  });

  return (
    <main className="mx-auto max-w-3xl p-8">
      <Link to="/" className="text-xs text-zinc-500 hover:text-zinc-300">
        ← back to hub
      </Link>
      <h1 className="mt-2 text-2xl font-semibold">
        {repo} / <span className="text-zinc-400">{name}</span>
      </h1>

      {query.isLoading && <p className="mt-6 text-sm text-zinc-500">Loading…</p>}
      {query.isError && (
        <p className="mt-6 text-sm text-red-400">
          Workspace not found, or backend unreachable.
        </p>
      )}
      {query.isSuccess && (
        <>
          <dl className="mt-6 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
            <dt className="text-zinc-500">branch</dt>
            <dd className="text-zinc-200">{query.data.row.branch}</dd>
            <dt className="text-zinc-500">status</dt>
            <dd className="text-zinc-200">{query.data.row.status}</dd>
            <dt className="text-zinc-500">path</dt>
            <dd className="font-mono text-xs text-zinc-300">{query.data.row.path}</dd>
            {query.data.row.ticket && (
              <>
                <dt className="text-zinc-500">ticket</dt>
                <dd className="text-zinc-200">{query.data.row.ticket}</dd>
              </>
            )}
            <dt className="text-zinc-500">claude session</dt>
            <dd className="text-zinc-200">
              {query.data.row.has_claude_session ? "open" : "—"}
            </dd>
          </dl>

          {query.data.log.length > 0 && (
            <section className="mt-8">
              <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                Setup log
              </h2>
              <pre className="mt-2 max-h-96 overflow-auto rounded border border-zinc-800 bg-zinc-950 p-3 font-mono text-xs text-zinc-200 whitespace-pre-wrap">
                {query.data.log.join("\n")}
              </pre>
            </section>
          )}

          <p className="mt-8 text-xs text-zinc-500">
            Action buttons (Open in iTerm2, skill-runner, …) land in the next
            slice.
          </p>
        </>
      )}
    </main>
  );
}
