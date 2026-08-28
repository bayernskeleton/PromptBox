import json
import time
from pathlib import Path
from typing import Any, Callable

from .prompt_audit import analyze_prompt


_PROMPT_PATH = Path(__file__).with_name("prompts") / "repair_prompt.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_ALLOWED_MODES = ("B", "A")

_BASE_PRESERVE = [
    "保留原 Prompt 的任务对象和业务意图。",
    "保留原 Prompt 中可观察且不互斥的明确约束。",
]
_BASE_FORBIDDEN = [
    "不得编造原 Prompt 未提供的事实、资料或业务规则。",
    "不得无依据扩大任务范围、输出范围或目标用户。",
    "不得引入 Skill、文件路径、权限、跨会话记忆、模型选择或平台运行机制。",
]
_ISSUE_CONTRACT_TEMPLATES = {
    "constraint_conflict": (
        "删除互斥的信息来源约束，或明确两者优先级。",
        "候选不得同时保留“仅依据原文”和“补充背景知识”且无优先级。",
    ),
    "direct_constraint_conflict": (
        "删除同一对象的直接反向约束，或明确冲突时的优先级。",
        "候选不得同时保留本次证据中的“必须 / 不得或禁止”直接冲突。",
    ),
    "missing_output_contract": (
        "补充至少一项可验证的输出格式、字段、条数或长度要求。",
        "候选包含至少一项可观察的输出契约。",
    ),
    "missing_scope_boundary": (
        "补充事实来源、禁止补充或未知时的处理方式。",
        "候选包含可观察的范围边界。",
    ),
    "missing_task_object": (
        "补充需要处理的明确对象。",
        "候选中的受支持任务动作具有直接对象。",
    ),
    "missing_input_boundary": (
        "写明引用材料的来源或处理边界。",
        "候选明确材料来源或处理边界。",
    ),
    "vague_constraint": (
        "将本次证据中的空泛约束词改为字数、条数、字段或明确禁止项。",
        "候选不再包含本次证据中的空泛约束词。",
    ),
    "missing_constraint_priority": (
        "删除重复输出格式限制，或补充明确优先级。",
        "候选不再包含两条不同的“必须输出 <格式>”且无优先级。",
    ),
    "no_op_quality": (
        "删除空泛质量要求，改为可检查条件。",
        "候选不再包含本次证据中的空泛质量要求。",
    ),
    "binary_pollution": (
        "删除本次证据中的中文二元对立表达，直接陈述任务或约束。",
        "候选不再包含本次证据中的中文二元对立表达。",
    ),
}


def _build_quick_check(audit: dict[str, object]) -> dict[str, list[str]]:
    """Build a local quick-check reference from audit issues.

    This is advisory only. The model is explicitly told it may be
    incomplete or wrong and must not be constrained by it.
    """
    required_changes: list[str] = []
    acceptance_checks: list[str] = []
    seen_codes: set[str] = set()
    for issue in audit.get("issues", []):
        if not isinstance(issue, dict):
            continue
        code = issue.get("code")
        if not isinstance(code, str) or code in seen_codes:
            continue
        seen_codes.add(code)
        template = _ISSUE_CONTRACT_TEMPLATES.get(code)
        if template is None:
            continue
        required_changes.append(template[0])
        acceptance_checks.append(template[1])
    return {
        "required_changes": required_changes,
        "preserve": list(_BASE_PRESERVE),
        "forbidden_changes": list(_BASE_FORBIDDEN),
        "acceptance_checks": acceptance_checks,
    }


def compose_user_payload(
    context_text: str,
    user_input: str,
    context_label: str = "",
) -> str:
    """Compose the user message for a real-payload verification call.

    Rules:
    - Empty context sends only ``user_input``; an empty section marker
      would dilute model attention, so it is never emitted.
    - Empty ``user_input`` is rejected: sending context alone is a
      meaningless call.
    """
    if not isinstance(user_input, str) or not user_input.strip():
        raise ValueError("user_input must not be empty")
    context = context_text.strip() if isinstance(context_text, str) else ""
    if not context:
        return user_input
    label = context_label.strip() if isinstance(context_label, str) else ""
    heading = f"【业务参考上下文（{label}）】" if label else "【业务参考上下文】"
    return f"{heading}\n{context}\n\n【本次任务输入】\n{user_input}"


