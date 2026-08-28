"""Prompt-only audit rules used before a repair candidate is generated."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable

_NO_OP_PATTERNS = (
    ("确保高质量输出", "命中无验收条件的质量要求。"),
    ("确保高质量", "命中无验收条件的质量要求。"),
    ("高质量输出", "命中无验收条件的质量要求。"),
)
_BINARY_CONTEXT_MARKERS = ("真正的问题", "本质上", "才是关键")
_BINARY_PATTERN = re.compile(r"不是[^。！？；;\n]*而是")
_BINARY_PATTERNS = _BINARY_CONTEXT_MARKERS
_CONSTRAINT_PREFIXES = ("仅", "不得", "禁止", "必须", "避免", "只")
_SOURCE_CONSTRAINTS = ("补充背景知识",)
_OUTPUT_PATTERNS = (
    re.compile(r"输出\s*JSON", re.IGNORECASE),
    re.compile(r"输出格式\s*[：:]\s*JSON", re.IGNORECASE),
    re.compile(r"包含[^。；;]+"),
    re.compile(r"[一二三四五六七八九十\d]+条[^，,。；;]*"),
    re.compile(r"每条不超过\s*\d+\s*字"),
)
_EXPANSIVE_TASK_MARKERS = ("总结", "分析", "提取", "改写", "生成")
_SCOPE_BOUNDARY_PATTERNS = (
    "仅依据原文",
    "只使用原文",
    "只依据原文",
    "不得补充",
    "禁止补充",
    "未知则说明",
)
_CONFLICT_SOURCE_PATTERNS = ("仅依据原文", "只使用原文")
_TASK_OBJECT_PATTERN = re.compile(
    r"(?:总结|分析|提取|改写|生成)\s*(?P<object>[^。！？；;\n]*)"
)
_TASK_OBJECT_FILLERS = ("一下", "一下吧")
_MATERIAL_REFERENCE_PATTERN = re.compile(
    r"(?:以下|提供的|输入的|输入\s*(?:JSON|CSV|文本|数据)|给定的)"
)
_INPUT_BOUNDARY_PATTERN = re.compile(
    r"(?:仅依据|基于|只处理|根据)\s*(?:以下|提供的|输入的|输入\s*(?:JSON|CSV|文本|数据)|给定的)"
)
_VAGUE_CONSTRAINT_TERMS = ("适当", "必要", "酌情", "尽量")
_REQUIRED_CONSTRAINT_PATTERN = re.compile(r"^必须\s*(?P<object>.+)$")
_FORBIDDEN_CONSTRAINT_PATTERN = re.compile(r"^(?:不得|禁止)\s*(?P<object>.+)$")
_REQUIRED_OUTPUT_FORMAT_PATTERN = re.compile(r"^必须输出\s*(?P<format>.+)$")
_PRIORITY_PATTERN = re.compile(r"以[^。！？；;\n]+为准|优先|若冲突|覆盖")


@dataclass(frozen=True)
class AuditContext:
    task: str
    constraints: tuple[str, ...]
    output_contract: tuple[str, ...]
    scope_boundary: str | None
    task_object: str | None
    input_boundary: str | None


@dataclass(frozen=True)
class AuditRule:
    code: str
    severity: str
    reason: str
    action: str
    applies: Callable[[AuditContext, str], bool]
    evidence: Callable[[AuditContext, str], str]


def _sentences(prompt: str) -> list[str]:
    return [segment.strip() for segment in re.split(r"[。！？；;\n]+", prompt) if segment.strip()]


def _has_output_contract(sentence: str) -> bool:
    return any(pattern.search(sentence) for pattern in _OUTPUT_PATTERNS)


def _first_task(sentences: list[str]) -> str:
    for sentence in sentences:
        if sentence.startswith(_CONSTRAINT_PREFIXES) or sentence in _SOURCE_CONSTRAINTS:
            if any(marker in sentence for marker in _EXPANSIVE_TASK_MARKERS):
                return sentence + "。"
            continue
        if _has_output_contract(sentence) and not _references_explicit_material(sentence):
            continue
        if any(evidence in sentence for evidence, _ in _NO_OP_PATTERNS):
            continue
        if _BINARY_PATTERN.search(sentence):
            continue
        return sentence + "。"
    return ""


def _unique(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _task_object(task: str) -> str | None:
    match = _TASK_OBJECT_PATTERN.search(task)
    if not match:
        return None
    value = match.group("object").strip("，,。！？；; ")
    if not value or value in _TASK_OBJECT_FILLERS:
        return None
    return value


def _has_supported_task_verb(task: str) -> bool:
    return bool(_TASK_OBJECT_PATTERN.search(task))


def _references_explicit_material(task: str) -> bool:
    return bool(_MATERIAL_REFERENCE_PATTERN.search(task))


def _input_boundary(prompt: str) -> str | None:
    match = _INPUT_BOUNDARY_PATTERN.search(prompt)
    return match.group(0) if match else None


def _extract_context(prompt: str) -> AuditContext:
    sentences = _sentences(prompt)
    constraints: list[str] = []
    output_contract: list[str] = []

    for sentence in sentences:
        for fragment in re.split(r"[，,；;]", sentence):
            item = fragment.strip()
            if item and (
                item.startswith(_CONSTRAINT_PREFIXES)
                or item in _SOURCE_CONSTRAINTS
                or any(prefix in item for prefix in ("不得", "禁止"))
                or any(term in item for term in _VAGUE_CONSTRAINT_TERMS)
            ):
                constraints.append(item)
        for pattern in _OUTPUT_PATTERNS:
            match = pattern.search(sentence)
            if match:
                output_contract.append(match.group(0))

    task = _first_task(sentences)
    scope_boundary = next(
        (boundary for boundary in _SCOPE_BOUNDARY_PATTERNS if boundary in prompt), None
    )
    return AuditContext(
        task=task,
        constraints=_unique(constraints),
        output_contract=_unique(output_contract),
        scope_boundary=scope_boundary,
        task_object=_task_object(task),
        input_boundary=_input_boundary(prompt),
    )


def _is_expansive_task(context: AuditContext) -> bool:
    return any(marker in context.task for marker in _EXPANSIVE_TASK_MARKERS)


def _has_source_conflict(context: AuditContext) -> bool:
    has_original_only = any(
        boundary in constraint
        for boundary in _SCOPE_BOUNDARY_PATTERNS
        for constraint in context.constraints
    )
    return has_original_only and any(
        "补充背景知识" in constraint for constraint in context.constraints
    )


def _vague_constraint_evidence(context: AuditContext) -> str:
    return next(
        constraint
        for constraint in context.constraints
        if any(term in constraint for term in _VAGUE_CONSTRAINT_TERMS)
    )


def _direct_constraint_conflict_evidence(context: AuditContext) -> str | None:
    required = [
        (constraint, match.group("object").strip())
        for constraint in context.constraints
        if (match := _REQUIRED_CONSTRAINT_PATTERN.fullmatch(constraint))
    ]
    forbidden = [
        (constraint, match.group("object").strip())
        for constraint in context.constraints
        if (match := _FORBIDDEN_CONSTRAINT_PATTERN.fullmatch(constraint))
    ]
    for required_text, required_object in required:
        for forbidden_text, forbidden_object in forbidden:
            if required_object == forbidden_object:
                return f"{required_text} / {forbidden_text}"
    return None


def _missing_constraint_priority_evidence(
    context: AuditContext, prompt: str
) -> str | None:
    if _PRIORITY_PATTERN.search(prompt):
        return None
    formats = [
        (constraint, match.group("format").strip())
        for constraint in context.constraints
        if (match := _REQUIRED_OUTPUT_FORMAT_PATTERN.fullmatch(constraint))
    ]
    for index, (first_text, first_format) in enumerate(formats):
        for second_text, second_format in formats[index + 1 :]:
            if first_format != second_format:
                return f"{first_text} / {second_text}"
    return None


def _has_binary_pollution(prompt: str) -> bool:
    return bool(_BINARY_PATTERN.search(prompt)) or any(
        marker in prompt for marker in _BINARY_CONTEXT_MARKERS
    )


def _binary_evidence(prompt: str) -> str:
    for marker in _BINARY_CONTEXT_MARKERS:
        if marker in prompt:
            return marker
    match = _BINARY_PATTERN.search(prompt)
    if match:
        return match.group(0)
    raise ValueError("binary evidence requested without a matching pattern")


_AUDIT_RULES = (
    AuditRule(
        "constraint_conflict",
        "error",
        "两条约束要求的信息来源互斥。",
        "删除其中一条，或写明两者的优先级。",
        lambda context, _prompt: _has_source_conflict(context),
        lambda _context, _prompt: "仅依据原文 / 补充背景知识",
    ),
    AuditRule(
        "direct_constraint_conflict",
        "error",
        "两条约束对同一对象提出了直接相反的要求。",
        "删除其中一条，或明确冲突时的优先级。",
        lambda context, _prompt: _direct_constraint_conflict_evidence(context)
        is not None,
        lambda context, _prompt: _direct_constraint_conflict_evidence(context) or "",
    ),
    AuditRule(
        "missing_output_contract",
        "warning",
        "已识别任务，但未识别可验证的输出契约。",
        "补充至少一项格式、字段、条数或长度要求。",
        lambda context, _prompt: bool(context.task) and not context.output_contract,
        lambda context, _prompt: context.task,
    ),
    AuditRule(
        "missing_scope_boundary",
        "warning",
        "任务可能扩展输入以外的信息，但未识别范围边界。",
        "补充事实来源、禁止补充或未知时的处理方式。",
        lambda context, _prompt: _is_expansive_task(context) and not context.scope_boundary,
        lambda context, _prompt: context.task,
    ),
    AuditRule(
        "missing_task_object",
        "warning",
        "已识别任务动作，但未识别可处理的对象。",
        "补充需要处理的对象，例如数据、文本、记录或具体主题。",
        lambda context, _prompt: _has_supported_task_verb(context.task) and not context.task_object,
        lambda context, _prompt: context.task,
    ),
    AuditRule(
        "missing_input_boundary",
        "warning",
        "任务明确引用材料，但未识别材料范围或引用方式。",
        "写明材料来源或处理边界，例如“仅依据以下文本”或“基于提供的 CSV”。",
        lambda context, _prompt: _references_explicit_material(context.task) and not context.input_boundary,
        lambda context, _prompt: context.task,
    ),
    AuditRule(
        "vague_constraint",
        "warning",
        "约束使用了不可直接验证的程度词。",
        "改为可检查条件，例如最大字数、条数、字段或明确禁止项。",
        lambda context, _prompt: any(
            term in constraint
            for constraint in context.constraints
            for term in _VAGUE_CONSTRAINT_TERMS
        ),
        lambda context, _prompt: _vague_constraint_evidence(context),
    ),
    AuditRule(
        "missing_constraint_priority",
        "warning",
        "同一对象存在多条必须限制，但未识别到优先级。",
        "删除重复限制，或补充“若冲突，以…为准”等优先级规则。",
        lambda context, prompt: _missing_constraint_priority_evidence(context, prompt)
        is not None,
        lambda context, prompt: _missing_constraint_priority_evidence(context, prompt)
        or "",
    ),
    AuditRule(
        "no_op_quality",
        "info",
        "命中无验收条件的质量要求。",
        "删除空泛质量要求，改为可检查的约束或输出格式。",
        lambda _context, prompt: any(evidence in prompt for evidence, _ in _NO_OP_PATTERNS),
        lambda _context, prompt: next(
            evidence for evidence, _ in _NO_OP_PATTERNS if evidence in prompt
        ),
    ),
    AuditRule(
        "binary_pollution",
        "info",
        "命中中文二元对立表达。",
        "删除二元对立句，直接陈述需要完成的任务或约束。",
        lambda _context, prompt: _has_binary_pollution(prompt),
        lambda _context, prompt: _binary_evidence(prompt),
    ),
)
_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def analyze_prompt(prompt: str) -> dict[str, object]:
    """Extract observable prompt requirements and flag supported prompt-only issues."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    context = _extract_context(prompt)
    issues = [
        {
            "code": rule.code,
            "severity": rule.severity,
            "evidence": rule.evidence(context, prompt),
            "reason": rule.reason,
            "action": rule.action,
        }
        for rule in _AUDIT_RULES
        if rule.applies(context, prompt)
    ]
    issues.sort(key=lambda issue: _SEVERITY_ORDER[issue["severity"]])

    return {
        "task": context.task,
        "constraints": list(context.constraints),
        "output_contract": list(context.output_contract),
        "coverage": {
            "has_task": bool(context.task),
            "has_constraints": bool(context.constraints),
            "has_output_contract": bool(context.output_contract),
            "has_scope_boundary": bool(context.scope_boundary),
            "has_task_object": bool(context.task_object),
            "has_input_boundary": bool(context.input_boundary),
        },
        "issues": issues,
    }
