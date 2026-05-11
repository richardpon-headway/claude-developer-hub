import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { onboardRepo, getOnboardStatus } from "../api/repos";
import type { OnboardResponse } from "../api/types";
import { Button } from "./Button";
import { Dialog } from "./Dialog";

interface Props {
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}

export function AddRepoModal({ open, onClose, onSaved }: Props) {
  const [path, setPath] = useState("");
  const [session, setSession] = useState<OnboardResponse | null>(null);
  const [copyConfirmed, setCopyConfirmed] = useState(false);

  const onboard = useMutation({
    mutationFn: (p: string) => onboardRepo(p),
    onSuccess: (data) => setSession(data),
  });

  // Poll /onboard/{sid} every second while awaiting Claude. Disabled when no
  // active session, so closing the modal stops polling immediately.
  const status = useQuery({
    queryKey: ["onboard-status", session?.session_id],
    queryFn: () => getOnboardStatus(session!.session_id),
    enabled: open && session !== null,
    refetchInterval: 1000,
    refetchIntervalInBackground: false,
  });

  function handleClose() {
    setPath("");
    setSession(null);
    setCopyConfirmed(false);
    onboard.reset();
    onClose();
  }

  // When backend reports the entry was saved (Claude POSTed /complete and the
  // entry passed validation), notify the parent so the repo list refetches,
  // then reset and dismiss. Effect, not render-side, to avoid loops.
  useEffect(() => {
    if (status.data?.state === "saved") {
      onSaved();
      handleClose();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status.data?.state]);

  async function handleCopy() {
    if (!session) return;
    await navigator.clipboard.writeText(session.prompt);
    setCopyConfirmed(true);
    setTimeout(() => setCopyConfirmed(false), 1500);
  }

  const onboardError =
    onboard.error instanceof ApiError ? onboard.error.detail : onboard.error?.message;

  return (
    <Dialog open={open} onClose={handleClose} title="Add a repo">
      {!session && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            onboard.mutate(path);
          }}
          className="space-y-3"
        >
          <label className="block text-sm text-zinc-400">
            Absolute path to a git repository
          </label>
          <input
            type="text"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="/Users/you/development/some-repo"
            className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus:border-indigo-500 focus:outline-none"
            autoFocus
            spellCheck={false}
          />
          {onboardError && (
            <p role="alert" className="text-sm text-red-400">
              {onboardError}
            </p>
          )}
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="secondary" onClick={handleClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={!path || onboard.isPending}>
              {onboard.isPending ? "Inspecting…" : "Inspect"}
            </Button>
          </div>
        </form>
      )}

      {session && (
        <div className="space-y-4">
          <div>
            <p className="text-sm text-zinc-300">
              Paste the prompt below into a Claude Code terminal session.
              Claude will inspect the repo and post the proposed entry back to
              CDH. This dialog will close automatically once the entry is
              saved.
            </p>
          </div>

          <div className="relative">
            <pre className="max-h-72 overflow-auto rounded border border-zinc-800 bg-zinc-950 p-3 font-mono text-xs text-zinc-200 whitespace-pre-wrap">
              {session.prompt}
            </pre>
            <button
              type="button"
              onClick={handleCopy}
              className="absolute right-2 top-2 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-800"
            >
              {copyConfirmed ? "Copied" : "Copy"}
            </button>
          </div>

          <div className="flex items-center justify-between">
            <span className="text-sm text-zinc-400">
              Waiting for Claude to complete onboarding…
            </span>
            <Button type="button" variant="secondary" onClick={handleClose}>
              Close
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}
