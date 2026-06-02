import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import {
  getGlobalSkills,
  openGlobalClaude,
  runGlobalFreeform,
  runGlobalSkill,
} from "../api/config";
import { useTerminalInfo } from "../api/terminal";
import { Button } from "./Button";
import { Tooltip } from "./Tooltip";

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return String(err);
}

// TODO(inline-output): the v1 button is one-shot — it spawns a
// terminal window and forgets. A future iteration could tail the
// Claude session jsonl (~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl)
// and surface the assistant's reply inline. Reuses the sidecar/discovery
// plumbing.
export function GlobalSkillsTile() {
  const terminal = useTerminalInfo();
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

  const openMutation = useMutation({
    mutationFn: () => openGlobalClaude(),
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
          skill. Opens the configured terminal at
          config.development_root with `claude '<input>'` as the
          initial message. */}
      <div className="mt-4 border-t border-zinc-800 pt-3">
        <label
          htmlFor="global-freeform-prompt"
          className="block text-[11px] uppercase tracking-wide text-zinc-500"
        >
          Ask Claude
        </label>
        <div className="mt-2 flex items-start gap-2">
          <textarea
            id="global-freeform-prompt"
            value={freeformInput}
            onChange={(e) => setFreeformInput(e.target.value)}
            onKeyDown={(e) => {
              // Multi-line input: Enter inserts a newline (default
              // textarea behavior). Cmd+Enter (macOS) or Ctrl+Enter
              // (cross-platform) submits, matching the chat-input
              // convention in most modern apps.
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                onFreeformSubmit();
              }
            }}
            disabled={freeformMutation.isPending}
            rows={3}
            placeholder="What should we work on?  (⌘↵ to send)"
            className="min-w-0 flex-1 resize-y rounded border border-zinc-700 bg-zinc-950 px-2 py-1 text-sm text-zinc-100 placeholder:text-zinc-600 focus:border-zinc-500 focus:outline-none disabled:opacity-50"
          />
          <Tooltip text={`Opens ${terminal.display_name} in your development_root with \`claude '<your input>'\` as the first message. ⌘↵ to submit from the textarea.`}>
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
        {/* Blank session — open Claude in development_root with no
            prompt at all. For when you just want a fresh session to
            type into, with no repo/worktree context. */}
        <div className="mt-3">
          <Tooltip text={`Opens ${terminal.display_name} in your development_root with a fresh \`claude\` session (no prompt, no repo context).`}>
            <Button
              variant="secondary"
              onClick={() => openMutation.mutate()}
              disabled={openMutation.isPending}
              className="w-full"
            >
              {openMutation.isPending ? "Opening…" : "Open Claude"}
            </Button>
          </Tooltip>
          {openMutation.error && (
            <p role="alert" className="mt-2 text-xs text-red-400">
              {errorMessage(openMutation.error)}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
