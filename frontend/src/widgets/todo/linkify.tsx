import type { ReactNode } from "react";

// Matches http(s) URLs. The trailing class strips common sentence
// punctuation (.,;:!?) and closing brackets/quotes that usually aren't
// part of the link the user meant — so "see https://example.com." links
// to the URL without the period.
const URL_RE = /(https?:\/\/[^\s<]+[^\s<.,;:!?)\]}'"])/gi;

/**
 * Split free text into plain strings and clickable <a> elements for any
 * URLs it contains. Used to render todo titles and bullets in their
 * read-only (non-editing) state.
 *
 * Links stop click propagation so opening a link doesn't also trigger
 * the click-to-edit handler on the surrounding text.
 */
export function linkify(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  // Fresh lastIndex per call — URL_RE is module-level + /g, so reset.
  URL_RE.lastIndex = 0;
  while ((match = URL_RE.exec(text)) !== null) {
    const url = match[0];
    const start = match.index;
    if (start > lastIndex) {
      nodes.push(text.slice(lastIndex, start));
    }
    nodes.push(
      <a
        key={`${start}-${url}`}
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        onClick={(e) => e.stopPropagation()}
        className="text-indigo-400 underline decoration-indigo-700/60 hover:text-indigo-300"
      >
        {url}
      </a>,
    );
    lastIndex = start + url.length;
  }
  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }
  return nodes;
}

/** True when the text contains at least one linkifiable URL. */
export function hasUrl(text: string): boolean {
  URL_RE.lastIndex = 0;
  return URL_RE.test(text);
}
