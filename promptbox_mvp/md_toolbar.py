"""Markdown 点选工具栏的纯函数层（方案 C，零依赖）。

与 GUI 的边界：本模块只做「文本 + 选区 → 新文本 + 新选区」的变换，
Tkinter 只做胶水。纯函数层可单测，GUI 层不可 —— 这是能不能写测试的分界。

索引约定：选区用 [start, end) 字符索引（Python 切片风格，不含 end）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EditResult:
    text: str
    start: int
    end: int


def _line_bounds(text: str, index: int) -> tuple[int, int]:
    """Return the [line_start, line_end) character bounds of the line at index.

    line_end excludes the trailing newline (or equals len(text) at EOF).
    """
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    if end == -1:
        end = len(text)
    return start, end


def _has_selection(start: int, end: int) -> bool:
    return end > start


def _covers_multiple_lines(text: str, start: int, end: int) -> bool:
    return "\n" in text[start:end]


def _resolve_target(text: str, start: int, end: int) -> tuple[int, int]:
    """Resolve the actual [start, end) target for a line-oriented operation.

    - No selection (start == end): the full line under the cursor.
    - Single-line selection: the selected range as-is.
    - Multi-line selection: extended to cover the last line fully, using
      the line bounds of the *end* position (the last line).
    """
    line_start, _ = _line_bounds(text, start)
    if not _has_selection(start, end):
        _, line_end = _line_bounds(text, start)
        return line_start, line_end
    if _covers_multiple_lines(text, start, end):
        _, last_line_end = _line_bounds(text, max(end - 1, 0))
        return line_start, last_line_end
    return start, end


def toggle_heading(text: str, start: int, end: int, level: int) -> EditResult:
    """Toggle the heading level of the line under the cursor / spanning selection.

    Rules:
    - Toggling the same level again strips the heading (idempotent).
    - Toggling a different level rewrites the marker in place.
    - Every line inside a multi-line selection is toggled independently.
    - Selection end is extended to cover the last line when it spans
      multiple lines, so the cursor does not silently lose the selection.
    """
    if not 1 <= level <= 6:
        raise ValueError("level must be between 1 and 6")
    marker = "#" * level
    line_start, target_end = _resolve_target(text, start, end)
    multi = _covers_multiple_lines(text, start, end)

    lines = text[line_start:target_end].split("\n")
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        cur_marker, rest = _split_marker(stripped)
        if cur_marker == marker:
            out.append(indent + rest)
        else:
            out.append(f"{indent}{marker} {rest}")

    new_line_block = "\n".join(out)
    new_text = text[:line_start] + new_line_block + text[target_end:]
    if multi:
        return EditResult(new_text, line_start, line_start + len(new_line_block))
    return EditResult(new_text, line_start, line_start)


def _split_marker(line: str) -> tuple[str, str]:
    """Split a line into (heading_marker, rest) if it starts with '#' markers.

    Returns ("", line) when the line is not a heading.
    """
    i = 0
    while i < len(line) and line[i] == "#":
        i += 1
    if i == 0:
        return "", line
    marker = line[:i]
    rest = line[i:].lstrip(" ")
    return marker, rest


def wrap_selection(text: str, start: int, end: int, token: str) -> EditResult:
    """Wrap the selection with an inline token (bold **, italic *, code `).

    - With a selection: wraps it, and leaves the selection covering the
      wrapped span.
    - Without a selection: inserts the empty marker pair and places the
      cursor between the markers.
    """
    if start > end:
        start, end = end, start
    sel = text[start:end]
    if sel:
        new_text = text[:start] + token + sel + token + text[end:]
        return EditResult(new_text, start, end + 2 * len(token))
    pair = token + token
    new_text = text[:start] + pair + text[end:]
    return EditResult(new_text, start + len(token), start + len(token))


def toggle_blockquote(text: str, start: int, end: int) -> EditResult:
    """Prefix each selected line (or the current line) with '> '.

    Line-level idempotent: already-quoted lines are left untouched; only
    unquoted lines receive the marker. Repeated invocation on the same
    selection is therefore a no-op for quoted lines.
    """
    line_start, target_end = _resolve_target(text, start, end)
    lines = text[line_start:target_end].split("\n")
    out = []
    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if stripped.startswith(">"):
            out.append(line)
        else:
            out.append(f"{indent}> {stripped}")
    block = "\n".join(out)
    new_text = text[:line_start] + block + text[target_end:]
    return EditResult(new_text, line_start, line_start + len(block))


def insert_fence(text: str, start: int, end: int, language: str = "") -> EditResult:
    """Wrap the selection in a fenced code block.

    - With a selection: fences around it.
    - Without a selection: inserts an empty fence with the cursor placed
      at the language position (inside the opening fence).
    """
    lang = language.strip()
    opening = f"```{lang}"
    sel = text[start:end]
    if sel:
        block = f"{opening}\n{sel}\n```"
        new_text = text[:start] + block + text[end:]
        return EditResult(new_text, start, end + len(opening) + 2 + 3)
    new_text = text[:start] + f"{opening}\n\n```" + text[end:]
    cursor = start + len(opening)
    return EditResult(new_text, cursor, cursor)


def insert_rule(text: str, start: int, end: int) -> EditResult:
    """Insert a horizontal rule '---' on its own line at the cursor.

    The rule is placed after the current line (or between lines when the
    cursor sits at a line boundary), so it never merges into surrounding
    text.
    """
    line_start, line_end = _line_bounds(text, start)
    at_line_break = line_end == start and start < len(text) and text[start] == "\n"
    if at_line_break:
        insert_at = start
        separator = ""
    else:
        insert_at = line_end
        separator = "\n"
    new_text = text[:insert_at] + separator + "---\n" + text[insert_at:]
    cursor = insert_at + len(separator) + 4
    return EditResult(new_text, cursor, cursor)


def toggle_list(text: str, start: int, end: int, ordered: bool = False) -> EditResult:
    """Toggle list markers on each selected line.

    - Unordered: '- '   (idempotent toggle).
    - Ordered:   1. 2. 3. (auto-incrementing; strips when the first line
      is already an ordered list item).
    """
    line_start, target_end = _resolve_target(text, start, end)
    lines = text[line_start:target_end].split("\n")

    def is_item(line: str) -> bool:
        stripped = line.lstrip()
        if ordered:
            rest = stripped
            i = 0
            while i < len(rest) and rest[i].isdigit():
                i += 1
            return i > 0 and rest[i:].startswith(". ")
        return stripped.startswith("- ")

    if all(is_item(line) for line in lines if line.strip()):
        out = []
        for line in lines:
            stripped = line.lstrip()
            indent = line[: len(line) - len(stripped)]
            if ordered:
                i = 0
                while i < len(stripped) and stripped[i].isdigit():
                    i += 1
                rest = stripped[i:].lstrip(". ").lstrip(" ")
                out.append(indent + rest)
            elif stripped.startswith("- "):
                out.append(indent + stripped[2:])
            else:
                out.append(line)
        block = "\n".join(out)
    else:
        out = []
        n = 1
        for line in lines:
            stripped = line.lstrip()
            indent = line[: len(line) - len(stripped)]
            if not stripped:
                out.append(line)
                continue
            if ordered:
                out.append(f"{indent}{n}. {stripped}")
                n += 1
            else:
                out.append(f"{indent}- {stripped}")
        block = "\n".join(out)

    new_text = text[:line_start] + block + text[target_end:]
    return EditResult(new_text, line_start, line_start + len(block))


def insert_task(text: str, start: int, end: int) -> EditResult:
    """Toggle '- [ ] ' on each selected line (idempotent)."""
    line_start, target_end = _resolve_target(text, start, end)
    lines = text[line_start:target_end].split("\n")
    all_tasks = all(line.lstrip().startswith("- [ ] ") for line in lines if line.strip())
    out = []
    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if all_tasks and stripped.startswith("- [ ] "):
            out.append(indent + stripped[len("- [ ] "):])
        elif all_tasks:
            out.append(line)
        else:
            out.append(f"{indent}- [ ] {stripped}")
    block = "\n".join(out)
    new_text = text[:line_start] + block + text[target_end:]
    return EditResult(new_text, line_start, line_start + len(block))


def insert_table(text: str, start: int, end: int, rows: int, cols: int) -> EditResult:
    """Insert a Markdown table skeleton (header + separator + empty rows).

    The cursor is placed at the first header cell.
    """
    if not 1 <= rows <= 10 or not 1 <= cols <= 10:
        raise ValueError("rows/cols must be between 1 and 10")
    header = "| " + " | ".join(["列1", "列2", "列3"][:cols]) + " |"
    separator = "| " + " | ".join(["---"] * cols) + " |"
    body = []
    for r in range(rows):
        body.append("| " + " | ".join([""] * cols) + " |")
    block = "\n".join([header, separator] + body)
    new_text = text[:start] + block + text[end:]
    cursor = start + len("| ")
    return EditResult(new_text, cursor, cursor)


def insert_placeholder(text: str, start: int, end: int) -> EditResult:
    """Insert '{}' and place the cursor between the braces."""
    new_text = text[:start] + "{}" + text[end:]
    return EditResult(new_text, start + 1, start + 1)
