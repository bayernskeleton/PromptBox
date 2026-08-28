"""Deterministic Context Pack assembly and capacity reporting."""

from dataclasses import dataclass, field

from .context_manifest import ManifestEntry
from .context_parsers import ParserResult


def estimate_tokens(chars: int) -> int:
    return round(chars / 1.6)


@dataclass
class ContextBudget:
    char_count: int
    estimated_tokens: int
    capacity_status: str
    model_context_window: int | None
    available_input_tokens: int | None
    can_send: bool
    warning: str | None = None

    @classmethod
    def from_pack(
        cls,
        text: str,
        *,
        model_context_window: int | None,
        system_prompt_tokens: int = 0,
        current_prompt_tokens: int = 0,
        reserved_output_tokens: int = 0,
        safety_margin_tokens: int = 0,
    ) -> "ContextBudget":
        estimated = estimate_tokens(len(text))
        if model_context_window is None:
            return cls(len(text), estimated, "unknown", None, None, True, "模型容量未知，仅供估算")
        available = model_context_window - system_prompt_tokens - current_prompt_tokens - reserved_output_tokens - safety_margin_tokens
        can_send = estimated <= max(available, 0)
        warning = None if can_send else f"预计需要 {estimated} token，可用输入预算 {max(available, 0)} token"
        return cls(len(text), estimated, "known", model_context_window, available, can_send, warning)


@dataclass
class ContextPack:
    text: str
    entries: list[dict]
    file_count: int
    failed_count: int
    original_chars: int
    assembled_chars: int
    sent_chars: int
    truncated: bool
    actions: list[dict] = field(default_factory=list)
    failure_summary: list[dict] = field(default_factory=list)
    rules_version: str = "context-rules-v1"


_CATEGORY_ORDER = {
    ".md": 0, ".markdown": 0, ".txt": 0,
    ".json": 1, ".yaml": 1, ".yml": 1, ".ini": 1, ".cfg": 1, ".toml": 1,
    ".py": 2, ".js": 2, ".ts": 2, ".html": 2, ".css": 2, ".sql": 2,
    ".csv": 3,
    ".pdf": 4, ".docx": 4, ".xlsx": 4, ".pptx": 4,
}


def _block(entry: ManifestEntry, text: str) -> str:
    return "\n".join(
        [
            f"[文件：{entry.relative_path}]",
            f"[类型：{entry.suffix}]",
            "[解析状态：成功]",
            "[正文开始]",
            text,
            "[正文结束]",
        ]
    )


def assemble_context_pack(
    parsed_entries: list[tuple[ManifestEntry, ParserResult]],
    *,
    failed_entries: list[ManifestEntry] | None = None,
    file_char_limit: int | None = None,
) -> ContextPack:
    """Sort successful parsed files, add boundaries, and record truncation."""
    failures = list(failed_entries or [])
    successful = [item for item in parsed_entries if item[1].status == "success"]
    for entry, parsed in parsed_entries:
        if parsed.status != "success":
            failures.append(entry)
    if not successful:
        raise ValueError("没有文件成功解析，无法生成业务上下文")

    successful.sort(key=lambda item: (_CATEGORY_ORDER.get(item[0].suffix, 5), item[0].relative_path.casefold(), item[0].relative_path))
    actions: list[dict] = []
    blocks: list[str] = []
    entries: list[dict] = []
    original_chars = 0
    any_truncated = False
    for entry, parsed in successful:
        source_text = parsed.text
        original_chars += len(source_text)
        sent_text = source_text
        if file_char_limit is not None and len(source_text) > file_char_limit:
            sent_text = source_text[:file_char_limit]
            any_truncated = True
            actions.append({
                "action": "truncate",
                "relative_path": entry.relative_path,
                "source_chars": len(source_text),
                "sent_chars": len(sent_text),
            })
        if parsed.truncated:
            any_truncated = True
            actions.append({
                "action": "parser_truncate",
                "relative_path": entry.relative_path,
                "source_chars": len(source_text),
                "sent_chars": len(source_text),
                "reason": parsed.reason,
            })
        blocks.append(_block(entry, sent_text))
        entries.append({
            "relative_path": entry.relative_path,
            "suffix": entry.suffix,
            "source_chars": len(source_text),
            "sent_chars": len(sent_text),
            "truncated": len(source_text) != len(sent_text) or parsed.truncated,
        })
    text = "\n\n".join(blocks)
    failure_summary = [
        {
            "relative_path": item.relative_path,
            "reason_code": item.reason_code,
            "reason": item.reason,
        }
        for item in failures
    ]
    return ContextPack(
        text=text,
        entries=entries,
        file_count=len(successful),
        failed_count=len(failure_summary),
        original_chars=original_chars,
        assembled_chars=len(text),
        sent_chars=len(text),
        truncated=any_truncated,
        actions=actions,
        failure_summary=failure_summary,
    )
