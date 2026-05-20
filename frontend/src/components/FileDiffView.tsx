import { useMemo, useState } from "react";

import type { DiffConfig } from "../api/config";
import type {
  FileViewLineKind,
  FileViewResponse,
} from "../api/worktrees";

/** A single rendered row in the diff view.
 *
 * Context lines have ``lineno != null``; ghost removes (``*_remove``)
 * are inserted between context lines and carry ``lineno = null``.
 */
interface RenderedLine {
  kind: FileViewLineKind;
  content: string;
  lineno: number | null;
}

/** A run of consecutive lines sharing the same kind. Determines the
 *  colored border block on screen. */
interface DiffBlock {
  kind: FileViewLineKind;
  lines: RenderedLine[];
}

/** A collapsed run of unchanged context — replaces a long stretch of
 *  ``kind: "context"`` blocks with one expander row. Click to expand. */
interface CollapsedBlock {
  kind: "collapsed";
  hiddenLineCount: number;
  hiddenStartLineno: number;
  hiddenEndLineno: number;
  expanded: false;
}

type Block = DiffBlock | CollapsedBlock;

function isCollapsed(b: Block): b is CollapsedBlock {
  return b.kind === "collapsed";
}

/** Walk on-disk content + hunks and produce a flat per-line list, with
 *  ghost-removes inserted at each hunk's anchor on-disk lineno. */
function buildLines(response: FileViewResponse): RenderedLine[] {
  if (response.on_disk_content == null) return [];
  const raw = response.on_disk_content;
  const onDisk = raw.split("\n");
  // ``split('\n')`` on a trailing-newline string yields a final empty
  // entry — drop it so line count matches the user's mental model.
  if (onDisk.length > 0 && onDisk[onDisk.length - 1] === "") onDisk.pop();

  // line-number → kind for any line classified by a hunk
  const kindByLine = new Map<number, FileViewLineKind>();
  // line-number → list of removes that should appear BEFORE that line
  const removesBefore = new Map<
    number,
    { kind: FileViewLineKind; content: string }[]
  >();

  for (const hunk of response.hunks) {
    for (const line of hunk.lines) {
      if (line.kind === "committed_add" || line.kind === "uncommitted_add") {
        if (line.on_disk_lineno != null) {
          const existing = kindByLine.get(line.on_disk_lineno);
          // Uncommitted wins over committed at the same line — matches
          // Q8 in plan-46 (differs from HEAD → uncommitted color).
          if (
            !existing ||
            (existing.startsWith("committed") &&
              line.kind.startsWith("uncommitted"))
          ) {
            kindByLine.set(line.on_disk_lineno, line.kind);
          }
        }
      } else if (
        line.kind === "committed_remove" ||
        line.kind === "uncommitted_remove"
      ) {
        const anchor = hunk.on_disk_start;
        if (!removesBefore.has(anchor)) removesBefore.set(anchor, []);
        removesBefore.get(anchor)!.push({
          kind: line.kind,
          content: line.content,
        });
      }
    }
  }

  const out: RenderedLine[] = [];
  for (let i = 0; i < onDisk.length; i++) {
    const lineno = i + 1;
    const removes = removesBefore.get(lineno);
    if (removes) {
      for (const r of removes) {
        out.push({ kind: r.kind, content: r.content, lineno: null });
      }
    }
    out.push({
      kind: kindByLine.get(lineno) ?? "context",
      content: onDisk[i],
      lineno,
    });
  }
  // Anchors past EOF (rare; trailing pure-remove hunks): drop their
  // ghosts at the end so the user still sees them.
  const lastLineno = onDisk.length;
  for (const [anchor, removes] of removesBefore.entries()) {
    if (anchor > lastLineno) {
      for (const r of removes) {
        out.push({ kind: r.kind, content: r.content, lineno: null });
      }
    }
  }
  return out;
}

/** Group consecutive lines sharing the same ``kind`` into one block. */
function groupBlocks(lines: RenderedLine[]): DiffBlock[] {
  const blocks: DiffBlock[] = [];
  for (const line of lines) {
    const last = blocks[blocks.length - 1];
    if (last && last.kind === line.kind) {
      last.lines.push(line);
    } else {
      blocks.push({ kind: line.kind, lines: [line] });
    }
  }
  return blocks;
}

