import { useMutation } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { openGlobalClaude } from "../api/config";
import { useTerminalInfo } from "../api/terminal";
import { Button } from "./Button";
import { Tooltip } from "./Tooltip";

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return String(err);
}

/**
 * A blank Claude session launcher — opens Claude in the configured
 * development_root with no prompt and no repo/worktree context. Lives
 * in its own tile (separate from Ask Claude) so the "just start a
 * session" action is visually distinct from the free-form prompt box.
 */
export function OpenClaudeTile() {
  const terminal = useTerminalInfo();

  const openMutation = useMutation({
    mutationFn: () => openGlobalClaude(),
  });

  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
      <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
        Open Claude Terminal
      </h3>
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
    </section>
  );
}
