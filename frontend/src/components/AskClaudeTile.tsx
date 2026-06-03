import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { openGlobalClaude, runGlobalFreeform } from "../api/config";
import { useTerminalInfo } from "../api/terminal";
import { Button } from "./Button";
import { Tooltip } from "./Tooltip";

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return String(err);
}

// TODO(inline-output): the v1 buttons are one-shot — they spawn a
// terminal window and forget. A future iteration could tail the
// Claude session jsonl (~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl)
// and surface the assistant's reply inline. Reuses the sidecar/discovery
// plumbing.
export function AskClaudeTile() {
  const terminal = useTerminalInfo();

  const [freeformInput, setFreeformInput] = useState("");
  const freeformMutation = useMutation({
    mutationFn: (prompt: string) => runGlobalFreeform(prompt),
    onSuccess: () => setFreeformInput(""),
  });

  const openMutation = useMutation({
    mutationFn: () => openGlobalClaude(),
  });

  const trimmed = freeformInput.trim();
  const freeformDisabled = trimmed.length === 0 || freeformMutation.isPending;

  const onFreeformSubmit = () => {
    if (freeformDisabled) return;
    freeformMutation.mutate(trimmed);
  };

  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
      <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
        Ask Claude
      </h3>
      {/* Free-form prompt input — opens the configured terminal at
          config.development_root with `claude '<input>'` as the
          initial message. */}
      <div className="mt-3">
        <label htmlFor="global-freeform-prompt" className="sr-only">
          Ask Claude
        </label>
        <div className="flex items-start gap-2">
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
        <div className="mt-3 flex justify-end">
          <Tooltip text={`Opens ${terminal.display_name} in your development_root with a fresh \`claude\` session (no prompt, no repo context).`}>
            <Button
              variant="secondary"
              onClick={() => openMutation.mutate()}
              disabled={openMutation.isPending}
            >
              {openMutation.isPending ? "Opening…" : "Open Claude"}
            </Button>
          </Tooltip>
        </div>
        {openMutation.error && (
          <p role="alert" className="mt-2 text-right text-xs text-red-400">
            {errorMessage(openMutation.error)}
          </p>
        )}
      </div>
    </section>
  );
}