/** Insert ``collapsed`` placeholders into long stretches of unchanged
 *  context blocks, keeping ``contextLines`` of context around each
 *  change block at the edges. Short files (≤ ``expandAllThreshold``
 *  total on-disk lines) skip collapsing entirely. */
function applyCollapse(
  blocks: DiffBlock[],
  diff: DiffConfig,
  totalLines: number,
): Block[] {
  if (totalLines <= diff.expand_all_threshold) return blocks;

  const collapseThreshold = diff.default_context_lines * 2;
  const out: Block[] = [];

  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i];
    if (b.kind !== "context" || b.lines.length <= collapseThreshold) {
      out.push(b);
      continue;
    }
    const isFirst = i === 0;
    const isLast = i === blocks.length - 1;
    const head = isFirst ? 0 : diff.default_context_lines;
    const tail = isLast ? 0 : diff.default_context_lines;
    if (head + tail >= b.lines.length) {
      out.push(b);
      continue;
    }
    if (head > 0) {
      out.push({ kind: "context", lines: b.lines.slice(0, head) });
    }
    const hidden = b.lines.slice(head, b.lines.length - tail);
    const hiddenStart = hidden[0].lineno ?? 0;
    const hiddenEnd = hidden[hidden.length - 1].lineno ?? 0;
    out.push({
      kind: "collapsed",
      hiddenLineCount: hidden.length,
      hiddenStartLineno: hiddenStart,
      hiddenEndLineno: hiddenEnd,
      expanded: false,
    });
    if (tail > 0) {
      out.push({
        kind: "context",
        lines: b.lines.slice(b.lines.length - tail),
      });
    }
  }
  return out;
}

// Diff-color CSS for each line kind. Border colors apply to the wrapping
// block <div>; background tints apply per line.
const KIND_STYLES: Record<
  FileViewLineKind,
  { border: string; bg: string; sigil: string }
> = {
  context: { border: "", bg: "", sigil: " " },
  committed_add: {
    border: "border-green-900",
    bg: "bg-green-500/10",
    sigil: "+",
  },
  committed_remove: {
    border: "border-red-900",
    bg: "bg-red-500/10",
    sigil: "−",
  },
  uncommitted_add: {
    border: "border-amber-700",
    bg: "bg-amber-500/10",
    sigil: "+",
  },
  uncommitted_remove: {
    border: "border-orange-800",
    bg: "bg-orange-500/10",
    sigil: "−",
  },
};

interface Props {
  response: FileViewResponse;
  diff: DiffConfig;
  expandAll: boolean;
}

/** Renders the unified diff view: per-line content, classified colors,
 *  block borders, collapsed unchanged-region expanders, and the
 *  click-to-toggle line-number gutter. */
