import { Tooltip } from "./Tooltip";

interface Props {
  url: string;
}

/**
 * "PR" button that opens a GitHub PR URL in a new tab. Use on row
 * surfaces (inbox, bookmark, authored-PR tier) where the URL is
 * already known — symmetric with the worktree-row PR button on
 * `WorkspaceList`, just without the API round-trip since these
 * surfaces always carry the URL on the row.
 *
 * Rendered as a button rather than an anchor so it visually matches
 * the other action buttons (Pull-down, Remove, Unbookmark).
 */
export function OpenPrLinkButton({ url }: Props) {
  return (
    <Tooltip text="Open the GitHub PR in a new tab.">
      <a
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex shrink-0 items-center rounded border border-zinc-700 bg-zinc-800 px-3 py-1 text-xs text-zinc-200 hover:bg-zinc-700"
      >
        PR
      </a>
    </Tooltip>
  );
}
