"""Pretty-printer for the JSONL stream `claude -p --output-format stream-json
--verbose` writes to dispatch logs.

Pure functions over parsed event dicts — no I/O. The CLI's `fabric logs
--pretty` reads the .log file, splits on newlines, parses each line, and
calls `format_event` on every dict; the future dashboard does the same
either off the file (replay) or off the WebSocket fanout (live).

The formatter is deliberately tolerant: unknown event types are dropped
silently, malformed JSON lines fall through to the input string, missing
fields render as best-effort with `?` placeholders. We never raise — a
log with one bad line is still readable.
"""

from __future__ import annotations

import json


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _summarize_input(tool_name: str, inp: object) -> str:
    """Tool-specific short argument display. Falls back to the first
    string-valued field. Empty string if nothing salient."""
    if not isinstance(inp, dict):
        return ""
    if tool_name in {"Read", "Edit", "Write", "NotebookEdit"} and "file_path" in inp:
        return str(inp["file_path"])
    if tool_name == "Bash" and "command" in inp:
        return _truncate(str(inp["command"]), 80)
    if tool_name == "Grep" and "pattern" in inp:
        return _truncate(str(inp["pattern"]), 60)
    if tool_name == "Glob" and "pattern" in inp:
        return str(inp["pattern"])
    if tool_name in {"Task", "Agent"} and "description" in inp:
        return _truncate(str(inp["description"]), 60)
    if tool_name == "WebFetch" and "url" in inp:
        return str(inp["url"])
    if tool_name == "TodoWrite":
        todos = inp.get("todos") or []
        return f"{len(todos)} todo(s)" if isinstance(todos, list) else ""
    # Fallback: first string value.
    for v in inp.values():
        if isinstance(v, str):
            return _truncate(v, 80)
    return ""


def _tool_result_preview(content: object) -> str:
    """`tool_result.content` may be a string OR a list of `{type:"text",
    text:"…"}` blocks. Flatten to a single short preview."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        pieces = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                pieces.append(str(c.get("text", "")))
        text = "".join(pieces)
    else:
        text = str(content)
    return _truncate(text.replace("\n", " ⏎ "), 200)


def format_event(ev: dict) -> list[str]:
    """Render one stream-json event as zero or more pretty lines.

    Returned strings have no trailing newline. The caller joins with
    "\\n" — that keeps the formatter unaware of where it's printing to.
    """
    et = ev.get("type")

    if et == "system":
        if ev.get("subtype") == "init":
            sid = str(ev.get("session_id") or "?")[:8]
            model = ev.get("model") or "?"
            return [f"[init] model={model} session={sid}"]
        return []

    if et == "assistant":
        out: list[str] = []
        for block in ev.get("message", {}).get("content", []) or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text", "")).rstrip()
                if text:
                    out.append(text)
            elif btype == "tool_use":
                name = str(block.get("name") or "?")
                summary = _summarize_input(name, block.get("input", {}))
                out.append(f"→ {name}({summary})" if summary else f"→ {name}()")
        return out

    if et == "user":
        out = []
        for block in ev.get("message", {}).get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                preview = _tool_result_preview(block.get("content", ""))
                out.append(f"← {preview}" if preview else "← (empty)")
        return out

    if et == "result":
        if ev.get("is_error"):
            dur = ev.get("duration_ms", 0)
            return [f"[result] ERROR duration={dur}ms"]
        cost = ev.get("total_cost_usd")
        dur = ev.get("duration_ms", 0)
        cost_s = f" cost=${float(cost):.4f}" if isinstance(cost, (int, float)) else ""
        return [f"[result] ok duration={dur}ms{cost_s}"]

    # Unknown event type — silently skip.
    return []


def format_stream(text: str) -> str:
    """Format an entire stream-json log file (multiline JSONL).

    Tolerates blank lines, non-JSON lines (passed through with a marker),
    and JSON values that aren't dicts (skipped). Always returns a string
    ending in a single trailing newline if non-empty.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            lines.append(f"[non-json] {raw}")
            continue
        if not isinstance(ev, dict):
            continue
        lines.extend(format_event(ev))
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


__all__ = ["format_event", "format_stream"]
