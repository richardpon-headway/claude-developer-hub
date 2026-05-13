import { createFileRoute, Link } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { getWorktree, runSkill, sendText, spawnIterm } from "../api/worktrees";
import { Button } from "../components/Button";
import { Tooltip } from "../components/Tooltip";

export const Route = createFileRoute("/workspace/$repo/$name")({
  component: WorkspaceRoute,
});

const SKILLS = ["pr-finalize-for-review", "pr-check-action-required"] as const;

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
  const ready = row?.status === "ready";

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["worktree", repo, name] });

  const spawnMutation = useMutation({
    mutationFn: () => spawnIterm(repo, name),
    onSuccess: invalidate,
  });

  const skillMutation = useMutation({
    mutationFn: (skill: string) => runSkill(repo, name, skill),
  });

  const sendMutation = useMutation({
    mutationFn: (text: string) => sendText(repo, name, text),
  });

  return (
    <main className="mx-auto max-w-3xl p-8">
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

          <section className="mt-8 space-y-4">
            <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
              Actions
            </h2>
            <div className="flex flex-wrap gap-2">
              <Tooltip
                text={
                  !ready
                    ? `worktree status is ${row.status}; nothing to spawn into`
                    : null
                }
              >
                <Button
                  onClick={() => spawnMutation.mutate()}
                  disabled={spawnMutation.isPending || !ready}
                >
                  {spawnMutation.isPending ? "Opening…" : "Open in iTerm2"}
                </Button>
              </Tooltip>
              {SKILLS.map((skill) => (
                <Tooltip
                  key={skill}
                  text={
                    !ready
                      ? `worktree status is ${row.status}; nothing to run into`
                      : !hasClaude
                        ? "Spawns iTerm2 and runs the skill"
                        : null
                  }
                >
                  <Button
                    variant="secondary"
                    onClick={() => skillMutation.mutate(skill)}
                    disabled={!ready || skillMutation.isPending}
                  >
                    /{skill}
                  </Button>
                </Tooltip>
              ))}
            </div>

            <SendTextForm
              hasClaude={hasClaude}
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
          </section>

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
  hasClaude: boolean;
  isPending: boolean;
  onSubmit: (text: string) => void;
}

function SendTextForm({ hasClaude, isPending, onSubmit }: SendTextFormProps) {
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
        Send text to Claude
      </label>
      <textarea
        name="text"
        rows={2}
        disabled={!hasClaude || isPending}
        placeholder={
          hasClaude
            ? "type a message, press Submit to send + Enter"
            : "open this workspace in iTerm2 first"
        }
        className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus:border-indigo-500 focus:outline-none disabled:opacity-50"
      />
      <div className="flex justify-end">
        <Button
          type="submit"
          variant="secondary"
          disabled={!hasClaude || isPending}
        >
          {isPending ? "Sending…" : "Submit"}
        </Button>
      </div>
    </form>
  );
}
