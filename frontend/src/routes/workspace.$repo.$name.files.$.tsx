import { createFileRoute, Link } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "../api/client";
import { getDiffConfig } from "../api/config";
import {
  getFileView,
  getWorktree,
  openInCursor,
} from "../api/worktrees";
import { Button } from "../components/Button";
import { FileDiffView } from "../components/FileDiffView";

export const Route = createFileRoute("/workspace/$repo/$name/files/$")({
  component: FileViewRoute,
});

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return String(err);
}

function FileViewRoute() {
  // ``_splat`` captures the splat segment(s) — the file path.
  const { repo, name, _splat } = Route.useParams();
  const filePath = decodeURIComponent(_splat ?? "");
  return <FileViewPage repo={repo} name={name} filePath={filePath} />;
}

interface FileViewPageProps {
  repo: string;
  name: string;
  filePath: string;
}

function FileViewPage({ repo, name, filePath }: FileViewPageProps) {
  const [loadAnyway, setLoadAnyway] = useState(false);
  const [expandAll, setExpandAll] = useState(false);

  const worktree = useQuery({
    queryKey: ["worktree", repo, name],
    queryFn: () => getWorktree(repo, name),
  });

  const fileView = useQuery({
    queryKey: ["file-view", repo, name, filePath, loadAnyway],
    queryFn: () => getFileView(repo, name, filePath, loadAnyway),
    enabled: filePath.length > 0,
  });

  const diffConfig = useQuery({
    queryKey: ["config", "diff"],
    queryFn: getDiffConfig,
  });

  const cursorMutation = useMutation({
    mutationFn: () => openInCursor(repo, name, filePath),
  });

  const row = worktree.data?.row;
  const fv = fileView.data;

  // ``Expand all unchanged`` is meaningful only when the file's lines
  // exceed the small-file threshold — otherwise everything renders
  // expanded already.
  const showExpandAllToggle =
    fv != null &&
    diffConfig.data != null &&
    (fv.line_count ?? 0) > diffConfig.data.expand_all_threshold;

  const githubHref =
    row?.pr_number != null && row.pr_repo && fv?.github_diff_anchor
      ? `https://github.com/${row.pr_repo}/pull/${row.pr_number}/files#diff-${fv.github_diff_anchor}`
      : null;

  return (
    <main className="mx-auto max-w-5xl p-8">
      <Link
        to="/workspace/$repo/$name"
        params={{ repo, name }}
        className="text-xs text-zinc-500 hover:text-zinc-300"
      >
        ← back to {repo} / {name}
      </Link>

      <h1 className="mt-2 break-all font-mono text-lg text-zinc-100">
        {filePath}
      </h1>

      {fv?.rename_from && (
        <p className="mt-1 text-xs text-zinc-500">
          renamed from{" "}
          <span className="font-mono text-zinc-400">{fv.rename_from}</span>
        </p>
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        <Button
          variant="secondary"
          onClick={() => cursorMutation.mutate()}
          disabled={cursorMutation.isPending}
        >
          {cursorMutation.isPending ? "Opening…" : "Open in Cursor"}
        </Button>
        {githubHref && (
          <a
            href={githubHref}
            target="_blank"
            rel="noopener noreferrer"
            className="rounded border border-zinc-700 px-3 py-1 text-xs text-zinc-200 hover:bg-zinc-800"
          >
            Open File in GitHub
          </a>
        )}
        {showExpandAllToggle && (
          <button
            type="button"
            onClick={() => setExpandAll((v) => !v)}
            className="rounded border border-zinc-700 px-3 py-1 text-xs text-zinc-200 hover:bg-zinc-800"
          >
            {expandAll ? "Collapse unchanged" : "Expand all unchanged"}
          </button>
        )}
      </div>

      {cursorMutation.error && (
        <p role="alert" className="mt-2 text-xs text-red-400">
          {errorMessage(cursorMutation.error)}
        </p>
      )}

      {/* Banners */}
      {fv && <Banners fv={fv} loadAnyway={loadAnyway} onLoadAnyway={() => setLoadAnyway(true)} />}

      {fileView.isLoading && (
        <p className="mt-6 text-sm text-zinc-500">Loading file…</p>
      )}
      {fileView.isError && (
        <p className="mt-6 text-sm text-red-400">
          Could not load file: {errorMessage(fileView.error)}
        </p>
      )}

      {fv && diffConfig.data && fv.on_disk_content != null && (
        <div className="mt-6">
          <FileDiffView
            response={fv}
            diff={diffConfig.data}
            expandAll={expandAll}
          />
        </div>
      )}
    </main>
  );
}

type FileViewData = Awaited<ReturnType<typeof getFileView>>;

function Banners({
  fv,
  loadAnyway,
  onLoadAnyway,
}: {
  fv: FileViewData;
  loadAnyway: boolean;
  onLoadAnyway: () => void;
}) {
  const banners: React.ReactElement[] = [];

  if (fv.is_binary) {
    banners.push(
      <Banner key="binary" tone="info">
        Binary file — cannot diff.
      </Banner>,
    );
  } else if (fv.is_missing) {
    banners.push(
      <Banner key="missing" tone="warn">
        File does not exist in this worktree.
      </Banner>,
    );
  } else if (fv.is_large && !loadAnyway) {
    const sizeMB =
      fv.size_bytes != null ? (fv.size_bytes / 1_048_576).toFixed(1) : "?";
    banners.push(
      <Banner key="large" tone="warn">
        Large file ({sizeMB} MB). Not rendered by default.{" "}
        <button
          type="button"
          onClick={onLoadAnyway}
          className="ml-1 underline hover:text-zinc-100"
        >
          Load anyway
        </button>
      </Banner>,
    );
  }

  if (
    !fv.is_binary &&
    !fv.is_missing &&
    !fv.file_in_pr_diff &&
    fv.on_disk_content != null
  ) {
    banners.push(
      <Banner key="not-in-pr" tone="info">
        Not modified in this PR · Viewing on-disk.
      </Banner>,
    );
  } else if (
    !fv.branch_matches_pr &&
    fv.pr_branch != null &&
    fv.workspace_branch != null
  ) {
    banners.push(
      <Banner key="branch-mismatch" tone="warn">
        Workspace is on{" "}
        <span className="font-mono">{fv.workspace_branch}</span>, this PR is
        on <span className="font-mono">{fv.pr_branch}</span>. Content shown
        reflects your current branch.
      </Banner>,
    );
  }

  if (banners.length === 0) return null;
  return <div className="mt-4 flex flex-col gap-2">{banners}</div>;
}

function Banner({
  tone,
  children,
}: {
  tone: "info" | "warn";
  children: React.ReactNode;
}) {
  const cls =
    tone === "warn"
      ? "border-amber-800 bg-amber-950/40 text-amber-200"
      : "border-zinc-800 bg-zinc-900/40 text-zinc-400";
  return (
    <div
      role="status"
      className={`rounded border px-3 py-2 text-xs ${cls}`}
    >
      {children}
    </div>
  );
}
