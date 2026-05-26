"""AppleScript quoting helpers for the Ghostty adapter.

AppleScript strings are double-quoted with ``"`` and use backslash
escapes for embedded ``"`` and ``\\``. We compose ``tell application
"Ghostty"`` snippets here that interpolate user-controlled paths and
shell commands; the helpers below produce safe literals.

We avoid heredoc piping into ``osascript -`` because some macOS
versions stumble on multi-line scripts via stdin under FastAPI's
subprocess wrapper. Instead callers pass the script as one
``-e <line>`` argument per line via :func:`build_osascript_args`.
"""
from __future__ import annotations


def quote(s: str) -> str:
    """Return ``s`` as an AppleScript double-quoted string literal.

    Escapes the two characters AppleScript treats specially inside a
    double-quoted string: backslash and double-quote. Everything else
    passes through — including spaces, single quotes, and Unicode.
    """
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def shell_single_quote(s: str) -> str:
    """Wrap ``s`` in POSIX single quotes for safe shell-command embedding.

    Used when we pass a free-form prompt through ``claude '<prompt>'``
    inside an AppleScript-defined ``command``. Single-quote rules:
    everything is literal *except* a single quote itself, which we
    close-quote, escape, and re-open.
    """
    return "'" + s.replace("'", "'\\''") + "'"


def build_osascript_args(lines: list[str]) -> list[str]:
    """Turn a list of AppleScript lines into the ``-e <line>`` args
    osascript expects on the command line. Empty lines are skipped so
    callers can use them for readability."""
    out: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        out.extend(["-e", line])
    return out
