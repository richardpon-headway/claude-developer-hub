import { useMutation, useQuery } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { getGlobalSkills, runGlobalSkill } from "../api/config";
import { Button } from "./Button";
import { Tooltip } from "./Tooltip";

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return String(err);
}

// TODO(inline-output): the v1 button is one-shot — it spawns iTerm2 and
// forgets. A future iteration could tail the Claude session jsonl
// (~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl) and surface
// the assistant's reply inline. Reuses the sidecar/discovery plumbing.
export function GlobalSkillsTile() {
  const skillsQuery = useQuery({
    queryKey: ["config", "skills"],
    queryFn: getGlobalSkills,
  });

  const mutation = useMutation({
    mutationFn: (skill: string) => runGlobalSkill(skill),
  });

  const skills = skillsQuery.data ?? [];
  if (skills.length === 0) return null;

  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
      <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
        Global skills
      </h3>
      <div className="mt-3 flex flex-col gap-2">
        {skills.map((s) => (
          <Tooltip key={s.name} text={s.description ?? null}>
            <Button
              variant="secondary"
              onClick={() => mutation.mutate(s.name)}
              disabled={mutation.isPending}
              className="w-full"
            >
              {mutation.isPending && mutation.variables === s.name
                ? "Opening…"
                : s.label}
            </Button>
          </Tooltip>
        ))}
      </div>
      {mutation.error && (
        <p role="alert" className="mt-2 text-xs text-red-400">
          {errorMessage(mutation.error)}
        </p>
      )}
    </section>
  );
}
