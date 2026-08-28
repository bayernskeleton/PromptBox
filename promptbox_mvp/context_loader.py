"""Backward-compatible adapter for the shared context parser.

The multi-file context pipeline owns parsing rules. ``load_context_file`` keeps
its historical dictionary return shape for existing single-file callers.
"""

import csv
import io
import json
from pathlib import Path
from typing import Any

SUPPORTED_TEXT_SUFFIXES = (
    ".txt",
    ".md",
    ".markdown",
    ".py",
    ".log",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".toml",
    ".sql",
    ".js",
    ".ts",
    ".html",
    ".css",
)

_ENCODING_CANDIDATES = ("utf-8", "utf-8-sig", "gbk")

_BINARY_SUFFIX_HINTS = {
    ".docx": (
        "暂不支持 .docx。请在 Word 中全选复制（Ctrl+A / Ctrl+C），"
        "再点「载入剪贴板」，效果相同。"
    ),
    ".doc": (
        "暂不支持 .doc。请在 Word 中全选复制（Ctrl+A / Ctrl+C），"
        "再点「载入剪贴板」，效果相同。"
    ),
    ".pdf": "暂不支持 .pdf。请先复制正文文字，再点「载入剪贴板」。",
    ".xlsx": "暂不支持 .xlsx。请另存为 .csv 后载入，或复制单元格再点「载入剪贴板」。",
    ".xls": "暂不支持 .xls。请另存为 .csv 后载入，或复制单元格再点「载入剪贴板」。",
}

DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_CSV_ROW_LIMIT = 200


def _normalize_text(value: str) -> str:
    """Strip all leading BOM markers and normalise CRLF / CR line endings to LF.

    Windows editors routinely produce CRLF and BOM-prefixed UTF-8. Leaving
    either in place corrupts downstream char counts and shows up as stray
    characters in the model payload.
    """
    value = value.lstrip("\ufeff")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def read_text_with_fallback(path: Path) -> tuple[str, str]:
    """Decode a text file, trying UTF-8 then UTF-8-SIG then GBK.

    Returns (text, encoding_used). Raises ValueError naming the file when
    every candidate encoding fails.
    """
    raw = path.read_bytes()
    for encoding in _ENCODING_CANDIDATES:
        try:
            decoded = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        return _normalize_text(decoded), encoding
    raise ValueError(f"无法解码文件（已尝试 UTF-8 / UTF-8-SIG / GBK）：{path.name}")


def _escape_cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", " ").strip()


def _csv_to_markdown(text: str, row_limit: int) -> tuple[str, bool, int]:
    """Convert CSV text into a Markdown table.

    Returns (markdown, truncated, total_data_rows).
    """
    rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    if not rows:
        return "", False, 0

    header = [_escape_cell(cell) for cell in rows[0]]
    data_rows = rows[1:]
    total = len(data_rows)
    truncated = total > row_limit
    shown = data_rows[:row_limit] if truncated else data_rows

    width = len(header)
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for row in shown:
        cells = [_escape_cell(cell) for cell in row[:width]]
        cells += [""] * (width - len(cells))
        lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines), truncated, total


def load_context_file(
    file_path: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
    csv_row_limit: int = DEFAULT_CSV_ROW_LIMIT,
) -> dict[str, Any]:
    """Load one local file as plain-text business context.

    Returns a dict with: text, label, chars, truncated, note, encoding.
    """
    path = Path(file_path)
    if not path.exists():
        raise ValueError(f"文件不存在：{path}")
    if not path.is_file():
        raise ValueError(f"路径不是文件：{path}")

    from .context_parsers import parse_context_file

    parsed = parse_context_file(
        path,
        max_bytes=max_bytes,
        csv_row_limit=csv_row_limit,
    )
    if parsed.status != "success":
        if parsed.reason_code == "empty_content" and path.suffix.lower() == ".csv":
            return {
                "text": "",
                "label": path.name,
                "chars": 0,
                "truncated": False,
                "note": parsed.reason,
                "encoding": None,
            }
        if parsed.reason_code == "unsupported_format":
            try:
                legacy_text, legacy_encoding = read_text_with_fallback(path)
            except ValueError:
                raise ValueError(f"无法解码文件：{path.name}") from None
            return {
                "text": legacy_text,
                "label": path.name,
                "chars": len(legacy_text),
                "truncated": False,
                "note": "",
                "encoding": legacy_encoding,
            }
        reason = parsed.reason or "文件解析失败"
        if parsed.reason_code == "single_file_too_large":
            reason = f"文件过大（{path.name}）：{reason}"
        raise ValueError(reason)

    return {
        "text": parsed.text,
        "label": path.name,
        "chars": parsed.chars,
        "truncated": parsed.truncated,
        "note": parsed.reason,
        "encoding": parsed.encoding,
    }