class RepairService:
    """Build a repair request and validate the transport response.

    The local audit is advisory (quick-check); the model drives the
    deep semantic diagnosis and must declare the mode ("B" or "A") it
    actually used. Human verification remains the final gate.
    """

    def __init__(self, transport: Callable[[list[dict[str, str]]], Any]):
        if not callable(transport):
            raise ValueError("transport must be callable")
        self.transport = transport

    def repair(
        self,
        prompt: str,
        output: str = "",
        comparison_input: str = "",
        task_goal: str = "",
        mode: str = "",
        context: str = "",
    ) -> dict[str, Any]:
        """Generate an optimized candidate. Only ``prompt`` is required.

        ``output``, ``comparison_input``, ``task_goal`` and ``context``
        are optional inputs that help the repair agent target the
        rewrite. Empty values are omitted from the payload instead of
        being sent as empty labelled sections.
        """
        if not self._non_empty_string(prompt):
            raise ValueError("prompt must be a non-empty string")
        if mode not in ("", "B", "A"):
            raise ValueError('mode must be "", "B" or "A"')
        try:
            audit = analyze_prompt(prompt)
            audit_status = {"status": "available", "message": ""}
            quick_check = _build_quick_check(audit)
        except Exception as exc:
            audit = None
            audit_status = {"status": "unavailable", "message": str(exc)}
            quick_check = None

        mode_hint = {
            "": "未指定，请按诊断结果自主选择档位",
            "B": "用户指定：意图保真微调（B 档）",
            "A": "用户指定：结构规范重构（A 档）",
        }[mode]

        sections = [f"原始提示词:\n{prompt}"]
        if context.strip():
            sections.append(f"业务上下文:\n{context}")
        if output.strip():
            sections.append(f"失败输出:\n{output}")
        if comparison_input.strip():
            sections.append(f"对照输入:\n{comparison_input}")
        if task_goal.strip():
            sections.append(f"任务目标:\n{task_goal}")
        sections.extend(
            [
                f"修复档位：{mode_hint}",
                "本地速检（仅作参考线索，可能漏检或误判，勿被其限制）:\n"
                + json.dumps(audit, ensure_ascii=False),
                "本地速检建议（同上，仅参考）:\n"
                + json.dumps(quick_check, ensure_ascii=False),
            ]
        )

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(sections)},
        ]
        try:
            raw_result = self.transport(messages)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("transport failed") from exc

        if isinstance(raw_result, str):
            try:
                result = json.loads(raw_result)
            except (TypeError, ValueError) as exc:
                raise ValueError("transport returned invalid JSON") from exc
        else:
            result = raw_result

        if not isinstance(result, dict):
            raise ValueError("repair result must be a JSON object")

        diagnosis = result.get("diagnosis")
        mode = result.get("mode")
        candidate = result.get("candidate")
        reasons = result.get("reasons")
        if not self._non_empty_string(diagnosis):
            raise ValueError("diagnosis must be a non-empty string")
        if mode not in _ALLOWED_MODES:
            raise ValueError('mode must be either "B" or "A"')
        if not self._non_empty_string(candidate):
            raise ValueError("candidate must be a non-empty string")
        if (
            not isinstance(reasons, list)
            or not reasons
            or any(not self._non_empty_string(reason) for reason in reasons)
        ):
            raise ValueError("reasons must be a non-empty list of non-empty strings")

        return {
            "diagnosis": diagnosis,
            "mode": mode,
            "candidate": candidate,
            "reasons": reasons,
            "audit": audit,
            "audit_status": audit_status,
            "quick_check": quick_check,
            "resolved_issue_codes": [
                code for code in result.get("resolved_issue_codes", [])
                if isinstance(code, str)
            ],
            "unresolved_issue_codes": [
                code for code in result.get("unresolved_issue_codes", [])
                if isinstance(code, str)
            ],
        }

    def verify(
        self,
        candidate_prompt: str,
        context_text: str,
        user_input: str,
        context_label: str = "",
        max_context_chars: int | None = None,
    ) -> dict[str, Any]:
        """Run one real payload with the candidate prompt as system role.

        Unlike :meth:`repair`, this performs no JSON contract validation:
        the model output is returned verbatim for human judgement.
        Truncation is always reported, never silently applied.
        """
        if not self._non_empty_string(candidate_prompt):
            raise ValueError("candidate_prompt must not be empty")
        if not isinstance(context_text, str):
            raise ValueError("context_text must be a string")
        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError("user_input must not be empty")
        if max_context_chars is not None:
            if not isinstance(max_context_chars, int) or isinstance(max_context_chars, bool):
                raise ValueError("max_context_chars must be positive")
            if max_context_chars <= 0:
                raise ValueError("max_context_chars must be positive")

        context = context_text.strip()
        truncated = False
        if max_context_chars is not None and len(context) > max_context_chars:
            context = context[:max_context_chars]
            truncated = True

        user_content = compose_user_payload(context, user_input, context_label)
        messages = [
            {"role": "system", "content": candidate_prompt},
            {"role": "user", "content": user_content},
        ]
        started = time.monotonic()
        try:
            raw_result = self.transport(messages)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("transport failed") from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if not isinstance(raw_result, str):
            raise ValueError("verify result must be a string")
        return {
            "output": raw_result,
            "payload_chars": len(candidate_prompt) + len(user_content),
            "context_chars": len(context),
            "truncated": truncated,
            "elapsed_ms": elapsed_ms,
        }

    @staticmethod
    def _non_empty_string(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())
