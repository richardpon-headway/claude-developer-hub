import type { RepoConfig } from "../api/types";

interface Props {
  repos: RepoConfig[];
}

export function RepoList({ repos }: Props) {
  if (repos.length === 0) return null;

  return (
    <ul className="divide-y divide-zinc-800 rounded-lg border border-zinc-800 bg-zinc-900/50">
      {repos.map((repo) => (
        <li key={repo.name} className="px-4 py-3">
          <div className="flex items-baseline justify-between gap-4">
            <span className="text-sm font-medium text-zinc-100">{repo.name}</span>
            <span className="font-mono text-xs text-zinc-500">{repo.path}</span>
          </div>
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-500">
            <span>branch: {repo.default_branch}</span>
            <span>
              setup steps: {repo.setup_steps.length}
              {repo.setup_steps.length > 0 && (
                <span className="ml-1 text-zinc-600">
                  ({repo.setup_steps.map((s) => s.cmd).join(", ")})
                </span>
              )}
            </span>
            {repo.ticket_pattern && (
              <span>tickets: <code className="text-zinc-400">{repo.ticket_pattern}</code></span>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}
