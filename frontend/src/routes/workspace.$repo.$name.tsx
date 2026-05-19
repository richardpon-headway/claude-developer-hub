import { createFileRoute, Link } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { getWorkspaceSkills } from "../api/config";
import {
  getPrFiles,
  getWorktree,
  openInCursor,
  runSkill,
  sendText,
  spawnIterm,
} from "../api/worktrees";
import { Button } from "../components/Button";
import { Tooltip } from "../components/Tooltip";
import { WorkspaceNotes } from "../components/WorkspaceNotes";

export const Route = createFileRoute("/workspace/$repo/$name")({
  component: WorkspaceRoute,
});

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return String(err);
}

function WorkspaceRoute() {
  const { repo, name } = Route.useParams();
  return <WorkspacePage repo={repo} name={name} />;
}

interface WorkspacePageProps {
  repo: string;
  name: string;
}

export function WorkspacePage({ repo, name }: WorkspacePageProps) {
  const queryClient = useQueryClient();

  const detail = useQuery({
    queryKey: ["worktree", repo, name],
    queryFn: () => getWorktree(repo, name),
    refetchInterval: 5_000,
  });

  const row = detail.data?.row;
  const hasClaude = row?.has_claude_session ?? false;
  // Action buttons gate on `usable`: code is on disk and workable,
  // regardless of whether all setup_steps succeeded. `code_on_disk`
  // rows are usable (only setup automation failed, not the worktree
  // creation itself).
  const usable =
    row?.status === "ready" || row?.status === "code_on_disk";

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["worktree", repo, name] });

  const spawnMutation = useMutation({
    mutationFn: () => spawnIterm(repo, name),
    onSuccess: invalidate,
  });

  const skillsQuery = useQuery({
    queryKey: ["config", "workspace-skills"],
    queryFn: getWorkspaceSkills,
  });
  const skills = skillsQuery.data ?? [];

  const skillMutation = useMutation({
    mutationFn: (skill: string) => runSkill(repo, name, skill),
  });

  const sendMutation = useMutation({
    mutationFn: (text: string) => sendText(repo, name, text),
  });

  const cursorMutation = useMutation({
    mutationFn: () => openInCursor(repo, name),
  });

  const cursorFileMutation = useMutation({
    mutationFn: (file: string) => openInCursor(repo, name, file),
  });

  const prFiles = useQuery({
    queryKey: ["pr-files", repo, name],
    queryFn: () => getPrFiles(repo, name),
    enabled: usable,
    staleTime: 60_000,
  });

  return (
    <main className="mx-auto max-w-5xl p-8">
      <Link to="/" className="text-xs text-zinc-500 hover:text-zinc-300">
        ← back to hub
      </Link>
      <h1 className="mt-2 text-2xl font-semibold">
        {repo} / <span className="text-zinc-400">{name}</span>
      </h1>

      {detail.isLoading && <p className="mt-6 text-sm text-zinc-500">Loading…</p>}
      {detail.isError && (
        <p className="mt-6 text-sm text-red-400">
          Workspace not found, or backend unreachable.
        </p>
      )}

      {detail.isSuccess && row && (
        <>
          <dl className="mt-6 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
            <dt className="text-zinc-500">branch</dt>
            <dd className="text-zinc-200">{row.branch}</dd>
            <dt className="text-zinc-500">status</dt>
            <dd className="text-zinc-200">{row.status}</dd>
            <dt className="text-zinc-500">path</dt>
            <dd className="font-mono text-xs text-zinc-300">{row.path}</dd>
            {row.ticket && (
              <>
                <dt className="text-zinc-500">ticket</dt>
                <dd className="text-zinc-200">{row.ticket}</dd>
              </>
            )}
            <dt className="text-zinc-500">claude session</dt>
            <dd className="text-zinc-200">{hasClaude ? "open" : "—"}</dd>
          </dl>

          {row.status === "code_on_disk" && (
            <div
              role="status"
              className="mt-6 rounded border border-amber-800 bg-amber-950/40 px-3 py-2 text-xs text-amber-200"
            >
              Setup didn't complete (see log below), but the code is
              on disk. You can open the worktree in iTerm2 or Cursor
              and re-run the failing step manually. Click{" "}
              <em>Recreate</em> on the hub if you want CDH to wipe +
              re-run setup from scratch.
            </div>
          )}

          <section className="mt-8">
            <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
              Notes
            </h2>
            <div className="mt-2">
              <WorkspaceNotes
                repo={repo}
                name={name}
                notes={row.notes}
                variant="full"
              />
            </div>
          </section>

          <section className="mt-8 space-y-4">
            <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
              Actions
            </h2>
            <div className="flex flex-wrap gap-2">
              <Tooltip
                text={
                  !usable
                    ? `worktree status is ${row.status}; nothing to spawn into`
                    : null
                }
              >
                <Button
                  onClick={() => spawnMutation.mutate()}
                  disabled={spawnMutation.isPending || !usable}
                >
                  {spawnMutation.isPending ? "Opening…" : "Open in iTerm2"}
                </Button>
              </Tooltip>
              <Button
                variant={cursorMutation.error ? "danger" : "secondary"}
                onClick={() => cursorMutation.mutate()}
                disabled={cursorMutation.isPending}
              >
                {cursorMutation.isPending
                  ? "Opening…"
                  : cursorMutation.error
                    ? "Open in Cursor ✗"
                    : "Open in Cursor"}
              </Button>
              {skills.map((skill) => (
                <Tooltip
                  key={skill.name}
                  text={
                    !usable
                      ? `worktree status is ${row.status}; nothing to run into`
                      : !hasClaude
                        ? skill.description
                          ? `${skill.description} — spawns iTerm2 first`
                          : "Spawns iTerm2 and runs the skill"
                        : skill.description
                  }
                >
                  <Button
                    variant="secondary"
                    onClick={() => skillMutation.mutate(skill.name)}
                    disabled={!usable || skillMutation.isPending}
                  >
                    {skill.label}
                  </Button>
                </Tooltip>
              ))}
            </div>

            <SendTextForm
              onSubmit={(text) => sendMutation.mutate(text)}
              isPending={sendMutation.isPending}
            />

            {spawnMutation.error && (
              <p role="alert" className="text-sm text-red-400">
                spawn failed: {errorMessage(spawnMutation.error)}
              </p>
            )}
            {skillMutation.error && (
              <p role="alert" className="text-sm text-red-400">
                skill failed: {errorMessage(skillMutation.error)}
              </p>
            )}
            {sendMutation.error && (
              <p role="alert" className="text-sm text-red-400">
                send failed: {errorMessage(sendMutation.error)}
              </p>
            )}
            {cursorMutation.error && (
              <p
                role="alert"
                className="text-sm text-red-400"
                title={errorMessage(cursorMutation.error)}
              >
                {errorMessage(cursorMutation.error)}
              </p>
            )}
          </section>

          {prFiles.data && prFiles.data.files.length > 0 && (
            <section className="mt-8">
              <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                Files changed ({prFiles.data.files.length})
                <span
                  className="ml-2 normal-case tracking-normal text-zinc-600"
                  title="Stats computed from `git diff --numstat origin/<default_branch>...HEAD` in the worktree. Reflects your working tree, not GitHub's view of the PR."
                >
                  (via local git)
                </span>
              </h2>
              <ul className="mt-2 divide-y divide-zinc-800 rounded border border-zinc-800">
                {prFiles.data.files.map((f) => (
                  <li
                    key={f.path}
                    className="flex items-center gap-3 px-3 py-2 text-sm"
                  >
                    <span className="flex-1 break-all font-mono text-xs text-zinc-200">
                      {f.path}
                    </span>
                    <span className="text-xs tabular-nums text-green-400">
                      +{f.additions}
                    </span>
                    <span className="text-xs tabular-nums text-red-400">
                      −{f.deletions}
                    </span>
                    <button
                      type="button"
                      onClick={() => cursorFileMutation.mutate(f.path)}
                      disabled={cursorFileMutation.isPending}
                      className="rounded border border-zinc-700 px-2 py-0.5 text-xs hover:bg-zinc-800 disabled:opacity-50"
                    >
                      Cursor
                    </button>
                    {row.pr_number != null && row.pr_repo != null && (
                      <a
                        href={`https://github.com/${row.pr_repo}/pull/${row.pr_number}/files#diff-${f.github_diff_anchor}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="rounded border border-zinc-700 px-2 py-0.5 text-xs hover:bg-zinc-800"
                      >
                        GitHub
                      </a>
                    )}
                  </li>
                ))}
              </ul>
              {cursorFileMutation.error && (
                <p
                  role="alert"
                  className="mt-2 text-xs text-red-400"
                  title={errorMessage(cursorFileMutation.error)}
                >
                  {errorMessage(cursorFileMutation.error)}
                </p>
              )}
            </section>
          )}

          {detail.data.log.length > 0 && (
            <section className="mt-8">
              <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                Setup log
              </h2>
              <pre className="mt-2 max-h-96 overflow-auto rounded border border-zinc-800 bg-zinc-950 p-3 font-mono text-xs text-zinc-200 whitespace-pre-wrap">
                {detail.data.log.join("\n")}
              </pre>
            </section>
          )}
        </>
      )}
    </main>
  );
}

interface SendTextFormProps {
  isPending: boolean;
  onSubmit: (text: string) => void;
}

function SendTextForm({ isPending, onSubmit }: SendTextFormProps) {
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        const data = new FormData(e.currentTarget);
        const text = String(data.get("text") ?? "").trim();
        if (!text) return;
        onSubmit(text);
        (e.currentTarget.querySelector("textarea") as HTMLTextAreaElement).value = "";
      }}
      className="space-y-2"
    >
      <label className="block text-xs uppercase tracking-wide text-zinc-500">
        Ask Claude
      </label>
      <textarea
        name="text"
        rows={3}
        disabled={isPending}
        placeholder="What should we work on?  (⌘↵ to send)"
        onKeyDown={(e) => {
          // Match the hub's Ask-Claude convention: plain Enter inserts
          // a newline, Cmd/Ctrl+Enter submits.
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            e.currentTarget.form?.requestSubmit();
          }
        }}
        className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus:border-indigo-500 focus:outline-none disabled:opacity-50"
      />
      <div className="flex justify-end">
        <Button
          type="submit"
          variant="secondary"
          disabled={isPending}
        >
          {isPending ? "Sending…" : "Submit"}
        </Button>
      </div>
    </form>
  );
}
