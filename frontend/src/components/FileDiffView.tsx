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
 *  ghost-removes inserted at the right anchor on-disk lineno.
 *
 *  Anchor convention varies by hunk shape (matches git diff -U0 output):
 *  - **Mixed hunk** (has at least one add): removes precede the first
 *    add at on_disk_start, so anchor = on_disk_start (insert BEFORE
 *    that line).
 *  - **Pure-delete hunk** (no adds): the new-side header reports the
 *    "last unchanged line before the deletion" as on_disk_start, so
 *    removes belong AFTER that line. Anchor = on_disk_start + 1.
 *  - Anchor 0 (delete-at-start-of-file) and anchors past EOF
 *    (delete-at-end) are handled as top/bottom bookends. */
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
  const removesAtTop: { kind: FileViewLineKind; content: string }[] = [];
  const removesAtBottom: { kind: FileViewLineKind; content: string }[] = [];

  const lastLineno = onDisk.length;

  for (const hunk of response.hunks) {
    const pureDelete = hunk.lines.every(
      (l) =>
        l.kind === "committed_remove" || l.kind === "uncommitted_remove",
    );

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
        const anchor = pureDelete
          ? hunk.on_disk_start + 1
          : hunk.on_disk_start;
        const entry = { kind: line.kind, content: line.content };
        if (anchor <= 0) {
          removesAtTop.push(entry);
        } else if (anchor > lastLineno) {
          removesAtBottom.push(entry);
        } else {
          if (!removesBefore.has(anchor)) removesBefore.set(anchor, []);
          removesBefore.get(anchor)!.push(entry);
        }
      }
    }
  }

  const out: RenderedLine[] = [];
  for (const r of removesAtTop) {
    out.push({ kind: r.kind, content: r.content, lineno: null });
  }
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
  for (const r of removesAtBottom) {
    out.push({ kind: r.kind, content: r.content, lineno: null });
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
// block <div>; background tints apply per line. Tints are stronger than
// you'd see in a typical text editor's diff overlay — review-focused UI,
// the change blocks are supposed to draw your eye.
const KIND_STYLES: Record<
  FileViewLineKind,
  { border: string; bg: string; sigil: string; sigilColor: string }
> = {
  context: { border: "", bg: "", sigil: " ", sigilColor: "text-zinc-600" },
  committed_add: {
    border: "border-green-700",
    bg: "bg-green-500/20",
    sigil: "+",
    sigilColor: "text-green-400",
  },
  committed_remove: {
    border: "border-red-700",
    bg: "bg-red-500/20",
    sigil: "−",
    sigilColor: "text-red-400",
  },
  uncommitted_add: {
    border: "border-amber-600",
    bg: "bg-amber-500/20",
    sigil: "+",
    sigilColor: "text-amber-300",
  },
  uncommitted_remove: {
    border: "border-orange-700",
    bg: "bg-orange-500/20",
    sigil: "−",
    sigilColor: "text-orange-300",
  },
};

interface Props {
  response: FileViewResponse;
  diff: DiffConfig;
  expandAll: boolean;
}

/** Renders the unified diff view: per-line content, classified colors,
 *  block borders, collapsed unchanged-region expanders, and the
 *  always-visible line-number gutter. */
export function FileDiffView({ response, diff, expandAll }: Props) {
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

  return (
    <div className="font-mono text-xs">
      <div
        className="flex flex-col overflow-x-auto rounded border border-zinc-800 bg-zinc-950"
        role="region"
        aria-label={`File diff for ${response.path}`}
      >
        {blocks.map((block, idx) => {
          if (isCollapsed(block)) {
            const expanded = expandedCollapses.has(idx);
            if (expanded) {
              return (
                <ExpandedFormerly
                  key={`expanded-${idx}`}
                  block={block}
                  blocks={allLines}
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
                <LineRow key={li} line={line} styles={styles} />
              ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function LineRow({
  line,
  styles,
}: {
  line: RenderedLine;
  styles: (typeof KIND_STYLES)[FileViewLineKind];
}) {
  return (
    <div className={`flex items-baseline ${styles.bg}`}>
      <span className="inline-block w-12 select-none pr-2 text-right text-[10px] tabular-nums text-zinc-600">
        {line.lineno ?? ""}
      </span>
      <span
        aria-hidden="true"
        className={`inline-block w-3 select-none text-center ${styles.sigilColor}`}
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
}: {
  block: CollapsedBlock;
  blocks: DiffBlock[];
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
          <LineRow key={li} line={line} styles={styles} />
        ))}
    </div>
  );
}