export function FileDiffView({ response, diff, expandAll }: Props) {
  // Gutter state: visible by default; click to toggle hover-only.
  // Not persisted across page loads (Q11 in plan-46).
  const [gutterAlwaysVisible, setGutterAlwaysVisible] = useState(true);

  // Per-collapsed-block expansion state. Key = stable index based on the
  // block's hidden range; expanded blocks render their hidden lines.
  const [expandedCollapses, setExpandedCollapses] = useState<Set<number>>(
    () => new Set(),
  );

  const { blocks, allLines } = useMemo(() => {
    const lines = buildLines(response);
    const grouped = groupBlocks(lines);
    const totalLines = response.line_count ?? 0;
    const final = expandAll
      ? (grouped as Block[])
      : applyCollapse(grouped, diff, totalLines);
    return { blocks: final, allLines: grouped };
  }, [response, diff, expandAll]);

  if (response.on_disk_content == null) {
    // Banners (binary / large / missing) are rendered by the route file.
    // This component just returns nothing for the body in those cases.
    return null;
  }

  const gutterClass = gutterAlwaysVisible
    ? "opacity-100"
    : "opacity-0 group-hover:opacity-100 transition-opacity";

  return (
    <div className="group relative font-mono text-xs">
      <div
        className="flex flex-col rounded border border-zinc-800 bg-zinc-950"
        role="region"
        aria-label={`File diff for ${response.path}`}
      >
        {blocks.map((block, idx) => {
          if (isCollapsed(block)) {
            const expanded = expandedCollapses.has(idx);
            if (expanded) {
              // Render the hidden lines that this collapse was masking.
              // We reach back into the ungrouped list by walking allLines
              // and finding the matching range — but for simplicity we
              // just re-fetch from blocks state. For v1 we trust the
              // stable-index keying.
              return (
                <ExpandedFormerly
                  key={`expanded-${idx}`}
                  block={block}
                  blocks={allLines}
                  gutterAlwaysVisible={gutterAlwaysVisible}
                />
              );
            }
            return (
              <button
                key={`collapsed-${idx}`}
                type="button"
                onClick={() =>
                  setExpandedCollapses((prev) => {
                    const next = new Set(prev);
                    next.add(idx);
                    return next;
                  })
                }
                className="border-y border-zinc-800 bg-zinc-900/50 px-3 py-1 text-left text-[11px] text-zinc-500 hover:bg-zinc-900 hover:text-zinc-300"
              >
                ⋯ {block.hiddenLineCount} unchanged lines (
                {block.hiddenStartLineno}–{block.hiddenEndLineno}) — click
                to expand
              </button>
            );
          }
          const styles = KIND_STYLES[block.kind];
          const borderClass = styles.border
            ? `border-l-2 ${styles.border}`
            : "border-l-2 border-transparent";
          return (
            <div key={`block-${idx}`} className={borderClass}>
              {block.lines.map((line, li) => (
                <LineRow
                  key={li}
                  line={line}
                  gutterClass={gutterClass}
                  styles={styles}
                />
              ))}
            </div>
          );
        })}
      </div>
      {/* The click-to-toggle area is the gutter itself, which sits
          absolutely positioned over the left edge. Mouse-over reveals
          line numbers when toggled to hover-only. */}
      <button
        type="button"
        aria-label={
          gutterAlwaysVisible
            ? "Hide line numbers (hover-to-reveal)"
            : "Show line numbers"
        }
        onClick={() => setGutterAlwaysVisible((v) => !v)}
        className="absolute left-0 top-0 h-full w-12 cursor-pointer"
        title={
          gutterAlwaysVisible
            ? "Click: hide line numbers"
            : "Click: show line numbers"
        }
      />
    </div>
  );
}

function LineRow({
  line,
  gutterClass,
  styles,
}: {
  line: RenderedLine;
  gutterClass: string;
  styles: (typeof KIND_STYLES)[FileViewLineKind];
}) {
  return (
    <div className={`flex items-baseline ${styles.bg}`}>
      <span
        className={`inline-block w-12 select-none pr-2 text-right text-[10px] tabular-nums text-zinc-600 ${gutterClass}`}
      >
        {line.lineno ?? ""}
      </span>
      <span
        aria-hidden="true"
        className="inline-block w-3 select-none text-center text-zinc-600"
      >
        {styles.sigil}
      </span>
      <span className="whitespace-pre text-zinc-200">{line.content}</span>
    </div>
  );
}

/** Renders the hidden lines a collapsed block was masking. ``blocks``
 *  is the pre-collapse grouped list; we find the run of context lines
 *  whose first lineno matches and render them. */
function ExpandedFormerly({
  block,
  blocks,
  gutterAlwaysVisible,
}: {
  block: CollapsedBlock;
  blocks: DiffBlock[];
  gutterAlwaysVisible: boolean;
}) {
  // Find the original context block this collapse came from. Match by
  // any line in its hidden range.
  const source = blocks.find(
    (b) =>
      b.kind === "context" &&
      b.lines.some(
        (l) =>
          l.lineno != null &&
          l.lineno >= block.hiddenStartLineno &&
          l.lineno <= block.hiddenEndLineno,
      ),
  );
  if (!source) return null;
  const styles = KIND_STYLES.context;
  const gutterClass = gutterAlwaysVisible
    ? "opacity-100"
    : "opacity-0 group-hover:opacity-100 transition-opacity";
  return (
    <div className="border-l-2 border-transparent">
      {source.lines
        .filter(
          (l) =>
            l.lineno != null &&
            l.lineno >= block.hiddenStartLineno &&
            l.lineno <= block.hiddenEndLineno,
        )
        .map((line, li) => (
          <LineRow
            key={li}
            line={line}
            gutterClass={gutterClass}
            styles={styles}
          />
        ))}
    </div>
  );
}
