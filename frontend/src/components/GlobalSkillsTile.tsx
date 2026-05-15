import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { getGlobalSkills, runGlobalFreeform, runGlobalSkill } from "../api/config";
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

  const [freeformInput, setFreeformInput] = useState("");
  const freeformMutation = useMutation({
    mutationFn: (prompt: string) => runGlobalFreeform(prompt),
    onSuccess: () => setFreeformInput(""),
  });

  const skills = skillsQuery.data ?? [];

  const trimmed = freeformInput.trim();
  const freeformDisabled = trimmed.length === 0 || freeformMutation.isPending;

  const onFreeformSubmit = () => {
    if (freeformDisabled) return;
    freeformMutation.mutate(trimmed);
  };

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

      {/* Free-form prompt input — same plumbing as the buttons above
          but accepts arbitrary user-typed text instead of a named
          skill. Opens iTerm2 at config.development_root with
          `claude '<input>'` as the initial message. */}
      <div className="mt-4 border-t border-zinc-800 pt-3">
        <label
          htmlFor="global-freeform-prompt"
          className="block text-[11px] uppercase tracking-wide text-zinc-500"
        >
          Ask Claude
        </label>
        <div className="mt-2 flex gap-2">
          <input
            id="global-freeform-prompt"
            type="text"
            value={freeformInput}
            onChange={(e) => setFreeformInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") onFreeformSubmit();
            }}
            disabled={freeformMutation.isPending}
            placeholder="What should we work on?"
            className="min-w-0 flex-1 rounded border border-zinc-700 bg-zinc-950 px-2 py-1 text-sm text-zinc-100 placeholder:text-zinc-600 focus:border-zinc-500 focus:outline-none disabled:opacity-50"
          />
          <Tooltip text="Opens iTerm2 in your development_root with `claude '<your input>'` as the first message.">
            <Button
              variant="secondary"
              onClick={onFreeformSubmit}
              disabled={freeformDisabled}
            >
              {freeformMutation.isPending ? "Opening…" : "Run"}
            </Button>
          </Tooltip>
        </div>
        {freeformMutation.error && (
          <p role="alert" className="mt-2 text-xs text-red-400">
            {errorMessage(freeformMutation.error)}
          </p>
        )}
      </div>
    </section>
  );
}
