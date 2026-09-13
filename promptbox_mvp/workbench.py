"""State controller and optional Tkinter UI for interactive repair work."""

import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable

from .contract import (
    create_candidate,
    create_repair_case,
    record_verdict,
    record_verification as record_case_verification,
    record_pairwise_verification as record_case_pairwise_verification,
)
from .prompt_variables import PromptTemplate
from .context_pack import ContextBudget, estimate_tokens



_RULE_LABELS = {
    "constraint_conflict": "约束冲突",
    "missing_output_contract": "缺失输出契约",
    "missing_scope_boundary": "无范围边界",
    "missing_task_object": "缺失任务对象",
    "missing_input_boundary": "缺失输入边界",
    "vague_constraint": "空泛约束词",
    "direct_constraint_conflict": "直接约束冲突",
    "missing_constraint_priority": "缺失约束优先级",
    "no_op_quality": "空泛质量要求",
    "binary_pollution": "中文二元对立污染",
}
_SEVERITY_LABELS = {"error": "错误", "warning": "警告", "info": "提示"}
_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

_WORKBENCH_COPY = {
    "title": "调优工作台",
    "intro": "输入已有提示词，生成一个可审阅的候选版本。",
    "prompt_label": "原提示词",
    "optional_label": "补充信息（可选）",
    "context_summary": "业务背景",
    "primary_action": "一键优化",
    "result_label": "候选版本",
    "result_section_label": "优化结果",
    "result_section_hint": "AI 生成的诊断、修改说明与候选版本，供你审阅后决定是否采纳。",
    "verify_label": "比较真实输出（可选）",
}
_WORKBENCH_STAGES = ("输入", "候选", "验证")


def snippet_display_content(snippet) -> str:
    """取一条 prompt 的当前展示正文：稳定版 → 当前版 → 顶层 content。
    与主程序 prompt_display_content 语义一致。此处实现为 promptbox_mvp 包内的
    解耦等价版——顶层 promptbox.py 已 import 本包，不能反向 import 顶层，否则循环依赖。"""
    versions = snippet.get("versions") or []
    by_id = {v.get("id"): v for v in versions}
    stable_id = snippet.get("stable_version_id")
    if stable_id and stable_id in by_id:
        return by_id[stable_id].get("content", "") or ""
    cur_id = snippet.get("current_version_id")
    if cur_id and cur_id in by_id:
        return by_id[cur_id].get("content", "") or ""
    return snippet.get("content", "") or ""


def snippet_merge_chunks(snippets, selected_ids) -> list[str]:
    """把选中的多条 prompt 按「# 标题\\n正文」拼成块列表，供追加进输入材料。
    只拼当前展示正文（见 snippet_display_content），跳过正文为空者；不拼 context。"""
    chunks: list[str] = []
    for s in snippets:
        if s.get("id") not in selected_ids:
            continue
        title = s.get("title") or "未命名提示词"
        content = snippet_display_content(s).strip()
        if not content:
            continue
        chunks.append(f"# {title}\n{content}")
    return chunks


def merge_text_into_prompt(existing: str, merged: str) -> str:
    """把拼接结果合入既有输入：空则直接放置，非空则以双换行追加。"""
    existing = (existing or "").strip()
    if existing:
        return f"{existing}\n\n{merged}"
    return merged


def _category_paths(categories) -> dict[str, list[str]]:
    by_id = {c.get("id"): c for c in categories if c.get("id")}
    paths = {}
    for cid in by_id:
        path = []
        current = cid
        seen = set()
        while current and current not in seen and current in by_id:
            seen.add(current)
            category = by_id[current]
            path.append(category.get("name") or current)
            current = category.get("parent_id")
        paths[cid] = list(reversed(path))
    return paths


def build_branch_payload(snippets, selected_ids, categories) -> dict[str, Any]:
    """Build a stable, structured payload for a selected multi-prompt branch."""
    paths = _category_paths(categories or [])
    items = []
    for order, snippet in enumerate(snippets or []):
        if snippet.get("id") not in selected_ids:
            continue
        content = snippet_display_content(snippet).strip()
        if not content:
            continue
        items.append({
            "id": snippet.get("id"),
            "title": snippet.get("title") or "未命名提示词",
            "category_path": paths.get(snippet.get("category_id"), []),
            "content": content,
            "order": order,
        })
    return {"is_branch": len(items) > 1, "items": items}


def branch_to_prompt(payload: dict[str, Any]) -> str:
    """Render a branch payload as one runnable prompt while preserving boundaries."""
    return branch_display_text(payload)


def branch_display_text(payload: dict[str, Any]) -> str:
    """Render a structured branch without losing category paths."""
    chunks = []
    for item in payload.get("items", []):
        path = "/".join(item.get("category_path") or [])
        title = item.get("title") or "未命名提示词"
        heading = f"# {path + '/' if path else ''}{title}"
        chunks.append(f"{heading}\n{item.get('content', '').strip()}")
    return "\n\n".join(chunks)


def format_branch_candidate(payload: dict[str, Any]) -> str:
    """Render branch candidate items and their explicit structural changes."""
    labels = {"unchanged": "保留", "modified": "修改", "merged": "合并", "split": "拆分", "added": "新增", "removed": "删除"}
    lines = []
    for item in payload.get("items", []):
        path = "/".join(item.get("category_path") or [])
        title = item.get("title") or "未命名提示词"
        lines.append(f"[{labels.get(item.get('change_type'), item.get('change_type', '变更'))}] {path + '/' if path else ''}{title}")
        lines.append(item.get("content", ""))
        if item.get("change_reason"):
            lines.append(f"理由：{item['change_reason']}")
    return "\n\n".join(lines)


def _workbench_theme(shared_theme=None) -> dict[str, str]:
    """Return the workbench palette, accepting the host app's active theme."""
    if shared_theme:
        return {
            "font": shared_theme.get("font", "Microsoft YaHei UI"),
            "bg": shared_theme["bg"],
            "input": shared_theme.get("bg_input", shared_theme.get("input", shared_theme["bg"])),
            "panel": shared_theme.get("bg_panel", shared_theme.get("panel", shared_theme["bg"])),
            "hover": shared_theme.get("bg_hover", shared_theme.get("hover", shared_theme["bg"])),
            "fg": shared_theme["fg"],
            "dim": shared_theme.get("fg_dim", shared_theme.get("dim", shared_theme["fg"])),
            "accent": shared_theme["accent"],
            "accent_2": shared_theme.get("accent_2", shared_theme["accent"]),
            "danger": shared_theme.get("danger", shared_theme["accent"]),
            "danger_fg": shared_theme.get("btn_danger_fg", shared_theme["bg"]),
            "border": shared_theme.get("border", shared_theme["bg"]),
            "secondary": shared_theme.get("btn_secondary", shared_theme.get("input", shared_theme["bg"])),
            "secondary_fg": shared_theme.get("btn_secondary_fg", shared_theme["fg"]),
            "primary_fg": shared_theme.get("primary_fg", shared_theme["bg"]),
        }
    return {
        "font": "Microsoft YaHei UI",
        "bg": "#faf6ee",
        "input": "#f4ede2",
        "panel": "#efe5d3",
        "hover": "#e4d9c5",
        "fg": "#4a3c31",
        "dim": "#8c7a6b",
        "accent": "#b38a5f",
        "accent_2": "#8a9a86",
        "danger": "#c94a29",
        "danger_fg": "#fff8eb",
        "border": "#dfd5c2",
        "secondary": "#e7dcc4",
        "secondary_fg": "#3a2f25",
        "primary_fg": "#faf6ee",
    }


def _format_audit_summary(
    audit: dict[str, Any] | None,
    audit_status: dict[str, str] | None = None,
    quick_check: dict[str, list[str]] | None = None,
    resolved_issue_codes: list[str] | None = None,
    unresolved_issue_codes: list[str] | None = None,
    mode: str | None = None,
) -> str:
    """Render local quick-check reference plus the model's mode for the workbench."""
    lines = []
    if mode is not None:
        mode_label = "结构规范重构（A 档）" if mode == "A" else "意图保真微调（B 档）"
        lines.append(f"修复档位：{mode_label}")
    if audit_status and audit_status.get("status") == "unavailable":
        message = audit_status.get("message") or "未知错误"
        lines.append(f"本地速检暂不可用：{message}")
    if not audit:
        return "\n".join(lines)
    lines.append("本地速检参考（代码规则，可能漏检或误判，仅参考）：")

    if audit.get("task"):
        lines.append(f"任务：{audit['task']}")
    if audit.get("constraints"):
        lines.append("约束：" + "；".join(audit["constraints"]))
    if audit.get("output_contract"):
        lines.append("输出契约：" + "；".join(audit["output_contract"]))
    coverage = audit.get("coverage", {})
    if coverage:
        boundary = "已识别" if coverage.get("has_scope_boundary") else "未识别"
        task_object = "已识别" if coverage.get("has_task_object") else "未识别"
        input_boundary = "已识别" if coverage.get("has_input_boundary") else "未识别"
        lines.append(f"范围边界：{boundary}")
        lines.append(f"任务对象：{task_object}")
        lines.append(f"输入边界：{input_boundary}")

    issues = list(audit.get("issues", []))
    issues.sort(key=lambda issue: _SEVERITY_ORDER.get(issue.get("severity"), 99))
    resolved = set(resolved_issue_codes or [])
    unresolved = set(unresolved_issue_codes or [])
    for issue in issues:
        code = issue.get("code", "")
        severity = issue.get("severity", "")
        rule_label = _RULE_LABELS.get(code, code or "未知规则")
        severity_label = _SEVERITY_LABELS.get(severity, severity or "提示")
        lines.append(f"[{severity_label}] {rule_label}")
        lines.append(f"证据：{issue.get('evidence', '')}")
        lines.append(f"处理：{issue.get('action', '')}")

    if quick_check is not None:
        lines.append("本地速检建议（同上，仅参考）")
        for label, key in (
            ("建议必改", "required_changes"),
            ("建议保留", "preserve"),
            ("建议禁止", "forbidden_changes"),
            ("建议验收", "acceptance_checks"),
        ):
            values = quick_check.get(key, [])
            lines.append(f"{label}：{'；'.join(values) if values else '无'}")
        for issue in issues:
            code = issue.get("code", "")
            rule_label = _RULE_LABELS.get(code, code or "未知规则")
            if code in resolved:
                lines.append(f"[模型声明已处理] {rule_label}")
            elif code in unresolved:
                lines.append(f"[模型声明未处理] {rule_label}")
        lines.append("模型声明不等于人工验证结论。")
    return "\n".join(lines)


def _strip_text_widget_trailing_newline(value: str) -> str:
    """Remove Tk Text's terminal newline without altering content characters."""
    if value.endswith("\n"):
        return value[:-1]
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RepairWorkbench:
    """Coordinate repair-case capture, candidate generation, and review."""

    def __init__(
        self,
        service: Any,
        save_case: Callable[[dict[str, Any]], Any] | None = None,
        adopt_candidate: Callable[[dict[str, Any]], str] | None = None,
        save_snapshot: Callable[[dict[str, Any]], Any] | None = None,
    ):
        self.service = service
        self.save_case = save_case
        self.adopt_candidate = adopt_candidate
        self.save_snapshot = save_snapshot
        self.case: dict[str, Any] | None = None
        self.analysis: dict[str, Any] | None = None
        self.mode: str = ""
        self.pairwise_cases: list[dict[str, Any]] = []
        self.active_pairwise_case_id: str | None = None
        self._next_pairwise_case_number = 1
        self.ui_context_pack: dict[str, Any] | None = None
        self.ui_context_manifest: dict[str, Any] | None = None
        self.ui_context_budget: dict[str, Any] | None = None
        self.ui_branch_payload: dict[str, Any] | None = None
        self.branch_verification: dict[str, Any] | None = None
        self.branch_decisions: dict[str, str] = {}

    def add_pairwise_case(
        self,
        context_label: str = "",
        context_text: str = "",
        user_input: str = "",
        *,
        source_type: str = "legacy_unknown",
        source_label: str = "",
        context_scope: str = "",
        user_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Add one editable pairwise case, capped at five cases."""
        if len(self.pairwise_cases) >= 5:
            raise ValueError("成对验证最多支持 5 条案例")
        case = {
            "id": f"run_{self._next_pairwise_case_number}",
            "context_label": context_label,
            "context_text": context_text,
            "user_input": user_input,
            "source_type": source_type,
            "source_label": source_label,
            "context_scope": context_scope,
            "user_confirmed": user_confirmed,
            "captured_at": _utc_now(),
            "run": None,
            "verdict": None,
            "note": "",
        }
        self.pairwise_cases.append(case)
        self._next_pairwise_case_number += 1
        self.active_pairwise_case_id = case["id"]
        return deepcopy(case)

    def get_pairwise_cases(self) -> list[dict[str, Any]]:
        return deepcopy(self.pairwise_cases)

    def select_pairwise_case(self, case_id: str) -> dict[str, Any] | None:
        for case in self.pairwise_cases:
            if case["id"] == case_id:
                self.active_pairwise_case_id = case_id
                return deepcopy(case)
        return None

    def remove_pairwise_case(self, case_id: str) -> dict[str, Any]:
        for index, case in enumerate(self.pairwise_cases):
            if case["id"] == case_id:
                removed = self.pairwise_cases.pop(index)
                self.active_pairwise_case_id = (
                    self.pairwise_cases[min(index, len(self.pairwise_cases) - 1)]["id"]
                    if self.pairwise_cases
                    else None
                )
                return deepcopy(removed)
        raise ValueError("pairwise case not found")

    def _get_pairwise_case(self, case_id: str) -> dict[str, Any]:
        for case in self.pairwise_cases:
            if case["id"] == case_id:
                return case
        raise ValueError("pairwise case not found")

    def update_pairwise_case(
        self,
        case_id: str,
        *,
        context_label: str,
        context_text: str,
        user_input: str,
    ) -> dict[str, Any]:
        case = self._get_pairwise_case(case_id)
        if case["run"] is not None:
            raise ValueError("已运行案例不能直接修改，请重新运行后再裁决")
        if not all(isinstance(value, str) for value in (context_label, context_text, user_input)):
            raise ValueError("案例字段必须是字符串")
        case["context_label"] = context_label
        case["context_text"] = context_text
        case["user_input"] = user_input
        self.active_pairwise_case_id = case_id
        return deepcopy(case)

    def run_pairwise_case(
        self,
        case_id: str,
        context_text: str | None = None,
        user_input: str | None = None,
        context_label: str | None = None,
        variables: dict[str, str] | None = None,
        context_hard_limit: int = 80_000,
        context_overflow_action: str | None = None,
        model_context_window: int | None = None,
        context_pack: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        case = self._get_pairwise_case(case_id)
        if context_text is not None:
            case["context_text"] = context_text
        if user_input is not None:
            case["user_input"] = user_input
        if context_label is not None:
            case["context_label"] = context_label
        case["verdict"] = None
        case["note"] = ""
        baseline_prompt = self.case["failure"]["prompt"] if self.case else ""
        candidate_data = self.analysis.get("candidate") if self.analysis else ""
        candidate_prompt = (
            candidate_data.get("content", "")
            if isinstance(candidate_data, dict)
            else candidate_data
        )
        branch_candidate = self.analysis.get("branch_candidate") if self.analysis else None
        if getattr(self, "ui_branch_payload", None) and isinstance(branch_candidate, dict):
            branch_result = self.run_branch_pairwise_verify(
                context_text=case["context_text"],
                user_input=case["user_input"],
                context_label=case["context_label"],
                variables=variables,
                context_hard_limit=context_hard_limit,
                context_overflow_action=context_overflow_action,
                model_context_window=model_context_window,
                context_pack=context_pack or getattr(self, "ui_context_pack", None),
            )
            result = branch_result["verification"]
        else:
            result = self.run_pairwise_verify(
                baseline_prompt=baseline_prompt,
                candidate_prompt=candidate_prompt,
                context_text=case["context_text"],
                user_input=case["user_input"],
                context_label=case["context_label"],
                variables=variables,
                context_hard_limit=context_hard_limit,
                context_overflow_action=context_overflow_action,
                model_context_window=model_context_window,
                context_pack=context_pack or getattr(self, "ui_context_pack", None),
            )
        run = {
            "id": case["id"],
            "snapshot_id": result.get("snapshot_id"),
            "source_type": case["source_type"],
            "source_label": case["source_label"],
            "context_scope": case["context_scope"],
            "source_chars": len(case["context_text"]),
            "context_chars": result["candidate"].get("context_chars", len(case["context_text"])),
            "context_label": case["context_label"],
            "context_text": case["context_text"],
            "context_hash": result.get("context_hash") or "sha256:" + hashlib.sha256(case["context_text"].encode("utf-8")).hexdigest(),
            "context_pack": result.get("context_pack"),
            "budget": result.get("budget"),
            "truncated": bool(result["baseline"].get("truncated") or result["candidate"].get("truncated")),
            "context_protection": result.get("context_protection", {}),
            "note": result.get("note"),
            "user_confirmed": case["user_confirmed"],
            "captured_at": case["captured_at"],
            "user_input": case["user_input"],
            "baseline_output": result["baseline"]["output"],
            "candidate_output": result["candidate"]["output"],
            "baseline_latency_ms": result["baseline_latency_ms"],
            "candidate_latency_ms": result["candidate_latency_ms"],
        }
        case["run"] = run
        self.active_pairwise_case_id = case_id
        return deepcopy(run)

    def set_pairwise_case_verdict(self, case_id: str, verdict: str, note: str = "") -> dict[str, Any]:
        if verdict not in {"candidate_better", "baseline_better", "equal", "undecided"}:
            raise ValueError("invalid pairwise verdict")
        case = self._get_pairwise_case(case_id)
        if case["run"] is None:
            raise ValueError("案例尚未运行，不能裁决")
        case["verdict"] = verdict
        case["note"] = note
        return deepcopy(case)

    def record_pairwise_cases(
        self,
        overall_conclusion: str,
        passed: bool,
        summary_note: str = "",
    ) -> dict[str, Any]:
        runs = []
        for case in self.pairwise_cases:
            if case["run"] is None:
                continue
            if case["verdict"] is None:
                raise ValueError("每条已运行案例都必须先完成人工裁决")
            run = deepcopy(case["run"])
            run["verdict"] = case["verdict"]
            run["note"] = case["note"]
            runs.append(run)
        if not runs:
            raise ValueError("至少运行一条案例后才能保存验证")
        return self.record_pairwise_verification(
            runs=runs,
            overall_conclusion=overall_conclusion,
            passed=passed,
            summary_note=summary_note,
        )

    def capture_case(
        self,
        snippet_id: str,
        base_version_id: str,
        base_version_number: int,
        prompt: str,
        output: str,
        comparison_input: str,
        task_goal: str = "",
    ) -> dict[str, Any]:
        case = create_repair_case(
            snippet_id,
            base_version_id,
            base_version_number,
            prompt,
            output,
            comparison_input,
            task_goal,
        )
        self.case = case
        self.analysis = None
        self.pairwise_cases = []
        self.active_pairwise_case_id = None
        self._next_pairwise_case_number = 1
        self.branch_verification = None
        self.branch_decisions = {}
        return case

    def start_optimization(
        self,
        prompt: str,
        output: str = "",
        comparison_input: str = "",
        task_goal: str = "",
    ) -> dict[str, Any]:
        """Start a one-click optimization with only the prompt required.

        Uses ``ui_baseline`` when the workbench was opened from a
        selected snippet; otherwise falls back to a sentinel identifier
        so prompt-only optimization still produces a valid case.
        """
        baseline = getattr(self, "ui_baseline", None) or {}
        snippet_id = baseline.get("snippet_id") or "__prompt_only__"
        base_version_id = baseline.get("base_version_id") or "__prompt_only__"
        base_version_number = baseline.get("base_version_number") or 1
        return self.capture_case(
            snippet_id=snippet_id,
            base_version_id=base_version_id,
            base_version_number=base_version_number,
            prompt=prompt,
            output=output,
            comparison_input=comparison_input,
            task_goal=task_goal,
        )

    def generate_candidate(self) -> dict[str, Any]:
        if self.case is None:
            raise ValueError("no repair case captured")
        if self.case["status"] != "captured":
            raise ValueError("candidate already generated")

        failure = self.case["failure"]
        repair_args = (
            failure["prompt"],
            failure["output"],
            self.case["comparison"]["input"],
            self.case["task_goal"],
            self.mode,
            getattr(self, "ui_context_text", ""),
        )
        branch_payload = getattr(self, "ui_branch_payload", None)
        if branch_payload:
            result = self.service.repair(*repair_args, branch=branch_payload)
        else:
            result = self.service.repair(*repair_args)
        self.analysis = deepcopy(result)
        self.case["analysis"] = {
            key: deepcopy(result.get(key))
            for key in (
                "diagnosis",
                "mode",
                "audit",
                "audit_status",
                "quick_check",
                "resolved_issue_codes",
                "unresolved_issue_codes",
                "branch_candidate",
                "structure_diagnosis",
            )
            if key in result
        }
        candidate = create_candidate(
            self.case,
            result["candidate"],
            result["reasons"],
        )
        return candidate

    def record_choice(self, choice: str, note: str = "") -> dict[str, Any]:
        if self.case is None:
            raise ValueError("no repair case captured")
        candidates = self.case.get("candidates", [])
        if not candidates:
            raise ValueError("no candidate generated")
        candidate = candidates[-1]
        record_verdict(self.case, candidate["id"], choice, note)
        if choice == "adopt" and self.save_case is not None:
            self.save_case(self.case)
        return self.case

    def _require_branch_decisions(self) -> None:
        if not (self.analysis and self.analysis.get("branch_candidate")):
            return
        candidate_branch = self.analysis["branch_candidate"]
        expected = {
            str(item.get("id"))
            for item in candidate_branch.get("items", [])
            if item.get("id")
        }
        if set(self.branch_decisions) != expected:
            raise ValueError("多 Prompt 候选必须先逐条完成人工裁决")

    def record_verification(
        self,
        input_text: str,
        output_note: str,
        rating: int | None,
        passed: bool,
    ) -> dict[str, Any]:
        """Record a real validation result and adopt only a passing candidate."""
        if self.case is None:
            raise ValueError("no repair case captured")
        if self.case.get("adopted_version_id") is not None:
            raise ValueError("case already adopted")
        if passed:
            self._require_branch_decisions()

        record_case_verification(
            self.case,
            input_text,
            output_note,
            rating,
            passed,
        )
        if not passed:
            if self.save_case is not None:
                self.save_case(self.case)
            return self.case

        if self.adopt_candidate is None:
            raise ValueError("no adopt callback configured")
        if passed:
            self._require_branch_decisions()
        try:
            adopted_version_id = self.adopt_candidate(self.case)
        except Exception:
            self.case["verification"] = {
                "status": "pending",
                "input": "",
                "output_note": "",
                "rating": None,
                "verified_at": None,
            }
            self.case["status"] = "candidate_ready"
            raise
        if not isinstance(adopted_version_id, (str, list)) or not adopted_version_id:
            raise ValueError("adopt callback must return a version id or ids")

        self.case["adopted_version_id"] = adopted_version_id
        self.case["status"] = "validated"
        if self.save_case is not None:
            self.save_case(self.case)
        return self.case

    def record_pairwise_verification(
        self,
        runs: list[dict[str, Any]],
        overall_conclusion: str,
        passed: bool,
        summary_note: str = "",
    ) -> dict[str, Any]:
        """Record a real pairwise verification result across 1-5 cases and adopt if passed."""
        if self.case is None:
            raise ValueError("no repair case captured")
        if self.case.get("adopted_version_id") is not None:
            raise ValueError("case already adopted")

        if passed:
            self._require_branch_decisions()
        record_case_pairwise_verification(
            self.case,
            runs,
            overall_conclusion,
            passed,
            summary_note=summary_note,
        )
        if not passed:
            if self.save_case is not None:
                self.save_case(self.case)
            return self.case

        if self.adopt_candidate is None:
            raise ValueError("no adopt callback configured")
        if passed:
            self._require_branch_decisions()
        try:
            adopted_version_id = self.adopt_candidate(self.case)
        except Exception:
            self.case["verification"] = {
                "status": "pending",
                "input": "",
                "output_note": "",
                "rating": None,
                "verified_at": None,
            }
            self.case["status"] = "candidate_ready"
            raise
        if not isinstance(adopted_version_id, (str, list)) or not adopted_version_id:
            raise ValueError("adopt callback must return a version id or ids")

        self.case["adopted_version_id"] = adopted_version_id
        self.case["status"] = "validated"
        if self.save_case is not None:
            self.save_case(self.case)
        return self.case

    def run_branch_pairwise_verify(
        self,
        context_text: str,
        user_input: str = "",
        context_label: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run the original and restructured multi-prompt branches side by side."""
        baseline_branch = getattr(self, "ui_branch_payload", None)
        candidate_branch = self.analysis.get("branch_candidate") if self.analysis else None
        if not isinstance(baseline_branch, dict) or not baseline_branch.get("is_branch"):
            raise ValueError("baseline branch is required")
        if not isinstance(candidate_branch, dict) or not candidate_branch.get("items"):
            raise ValueError("candidate branch is required")
        verification = self.run_pairwise_verify(
            baseline_prompt=branch_to_prompt(baseline_branch),
            candidate_prompt=branch_to_prompt(candidate_branch),
            context_text=context_text,
            user_input=user_input,
            context_label=context_label,
            **kwargs,
        )
        self.branch_verification = {
            "baseline_branch": deepcopy(baseline_branch),
            "candidate_branch": deepcopy(candidate_branch),
            "verification": deepcopy(verification),
        }
        return deepcopy(self.branch_verification)

    def set_branch_decisions(self, decisions: dict[str, str]) -> dict[str, str]:
        """Record an explicit human decision for every branch candidate item."""
        candidate_branch = self.analysis.get("branch_candidate") if self.analysis else None
        items = candidate_branch.get("items", []) if isinstance(candidate_branch, dict) else []
        expected = {str(item.get("id")) for item in items if item.get("id")}
        if set(decisions) != expected:
            raise ValueError("每条分支候选都必须完成人工裁决")
        allowed = {"adopt", "reject", "keep_original"}
        if any(value not in allowed for value in decisions.values()):
            raise ValueError("分支候选裁决必须是 adopt、reject 或 keep_original")
        self.branch_decisions = dict(decisions)
        if self.case is not None:
            self.case["branch_decisions"] = deepcopy(self.branch_decisions)
        return deepcopy(self.branch_decisions)

    def build_adopted_branch(self) -> dict[str, Any]:
        """Build the branch that is allowed to be written back after review."""
        candidate_branch = self.analysis.get("branch_candidate") if self.analysis else None
        items = candidate_branch.get("items", []) if isinstance(candidate_branch, dict) else []
        expected = {str(item.get("id")) for item in items if item.get("id")}
        if set(self.branch_decisions) != expected:
            raise ValueError("每条分支候选都必须先完成裁决")
        adopted = []
        for item in items:
            decision = self.branch_decisions[str(item["id"])]
            if decision == "reject":
                continue
            if decision == "keep_original":
                source_id = next(iter(item.get("source_ids") or []), None)
                original = next((x for x in (getattr(self, "ui_branch_payload", {}) or {}).get("items", []) if x.get("id") == source_id), None)
                if original is not None:
                    kept = deepcopy(original)
                    kept["source_ids"] = [source_id] if source_id else []
                    kept["change_type"] = "unchanged"
                    kept["change_reason"] = "人工保留原 Prompt"
                    adopted.append(kept)
                continue
            adopted.append(deepcopy(item))
        return {"is_branch": len(adopted) > 1, "items": adopted}

    def get_view(self) -> dict[str, Any]:
        if self.case is None:
            return {
                "status": None,
                "diagnosis": None,
                "mode": None,
                "candidate": None,
                "verdict": None,
                "audit": None,
                "audit_status": None,
                "quick_check": None,
                "resolved_issue_codes": [],
                "unresolved_issue_codes": [],
            }

        candidate = None
        if self.case["candidates"]:
            source = self.case["candidates"][-1]
            candidate = {
                "content": source["content"],
                "reasons": list(source["change_reasons"]),
            }
        view = {
            "status": self.case["status"],
            "diagnosis": self.analysis.get("diagnosis") if self.analysis else None,
            "mode": self.analysis.get("mode") if self.analysis else None,
            "candidate": candidate,
            "verdict": self.case["verdict"],
            "audit": self.analysis.get("audit") if self.analysis else None,
            "audit_status": self.analysis.get("audit_status") if self.analysis else None,
            "quick_check": self.analysis.get("quick_check") if self.analysis else None,
            "resolved_issue_codes": self.analysis.get("resolved_issue_codes", []) if self.analysis else [],
            "unresolved_issue_codes": self.analysis.get("unresolved_issue_codes", []) if self.analysis else [],
        }
        if self.analysis and self.analysis.get("branch_candidate"):
            view["branch_candidate"] = self.analysis["branch_candidate"]
            view["structure_diagnosis"] = self.analysis.get("structure_diagnosis", [])
        return deepcopy(view)

    @staticmethod
    def _protect_context(
        context_text: str,
        *,
        hard_limit: int,
        max_context_chars: int,
        overflow_action: str | None,
    ) -> tuple[str, dict[str, Any]]:
        """Apply the hard context gate before sending a verification request."""
        if overflow_action not in {None, "truncate", "cancel"}:
            raise ValueError("上下文超限时只能选择截断或取消")
        if not isinstance(hard_limit, int) or isinstance(hard_limit, bool) or hard_limit <= 0:
            raise ValueError("context_hard_limit must be positive")
        if not isinstance(max_context_chars, int) or isinstance(max_context_chars, bool) or max_context_chars <= 0:
            raise ValueError("max_context_chars must be positive")
        source_chars = len(context_text)
        if source_chars <= hard_limit:
            return context_text, {
                "source_chars": source_chars,
                "hard_limit": hard_limit,
                "action": None,
                "sent_chars": source_chars,
            }
        if overflow_action in {None, "cancel"}:
            if overflow_action == "cancel":
                raise ValueError("已取消发送：上下文超过硬限")
            raise ValueError(
                f"上下文 {source_chars:,} 字符超过硬限 {hard_limit:,}，请选择截断或取消"
            )
        sent_chars = min(max_context_chars, hard_limit)
        return context_text[:sent_chars], {
            "source_chars": source_chars,
            "hard_limit": hard_limit,
            "action": "truncate",
            "sent_chars": sent_chars,
        }

    def run_verify(
        self,
        candidate_prompt: str,
        context_text: str,
        user_input: str = "",
        context_label: str = "",
        soft_limit: int = 20_000,
        max_context_chars: int = 20_000,
        variables: dict[str, str] | None = None,
        context_hard_limit: int = 80_000,
        context_overflow_action: str | None = None,
        model_context_window: int | None = None,
        context_pack: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a real-payload verification of the candidate prompt.

        Testable logic behind the GUI: the Toplevel only renders widgets
        and delegates here. Soft limit produces a warning (token estimate
        at roughly 1 char per 1.6 tokens for Chinese); the hard limit is
        enforced inside :meth:`RepairService.verify` which flags truncation
        so a result produced on partial input is never presented silently.
        """
        if self.analysis is None or not self.analysis.get("candidate"):
            raise ValueError("no candidate available for verification")
        if not isinstance(candidate_prompt, str) or not candidate_prompt.strip():
            raise ValueError("no candidate available for verification")
        if not isinstance(context_text, str) or not context_text.strip():
            context_text = ""
        if not isinstance(user_input, str) or not user_input.strip():
            user_input = "（本次材料）"
        pack_metadata = deepcopy(context_pack) if context_pack else None
        if pack_metadata:
            context_text = pack_metadata.get("text", context_text)
        context_budget = ContextBudget.from_pack(context_text, model_context_window=model_context_window)
        context_text, context_protection = self._protect_context(
            context_text,
            hard_limit=context_hard_limit,
            max_context_chars=max_context_chars,
            overflow_action=context_overflow_action,
        )
        context_hash = "sha256:" + hashlib.sha256(context_text.encode("utf-8")).hexdigest()
        self.last_snapshot_id = None
        if variables is not None:
            template = PromptTemplate.from_text(
                candidate_prompt,
                getattr(self, "ui_variable_definitions", {}),
            )
            candidate_prompt = template.render(variables)
            if self.save_snapshot is not None:
                self.last_snapshot_id = self.save_snapshot({
                    "trigger": "单次验证",
                    "template": template.text,
                    "rendered_prompt": candidate_prompt,
                    "variables": dict(variables),
                    "variable_definitions": template.definitions(),
                })

        result = self.service.verify(
            candidate_prompt,
            context_text,
            user_input,
            context_label=context_label,
            max_context_chars=max_context_chars,
        )

        warning = None
        if len(context_text) > soft_limit:
            tokens = round(len(context_text) / 1.6)
            warning = (
                f"上下文 {len(context_text):,} 字符（约 {tokens:,} token）"
                f"超过软限 {soft_limit:,}，注意模型注意力稀释与 token 成本。"
            )
        note = (
            f"本次上下文已截断至 {result['context_chars']:,} 字符，结果基于部分输入。"
            if result["truncated"]
            else None
        )
        budget_data = deepcopy(pack_metadata.get("budget")) if pack_metadata and pack_metadata.get("budget") else context_budget.__dict__.copy()
        output = {
            **result,
            "warning": warning,
            "budget_warning": context_budget.warning,
            "note": note,
            "context_protection": context_protection,
            "context_hash": context_hash,
            "budget": budget_data,
            "context_pack": pack_metadata,
        }
        if getattr(self, "last_snapshot_id", None):
            output["snapshot_id"] = self.last_snapshot_id
        return output

    def run_pairwise_verify(
        self,
        baseline_prompt: str,
        candidate_prompt: str,
        context_text: str,
        user_input: str = "",
        context_label: str = "",
        soft_limit: int = 20_000,
        max_context_chars: int = 20_000,
        variables: dict[str, str] | None = None,
        context_hard_limit: int = 80_000,
        context_overflow_action: str | None = None,
        model_context_window: int | None = None,
        context_pack: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run verification on both prompts, using one shared variable mapping when supplied."""
        if not isinstance(baseline_prompt, str) or not baseline_prompt.strip():
            raise ValueError("no baseline prompt available for verification")
        if self.analysis is None or not self.analysis.get("candidate"):
            raise ValueError("no candidate available for verification")
        if not isinstance(candidate_prompt, str) or not candidate_prompt.strip():
            raise ValueError("no candidate available for verification")
        if not isinstance(context_text, str) or not context_text.strip():
            context_text = ""
        if not isinstance(user_input, str) or not user_input.strip():
            user_input = "（本次材料）"
        pack_metadata = deepcopy(context_pack) if context_pack else None
        if pack_metadata:
            context_text = pack_metadata.get("text", context_text)
        context_budget = ContextBudget.from_pack(context_text, model_context_window=model_context_window)
        context_text, context_protection = self._protect_context(
            context_text,
            hard_limit=context_hard_limit,
            max_context_chars=max_context_chars,
            overflow_action=context_overflow_action,
        )
        context_hash = "sha256:" + hashlib.sha256(context_text.encode("utf-8")).hexdigest()
        self.last_snapshot_id = None
        snapshot_payload = None
        if variables is not None:
            baseline_template = PromptTemplate.from_text(
                baseline_prompt,
                getattr(self, "ui_variable_definitions", {}),
            )
            candidate_template = PromptTemplate.from_text(
                candidate_prompt,
                getattr(self, "ui_variable_definitions", {}),
            )
            baseline_prompt = baseline_template.render(variables)
            candidate_prompt = candidate_template.render(variables)
            snapshot_payload = {
                "trigger": "成对验证",
                "template": baseline_template.text,
                "rendered_prompt": baseline_prompt,
                "variables": dict(variables),
                "variable_definitions": baseline_template.definitions(),
                "extra": {
                    "baseline_template": baseline_template.text,
                    "baseline_rendered_prompt": baseline_prompt,
                    "candidate_template": candidate_template.text,
                    "candidate_rendered_prompt": candidate_prompt,
                },
            }
            if self.save_snapshot is not None:
                self.last_snapshot_id = self.save_snapshot(snapshot_payload)

        baseline_res = self.service.verify(
            baseline_prompt,
            context_text,
            user_input,
            context_label=context_label,
            max_context_chars=max_context_chars,
        )
        candidate_res = self.service.verify(
            candidate_prompt,
            context_text,
            user_input,
            context_label=context_label,
            max_context_chars=max_context_chars,
        )

        warning = None
        if len(context_text) > soft_limit:
            tokens = round(len(context_text) / 1.6)
            warning = (
                f"上下文 {len(context_text):,} 字符（约 {tokens:,} token）"
                f"超过软限 {soft_limit:,}，注意模型注意力稀释与 token 成本。"
            )
        note = (
            f"本次上下文已截断至 {candidate_res['context_chars']:,} 字符，结果基于部分输入。"
            if (baseline_res.get("truncated") or candidate_res.get("truncated"))
            else None
        )
        result = {
            "baseline": baseline_res,
            "candidate": candidate_res,
            "baseline_latency_ms": baseline_res.get("elapsed_ms", 0),
            "candidate_latency_ms": candidate_res.get("elapsed_ms", 0),
            "warning": warning,
            "note": note,
            "context_protection": context_protection,
            "context_hash": context_hash,
            "budget": deepcopy(pack_metadata.get("budget")) if pack_metadata and pack_metadata.get("budget") else context_budget.__dict__.copy(),
            "context_pack": pack_metadata,
        }
        result["baseline"]["context_hash"] = context_hash
        result["candidate"]["context_hash"] = context_hash
        if getattr(self, "last_snapshot_id", None):
            result["snapshot_id"] = self.last_snapshot_id
        return result

    def open_toplevel(self, parent: Any, theme: dict[str, str] | None = None) -> Any:
        """Open the optional Tkinter workbench window without import-time UI work."""
        try:
            import tkinter as tk
            from tkinter import filedialog, messagebox
        except ImportError as exc:
            raise RuntimeError("Tkinter is unavailable") from exc

        try:
            import pyperclip
        except ImportError:
            pyperclip = None

        from .context_loader import load_context_file
        from .context_pipeline import build_context_from_selection

        colors = _workbench_theme(theme)
        window = tk.Toplevel(parent)
        window.title("PromptBox 调优工作台")
        window.geometry("920x760")
        window.minsize(760, 620)
        window.configure(bg=colors["bg"])

        # 图标处理：Windows Tk 上对 Toplevel 显式调用 iconbitmap/iconphoto 反而会
        # 错装成默认灰色图标；正确做法是「不设置任何图标」，子窗口会自动继承父窗口
        # （主 Toplevel self.win 已在 promptbox.py 经 apply_window_icon 挂上新 logo）。
        # 若父窗口没图标（极少数情况），这里维持 Tk 默认，不做二次覆盖。

        def _widget_bg(widget: Any) -> str:
            """读取容器当前背景，供 label 默认继承，避免在卡片内出现白框。

            卡片（Frame）用 panel 色，窗口用 bg 色；label 默认跟随父容器，
            保证与所在卡片视觉一致，不再用窗口 bg 色平铺出浅一档的矩形。
            显式传入 bg= 时优先。
            """
            try:
                return widget.cget("bg") or colors["bg"]
            except Exception:
                return colors["bg"]

        def make_label(parent_, text="", *, size=10, bold=False, color=None, **kwargs):
            label_bg = kwargs.pop("bg", None) or _widget_bg(parent_)
            return tk.Label(parent_, text=text, bg=label_bg, fg=color or colors["fg"],
                            font=(colors["font"], size, "bold" if bold else "normal"), **kwargs)

        def make_button(parent_, text, command, *, primary=False, danger=False, **kwargs):
            if primary:
                bg, fg, active = colors["accent"], colors["primary_fg"], colors["accent"]
            elif danger:
                bg, fg, active = colors["danger"], colors["danger_fg"], colors["danger"]
            else:
                bg, fg, active = colors["secondary"], colors["secondary_fg"], colors["hover"]
            return tk.Button(parent_, text=text, command=command, bg=bg, fg=fg,
                             activebackground=active, activeforeground=fg, relief="flat", bd=0,
                             highlightthickness=0, padx=14, pady=7,
                             font=(colors["font"], 10, "bold" if primary else "normal"), **kwargs)

        def make_legacy_label(parent_, text="", *, color=None, font=None, bg=None, **kwargs):
            label_bg = bg or _widget_bg(parent_)
            return tk.Label(parent_, text=text, bg=label_bg, fg=color or colors["fg"],
                            font=font or (colors["font"], 9), **kwargs)

        def make_legacy_text(parent_, *, height=5, state="normal", **kwargs):
            return tk.Text(parent_, height=height, state=state, wrap="word", bg=colors["input"],
                           fg=colors["fg"], insertbackground=colors["fg"], relief="flat", bd=0,
                           highlightthickness=1, highlightbackground=colors["border"],
                           highlightcolor=colors["accent"], font=(colors["font"], 10),
                           padx=8, pady=8, **kwargs)

        def make_legacy_listbox(parent_, **kwargs):
            return tk.Listbox(parent_, bg=colors["input"], fg=colors["fg"],
                              selectbackground=colors["accent"], selectforeground=colors["primary_fg"],
                              relief="flat", bd=0, highlightthickness=1,
                              highlightbackground=colors["border"],
                              highlightcolor=colors["accent"],
                              font=(colors["font"], 9), **kwargs)

        def make_legacy_radio(parent_, text, variable, value, *, bg=None, **kwargs):
            surface = bg or colors["bg"]
            return tk.Radiobutton(parent_, text=text, variable=variable, value=value,
                                  bg=surface, fg=colors["fg"], selectcolor=colors["input"],
                                  activebackground=surface, activeforeground=colors["fg"],
                                  font=(colors["font"], 9), relief="flat", bd=0,
                                  highlightthickness=0, **kwargs)

        def make_legacy_checkbutton(parent_, text, variable, *, bg=None, **kwargs):
            surface = bg or colors["bg"]
            return tk.Checkbutton(parent_, text=text, variable=variable,
                                  bg=surface, fg=colors["fg"], selectcolor=colors["input"],
                                  activebackground=surface, activeforeground=colors["fg"],
                                  font=(colors["font"], 9), relief="flat", bd=0,
                                  highlightthickness=0, **kwargs)

        def make_legacy_option_menu(parent_, variable, *values, bg=None, **kwargs):
            surface = bg or colors["bg"]
            menu = tk.OptionMenu(parent_, variable, *values)
            menu.configure(bg=surface, fg=colors["fg"], activebackground=colors["hover"],
                           activeforeground=colors["fg"], relief="flat", bd=0,
                           highlightthickness=0, font=(colors["font"], 9), **kwargs)
            menu["menu"].configure(bg=colors["input"], fg=colors["fg"],
                                    activebackground=colors["accent"],
                                    activeforeground=colors["primary_fg"],
                                    font=(colors["font"], 9))
            return menu

        def make_legacy_scrollbar(parent_, command):
            return tk.Scrollbar(parent_, orient="vertical", command=command,
                                bg=colors["input"], troughcolor=colors["bg"],
                                activebackground=colors["accent"], relief="flat", bd=0,
                                highlightthickness=0)

        def make_text(parent_, *, height=5, state="normal"):
            return tk.Text(parent_, height=height, state=state, wrap="word", bg=colors["input"],
                           fg=colors["fg"], insertbackground=colors["fg"], relief="flat", bd=0,
                           highlightthickness=1, highlightbackground=colors["border"],
                           highlightcolor=colors["accent"], font=(colors["font"], 10), padx=8, pady=8)

        def make_entry(parent_, **kwargs):
            return tk.Entry(parent_, bg=colors["input"], fg=colors["fg"], insertbackground=colors["fg"],
                            relief="flat", bd=0, highlightthickness=1, highlightbackground=colors["border"],
                            highlightcolor=colors["accent"], font=(colors["font"], 10), **kwargs)

        header = tk.Frame(window, bg=colors["bg"], padx=16, pady=14)
        header.pack(fill="x")
        make_label(header, _WORKBENCH_COPY["title"], size=18, bold=True, color=colors["accent"]).pack(anchor="w")
        make_label(header, _WORKBENCH_COPY["intro"], color=colors["dim"]).pack(anchor="w", pady=(3, 8))
        stage_frame = tk.Frame(header, bg=colors["bg"])
        stage_frame.pack(anchor="w")
        stage_labels = {}
        for index, stage in enumerate(_WORKBENCH_STAGES):
            if index:
                make_label(stage_frame, "·", color=colors["border"]).pack(side="left", padx=5)
            stage_labels[stage] = make_label(stage_frame, stage, color=colors["dim"])
            stage_labels[stage].pack(side="left")

        content = tk.Frame(window, bg=colors["bg"], padx=16)
        content.pack(fill="both", expand=True)
        canvas = tk.Canvas(content, bg=colors["bg"], highlightthickness=0, bd=0)
        scrollbar = make_legacy_scrollbar(content, canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=colors["bg"])
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(inner_id, width=event.width))

        def set_stage(stage):
            for name, label in stage_labels.items():
                label.configure(fg=colors["accent"] if name == stage else colors["dim"],
                                font=(colors["font"], 10, "bold" if name == stage else "normal"))

        prompt_card = tk.Frame(inner, bg=colors["panel"], padx=12, pady=12,
                               highlightbackground=colors["border"], highlightthickness=1)
        prompt_card.pack(fill="x", pady=(0, 12))
        prompt_header = tk.Frame(prompt_card, bg=colors["panel"])
        prompt_header.pack(fill="x")
        make_label(prompt_header, _WORKBENCH_COPY["prompt_label"], size=11, bold=True).pack(side="left")
        prompt_text = make_text(prompt_card, height=10)
        prompt_text.pack(fill="x", pady=(7, 0))
        baseline_content = getattr(self, "ui_baseline_content", None)
        if baseline_content:
            prompt_text.insert("1.0", baseline_content)

        # ── 从 PromptBox 内部选择输入材料（单条 / 多选 / 整文件夹）──
        # 只从内部 prompt 集合选取（ui_available_snippets / ui_available_categories），
        # 不访问外部磁盘；选中的多条按「# 标题 + content」拼进原提示词输入框。
        # 视觉：左侧树形文件夹（含子分类，点击即勾选/取消其下全部 prompt），
        #        右侧预览已选内容，全部沿用主视觉 colors 变量，与产品主题一致。
        def _open_internal_picker() -> None:
            internal_snippets = getattr(self, "ui_available_snippets", None) or []
            categories = getattr(self, "ui_available_categories", None) or []
            if not internal_snippets:
                messagebox.showinfo(
                    "从 PromptBox 选择", "PromptBox 里还没有任何提示词，请先新建或导入。",
                    parent=window,
                )
                return

            try:
                import tkinter.ttk as ttk
            except ImportError:
                ttk = None

            picker = tk.Toplevel(window)
            picker.title("从 PromptBox 选择输入材料")
            picker.geometry("760x540")
            picker.configure(bg=colors["bg"])
            picker.transient(window)
            picker.grab_set()

            # 已选集合：snippet_id → snippet（树节点勾选 + 右侧预览共用）
            selected: dict[str, dict[str, Any]] = {}

            def _snippet_content(s: dict[str, Any]) -> str:
                """取该 prompt 当前应展示的正文（稳定版优先 → 当前版 → 顶层）。
                复用模块级 snippet_display_content，语义与主程序一致。"""
                return snippet_display_content(s)

            def cat_children(parent_id: str) -> list[str]:
                return [c["id"] for c in categories if c.get("parent_id") == parent_id]

            def cat_tree_roots() -> list[str]:
                return [c["id"] for c in categories if c.get("parent_id") in (None, "")]

            def cat_label(cid: str) -> str:
                for c in categories:
                    if c.get("id") == cid:
                        return c.get("name") or "未命名"
                return "未命名"

            def snippets_in(cat_id: str) -> list[dict[str, Any]]:
                # 当前分类 + 全部子孙分类下的非删除 prompt
                ids = {cat_id}
                frontier = [cat_id]
                while frontier:
                    cur = frontier.pop()
                    for child in cat_children(cur):
                        if child not in ids:
                            ids.add(child)
                            frontier.append(child)
                return [s for s in internal_snippets
                        if not s.get("_deleted") and s.get("category_id") in ids and (_snippet_content(s) or s.get("title"))]

            def all_snippets() -> list[dict[str, Any]]:
                return [s for s in internal_snippets
                        if not s.get("_deleted") and (_snippet_content(s) or s.get("title"))]

            # ── ttk 样式就地注册（幂等，保证独立打开工作台也有主视觉）──
            style_name = "PB.WorkbenchPicker.Treeview"
            if ttk is not None:
                _st = ttk.Style()
                _st.theme_use("clam")
                _st.configure(style_name,
                              background=colors["input"], foreground=colors["fg"],
                              fieldbackground=colors["input"], font=(colors["font"], 10),
                              rowheight=30, borderwidth=0)
                _st.map(style_name, background=[("selected", colors["accent"])],
                        foreground=[("selected", colors["primary_fg"])])

            # ── 布局：左树 + 右预览 ──
            body = tk.Frame(picker, bg=colors["bg"])
            body.pack(fill="both", expand=True, padx=12, pady=(12, 4))
            left_frame = tk.Frame(body, bg=colors["bg"])
            left_frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
            right_frame = tk.Frame(body, bg=colors["bg"])
            right_frame.pack(side="left", fill="both", expand=True, padx=(8, 0))

            make_label(left_frame, "选择分类（勾选即选中该分类及子分类下的所有提示词）：",
                       color=colors["fg"], size=10).pack(anchor="w")
            tree_frame = tk.Frame(left_frame, bg=colors["bg"])
            tree_frame.pack(fill="both", expand=True, pady=(5, 0))
            cat_tree = ttk.Treeview(tree_frame, show="tree", style=style_name, selectmode="none") if ttk else None
            if cat_tree is not None:
                vsb = make_legacy_scrollbar(tree_frame, cat_tree.yview)
                cat_tree.configure(yscrollcommand=vsb.set)
                cat_tree.pack(side="left", fill="both", expand=True)
                vsb.pack(side="right", fill="y")
            else:
                make_label(tree_frame, "（当前环境无 ttk，树形不可用）", color=colors["dim"], size=10).pack(anchor="w")

            # 已选预览
            make_label(right_frame, "已选内容预览：", color=colors["fg"], size=10).pack(anchor="w")
            preview_list = make_legacy_listbox(right_frame, height=14, selectmode="single")
            preview_list.pack(fill="both", expand=True, pady=(5, 0))

            selected_lbl_var = tk.StringVar(value="已选 0 条")
            make_label(picker, "", color=colors["dim"], size=10, textvariable=selected_lbl_var).pack(anchor="w", padx=12)

            # ── 树节点 ↔ prompt 映射 ──
            node_to_cat: dict[str, str] = {}   # tree item -> category id

            def _insert_cat_node(parent_item: str, cid: str) -> None:
                item = cat_tree.insert(parent_item, "end", text="☐ " + cat_label(cid))
                node_to_cat[item] = cid
                for child in cat_children(cid):
                    _insert_cat_node(item, child)

            def build_tree() -> None:
                cat_tree.delete(*cat_tree.get_children())
                node_to_cat.clear()
                for root_cid in cat_tree_roots():
                    _insert_cat_node("", root_cid)
                # 「全部」根节点
                all_item = cat_tree.insert("", 0, text="☐ 全部")
                node_to_cat[all_item] = "__all__"

            def refresh_selected_label() -> None:
                selected_lbl_var.set(f"已选 {len(selected)} 条")

            def refresh_preview() -> None:
                preview_list.delete(0, "end")
                for s in internal_snippets:
                    if s.get("id") in selected:
                        preview_list.insert("end", s.get("title") or "未命名提示词")
                refresh_selected_label()

            def update_check_marks() -> None:
                # 根据勾选状态重绘节点文字前缀
                for item, cid in node_to_cat.items():
                    if cid == "__all__":
                        label = "☑ 全部" if len(selected) == len(all_snippets()) and all_snippets() else "☐ 全部"
                    else:
                        # 该分类（含子孙）下所有可选的都进 selected 才显示勾选
                        cat_items = snippets_in(cid)
                        full = bool(cat_items) and all(s["id"] in selected for s in cat_items)
                        label = ("☑ " if full else "☐ ") + cat_label(cid)
                    cat_tree.item(item, text=label)

            def toggle_node(item: str) -> None:
                cid = node_to_cat.get(item)
                if cid is None:
                    return
                if cid == "__all__":
                    # 全选 / 全不选
                    if len(selected) == len(all_snippets()) and all_snippets():
                        selected.clear()
                    else:
                        for s in all_snippets():
                            selected[s["id"]] = s
                else:
                    cat_items = snippets_in(cid)
                    cat_ids = {s["id"] for s in cat_items}
                    if cat_ids and all(sid in selected for sid in cat_ids):
                        for sid in cat_ids:
                            selected.pop(sid, None)
                    else:
                        for s in cat_items:
                            selected[s["id"]] = s
                update_check_marks()
                refresh_preview()

            if cat_tree is not None:
                cat_tree.bind("<Button-1>", lambda event: _tree_click(event))
                build_tree()

            def _tree_click(event) -> None:
                region = cat_tree.identify("region", event.x, event.y)
                if region == "cell" or region == "tree":
                    item = cat_tree.identify_row(event.y)
                    if item:
                        toggle_node(item)
                cat_tree.selection_remove(*cat_tree.selection())

            # ── 底部操作 ──
            def clear_all() -> None:
                selected.clear()
                self.ui_branch_payload = None
                update_check_marks()
                refresh_preview()

            def confirm() -> None:
                if not selected:
                    messagebox.showinfo("从 PromptBox 选择", "还没有选中任何提示词。", parent=picker)
                    return
                chunks = snippet_merge_chunks(internal_snippets, set(selected.keys()))
                if not chunks:
                    messagebox.showinfo("从 PromptBox 选择", "选中的提示词没有可拼入的正文内容。", parent=picker)
                    return
                merged = branch_display_text(build_branch_payload(internal_snippets, set(selected.keys()), categories))
                self.ui_branch_payload = build_branch_payload(internal_snippets, set(selected.keys()), categories)
                existing = prompt_text.get("1.0", "end-1c").strip()
                if existing:
                    prompt_text.insert("end", f"\n\n{merged}")
                else:
                    prompt_text.insert("1.0", merged)
                picker.destroy()
                refresh_variable_inputs()

            btn_row = tk.Frame(picker, bg=colors["bg"])
            btn_row.pack(fill="x", padx=12, pady=(6, 12))
            make_button(btn_row, "清空重选", clear_all).pack(side="left", padx=(0, 6))
            make_button(btn_row, "确定拼接", confirm, primary=True).pack(side="right")

        # 从 PromptBox 内部选择单条/多选/整文件夹，拼接为输入材料（复用内部集合，不访问外部磁盘）
        make_button(prompt_header, "从 PromptBox 选择", _open_internal_picker).pack(side="right")

        variable_entries: dict[str, Any] = {}
        variable_frame = tk.Frame(inner, bg=colors["bg"])
        variable_frame.pack(fill="x", pady=(0, 12))

        def refresh_variable_inputs(*_args: Any) -> None:
            for child in variable_frame.winfo_children():
                child.destroy()
            variable_entries.clear()
            prompt_contents = [prompt_text.get("1.0", "end-1c")]
            candidate_data = self.analysis.get("candidate") if self.analysis else None
            if isinstance(candidate_data, dict) and candidate_data.get("content"):
                prompt_contents.append(candidate_data["content"])
            template = PromptTemplate.from_text(
                "\n".join(prompt_contents),
                getattr(self, "ui_variable_definitions", {}),
            )
            if not template.variables:
                return
            make_label(variable_frame, "变量", size=10, bold=True).pack(anchor="w")
            for variable in template.variables:
                row = tk.Frame(variable_frame, bg=colors["bg"])
                row.pack(fill="x", pady=3)
                label = "{" + variable["name"] + "}"
                if variable["description"]:
                    label += "：" + variable["description"]
                if variable["example"]:
                    label += "（例如：" + variable["example"] + "）"
                make_label(row, label, width=42, anchor="w", color=colors["dim"]).pack(side="left")
                entry = make_entry(row)
                entry.pack(side="left", fill="x", expand=True)
                variable_entries[variable["name"]] = entry

        prompt_text.bind("<KeyRelease>", refresh_variable_inputs)
        refresh_variable_inputs()

        # ── 补充信息（可选，默认折叠）──
        optional_frame = tk.Frame(inner, bg=colors["bg"])
        optional_frame.pack(fill="x", pady=(0, 12))
        optional_header = tk.Frame(optional_frame, bg=colors["bg"])
        optional_header.pack(fill="x")
        make_label(optional_header, _WORKBENCH_COPY["optional_label"], size=11, bold=True).pack(side="left")
        optional_toggle_var = tk.StringVar(value="展开")
        make_legacy_label(optional_header, "", color=colors["accent"], bg=colors["bg"],
                          font=(colors["font"], 10), textvariable=optional_toggle_var,
                          cursor="hand2").pack(side="right")
        optional_body = tk.Frame(optional_frame, bg=colors["panel"], padx=12, pady=10)
        optional_body.pack(fill="x", pady=(7, 0))
        optional_body.pack_forget()

        def toggle_optional() -> None:
            if optional_body.winfo_ismapped():
                optional_body.pack_forget()
                optional_toggle_var.set("展开")
            else:
                optional_body.pack(fill="x", pady=(7, 0))
                optional_toggle_var.set("收起")

        optional_header.bind("<Button-1>", lambda _event: toggle_optional())

        make_label(optional_body, "失败输出（可选）", color=colors["dim"]).pack(anchor="w")
        output_text = make_text(optional_body, height=3)
        output_text.pack(fill="x", pady=(3, 0))
        make_label(optional_body, "本次材料（可选）", color=colors["dim"]).pack(anchor="w", pady=(8, 0))
        comparison_text = make_text(optional_body, height=3)
        comparison_text.pack(fill="x", pady=(3, 0))
        make_label(optional_body, "优化目标（可选）", color=colors["dim"]).pack(anchor="w", pady=(8, 0))
        goal_entry = make_entry(optional_body)
        goal_entry.pack(fill="x", pady=(3, 0))

        # ── 业务上下文（Context）面板：优化前供 Agent 参考；验证时作为双侧 Prompt 的共同运行条件 ──
        context_frame = tk.Frame(optional_body, bg=colors["panel"], padx=10, pady=10,
                                 highlightbackground=colors["border"], highlightthickness=1)
        context_frame.pack(fill="x", pady=(12, 0))
        make_label(context_frame, _WORKBENCH_COPY["context_summary"], size=10, bold=True).pack(anchor="w")
        make_label(
            context_frame,
            "你长期所处的业务环境（项目背景、公司/品类/团队背景）；验证时会同时提供给原版和候选版，作为共同运行条件。",
            color=colors["dim"],
        ).pack(anchor="w", pady=(2, 6))
        ctx_buttons = tk.Frame(context_frame, bg=colors["panel"])
        ctx_buttons.pack(fill="x")
        make_button(ctx_buttons, "选择文件夹", lambda: load_context_folder_ui(), primary=True).pack(side="left")
        make_button(ctx_buttons, "选择多个文件", lambda: load_context_files_ui()).pack(side="left", padx=(4, 0))
        make_button(ctx_buttons, "选择单文件", lambda: load_context_file_ui()).pack(side="left", padx=(4, 0))
        make_button(ctx_buttons, "载入剪贴板", lambda: load_clipboard_ui()).pack(side="left", padx=(4, 0))
        make_button(ctx_buttons, "清空", lambda: clear_context(), danger=True).pack(side="left", padx=(4, 0))
        context_info_var = tk.StringVar(value="未载入上下文")
        make_label(context_frame, "", anchor="w", color=colors["dim"]).pack_forget()
        context_status_label = make_label(context_frame, "", anchor="w", color=colors["dim"])
        context_status_label.pack(fill="x", pady=(6, 4))
        context_info_var.trace_add("write", lambda *_args: context_status_label.configure(text=context_info_var.get()))
        context_text = make_text(context_frame, height=6)
        context_text.pack(fill="both", expand=True)
        make_label(context_frame, "清单状态和解析失败原因会在载入后显示；此文本区是实际发送内容。",
                   color=colors["dim"], wraplength=760, justify="left").pack(anchor="w", pady=(5, 0))
        manifest_text = make_text(context_frame, height=5, state="disabled")
        manifest_text.pack(fill="x", pady=(6, 0))
        ctx_label_var = tk.StringVar(value="")
        source_type_var = tk.StringVar(value="legacy_unknown")
        source_label_var = tk.StringVar(value="")
        context_scope_var = tk.StringVar(value="")
        user_confirmed_var = tk.BooleanVar(value=False)

        provenance_frame = tk.Frame(context_frame, bg=colors["panel"])
        provenance_frame.pack(fill="x", pady=(8, 0))
        make_label(provenance_frame, "样本来源", color=colors["dim"]).pack(side="left")
        source_menu = make_legacy_option_menu(
            provenance_frame,
            source_type_var,
            "real_business_replay",
            "historical_failure",
            "designed_boundary",
            "ai_synthetic",
            "legacy_unknown",
            bg=colors["panel"],
        )
        source_menu.pack(side="left", padx=(4, 8))
        make_label(provenance_frame, "来源标签", color=colors["dim"]).pack(side="left")
        make_entry(provenance_frame, textvariable=source_label_var, width=18).pack(side="left", padx=(4, 8))
        make_label(provenance_frame, "上下文范围", color=colors["dim"]).pack(side="left")
        make_entry(provenance_frame, textvariable=context_scope_var, width=18).pack(side="left", padx=(4, 8))
        make_legacy_checkbutton(
            provenance_frame,
            "我确认该来源声明",
            user_confirmed_var,
            bg=colors["panel"],
        ).pack(side="left")

        def refresh_context_info() -> None:
            content = _strip_text_widget_trailing_newline(context_text.get("1.0", tk.END))
            if not content:
                context_info_var.set("未载入上下文")
                return
            tokens = round(len(content) / 1.6)
            ctx_label = ctx_label_var.get().strip()
            source = f" 来源：{ctx_label}" if ctx_label else ""
            capacity_note = "容量未知，仅供估算"
            if self.ui_context_budget:
                capacity_note = "容量未知，仅供估算"
            context_info_var.set(
                f"已载入 {len(content):,} 字符（约 {tokens:,} token）{source} · {capacity_note}"
            )

        # ── 接通断线：验证面板默认复用优化阶段实际发送的业务背景 ──
        # 优先取优化时存下的 self.ui_context_text（1630），其次才是主编辑器带入的
        # snippet["context"]（self.ui_context）。框内已有内容则不重复插入。
        ui_context_text_value = getattr(self, "ui_context_text", None)
        ui_context = getattr(self, "ui_context", None)
        prefill_context = ui_context_text_value or ui_context
        if prefill_context and not context_text.get("1.0", "end-1c").strip():
            context_text.insert("1.0", prefill_context)
            ctx_label_var.set("提示词自带")
            refresh_context_info()

        def load_context_file_ui() -> None:
            path = filedialog.askopenfilename(parent=window)
            if path:
                load_context_selection_ui([path], path)

        def load_context_files_ui() -> None:
            paths = filedialog.askopenfilenames(parent=window)
            if paths:
                load_context_selection_ui(list(paths), "多文件")

        def load_context_folder_ui() -> None:
            path = filedialog.askdirectory(parent=window)
            if path:
                load_context_selection_ui([path], path)

        def load_context_selection_ui(paths: list[str], label: str) -> None:
            try:
                manifest, pack = build_context_from_selection(paths)
            except ValueError as exc:
                messagebox.showerror("业务背景", str(exc), parent=window)
                return
            context_text.delete("1.0", tk.END)
            context_text.insert("1.0", pack.text)
            manifest_text.configure(state="normal")
            manifest_text.delete("1.0", tk.END)
            manifest_lines = ["纳入 | 相对路径 | 类型 | 大小 | 状态 | 原因"]
            for item in manifest.entries:
                state = "纳入" if item.status == "included" else ("排除" if item.status == "excluded" else "失败")
                reason = item.reason or ""
                manifest_lines.append(f"{state} | {item.relative_path} | {item.suffix or '-'} | {item.size_bytes:,} B | {item.status} | {reason}")
            manifest_text.insert("1.0", "\\n".join(manifest_lines))
            manifest_text.configure(state="disabled")
            self.ui_context_pack = {
                "text": pack.text,
                "manifest": {"root_paths": manifest.root_paths, "rules_version": manifest.rules_version, "entries": [item.__dict__ for item in manifest.entries]},
                "budget": {"char_count": pack.sent_chars, "estimated_tokens": round(pack.sent_chars / 1.6), "capacity_status": "unknown", "can_send": True},
                "actions": pack.actions,
            }
            self.ui_context_manifest = self.ui_context_pack["manifest"]
            self.ui_context_budget = self.ui_context_pack["budget"]
            ctx_label_var.set(label)
            refresh_context_info()
            failures = [item for item in manifest.entries if item.status != "included"]
            if failures:
                messagebox.showinfo("业务背景", f"已组装 {pack.file_count} 个文件；{len(failures)} 个文件被排除或解析失败。", parent=window)

        def load_clipboard_ui() -> None:
            if pyperclip is None:
                messagebox.showerror("业务背景", "未安装 pyperclip，无法读取剪贴板。", parent=window)
                return
            content = pyperclip.paste()
            if not content or not content.strip():
                messagebox.showinfo("业务背景", "剪贴板为空。", parent=window)
                return
            context_text.delete("1.0", tk.END)
            context_text.insert("1.0", content)
            ctx_label_var.set("剪贴板")
            refresh_context_info()

        def clear_context() -> None:
            context_text.delete("1.0", tk.END)
            ctx_label_var.set("")
            refresh_context_info()

        context_text.bind("<KeyRelease>", lambda _e: refresh_context_info())

        mode_frame = tk.Frame(optional_body, bg=colors["panel"])
        mode_frame.pack(fill="x", pady=(12, 0))
        make_label(mode_frame, "优化方式", color=colors["dim"]).pack(side="left")
        mode_var = tk.StringVar(value="B")
        make_legacy_radio(mode_frame, "保留原意", mode_var, "B", bg=colors["panel"]).pack(side="left", padx=(10, 0))
        make_legacy_radio(mode_frame, "重新梳理结构", mode_var, "A", bg=colors["panel"]).pack(side="left", padx=(8, 0))

        result_frame = tk.Frame(inner, bg=colors["bg"])
        result_frame.pack(fill="x", pady=(0, 12))
        result_frame.pack_forget()
        # ── 优化结果分组标题：与「补充信息（可选）」同级，明确这是 AI 产物 ──
        result_header = tk.Frame(result_frame, bg=colors["bg"])
        result_header.pack(fill="x", pady=(20, 8))
        make_label(result_header, _WORKBENCH_COPY["result_section_label"], size=11, bold=True).pack(anchor="w")
        make_label(
            result_header,
            _WORKBENCH_COPY["result_section_hint"],
            color=colors["dim"],
        ).pack(anchor="w", pady=(2, 0))
        diagnosis_card = tk.Frame(result_frame, bg=colors["panel"], padx=12, pady=10)
        diagnosis_card.pack(fill="x", pady=(0, 8))
        make_label(diagnosis_card, "诊断", size=10, bold=True).pack(anchor="w")
        diagnosis_label = make_label(diagnosis_card, "", justify="left", anchor="w", color=colors["fg"])
        diagnosis_label.pack(fill="x", pady=(4, 0))
        change_card = tk.Frame(result_frame, bg=colors["panel"], padx=12, pady=10)
        change_card.pack(fill="x", pady=(0, 8))
        make_label(change_card, "修改说明", size=10, bold=True).pack(anchor="w")
        audit_label = make_label(change_card, "", justify="left", anchor="w", color=colors["dim"])
        audit_label.pack(fill="x", pady=(4, 0))
        candidate_card = tk.Frame(result_frame, bg=colors["panel"], padx=12, pady=10)
        candidate_card.pack(fill="x")
        make_label(candidate_card, _WORKBENCH_COPY["result_label"], size=11, bold=True).pack(anchor="w")
        candidate_text = make_text(candidate_card, height=12, state="disabled")
        candidate_text.pack(fill="x", pady=(7, 0))
        branch_text = make_text(candidate_card, height=12, state="disabled")
        branch_text.pack(fill="x", pady=(7, 0))
        branch_text.pack_forget()
        branch_decision_frame = tk.Frame(candidate_card, bg=colors["panel"])
        branch_decision_vars: dict[str, Any] = {}
        branch_decision_frame.pack_forget()
        # ── 验证与成对对比面板：候选生成后仍默认折叠 ──
        verify_frame = tk.Frame(result_frame, bg=colors["bg"])
        verify_frame.pack(fill="x", pady=(12, 0))
        verify_header = tk.Frame(verify_frame, bg=colors["bg"])
        verify_header.pack(fill="x")
        make_label(verify_header, _WORKBENCH_COPY["verify_label"], size=11, bold=True).pack(side="left")
        verify_toggle_var = tk.StringVar(value="展开")
        make_legacy_label(verify_header, "", color=colors["accent"], bg=colors["bg"],
                          font=(colors["font"], 10), textvariable=verify_toggle_var,
                          cursor="hand2").pack(side="right")
        verify_box = tk.Frame(verify_frame, bg=colors["panel"], padx=12, pady=10)
        verify_box.pack(fill="x", pady=(7, 0))
        verify_box.pack_forget()

        def toggle_verify() -> None:
            if verify_box.winfo_ismapped():
                verify_box.pack_forget()
                verify_toggle_var.set("展开")
            else:
                verify_box.pack(fill="x", pady=(7, 0))
                verify_toggle_var.set("收起")

        verify_header.bind("<Button-1>", lambda _event: toggle_verify())

        make_legacy_label(verify_box, "本次材料（单次代表性任务输入）：", color=colors["fg"]).pack(anchor="w")
        verification_input_text = make_legacy_text(verify_box, height=3, width=80)
        verification_input_text.pack(fill="x")

        def sync_verification_input_from_optimization() -> None:
            """接通断线：验证面板默认复用优化阶段已存的「本次材料」。

            在候选生成后调用：把优化时用户填的本次材料（存于
            ``self.case["comparison"]["input"]``）带入验证框，保证优化与
            验证走在同一条路径；用户仍可改（换场景验证），默认是优化时那份。
            """
            if self.case and self.case.get("comparison", {}).get("input"):
                verification_input_text.delete("1.0", tk.END)
                verification_input_text.insert("1.0", self.case["comparison"]["input"])

        # 多案例编排：案例字段在上方编辑，运行状态保存在控制器而非 UI 临时变量。
        cases_frame = tk.Frame(verify_box, bg=colors["panel"])
        cases_frame.pack(fill="x", pady=(4, 2))
        make_legacy_label(cases_frame, "验证案例（最多 5 条）：", color=colors["fg"]).pack(side="left")
        case_listbox = make_legacy_listbox(cases_frame, height=3, exportselection=False)
        case_listbox.pack(side="left", fill="x", expand=True, padx=4)
        case_button_frame = tk.Frame(cases_frame, bg=colors["panel"])
        case_button_frame.pack(side="left")

        # 并排双栏输出区
        panes_frame = tk.Frame(verify_box, bg=colors["panel"])
        panes_frame.pack(fill="both", expand=True, pady=(4, 4))

        baseline_col = tk.Frame(panes_frame, bg=colors["panel"])
        baseline_col.pack(side="left", fill="both", expand=True, padx=(0, 3))
        baseline_title_var = tk.StringVar(value="基线版本输出 (Baseline)")
        make_legacy_label(baseline_col, "", color=colors["fg"], textvariable=baseline_title_var,
                          font=(colors["font"], 9, "bold"), anchor="w").pack(fill="x")
        baseline_output_text = make_legacy_text(baseline_col, height=8, width=38, state="disabled")
        baseline_output_text.pack(fill="both", expand=True)

        candidate_col = tk.Frame(panes_frame, bg=colors["panel"])
        candidate_col.pack(side="right", fill="both", expand=True, padx=(3, 0))
        candidate_title_var = tk.StringVar(value="候选版本输出 (Candidate)")
        make_legacy_label(candidate_col, "", color=colors["fg"], textvariable=candidate_title_var,
                          font=(colors["font"], 9, "bold"), anchor="w").pack(fill="x")
        candidate_output_text = make_legacy_text(candidate_col, height=8, width=38, state="disabled")
        candidate_output_text.pack(fill="both", expand=True)

        # 案例人工裁决单选区
        verdict_frame = tk.Frame(verify_box, bg=colors["panel"])
        verdict_frame.pack(fill="x", pady=(2, 2))
        make_legacy_label(verdict_frame, "人工裁决：", color=colors["fg"]).pack(side="left")
        pairwise_verdict_var = tk.StringVar(value="undecided")
        make_legacy_radio(verdict_frame, "候选更优", pairwise_verdict_var, "candidate_better", bg=colors["panel"]).pack(side="left", padx=4)
        make_legacy_radio(verdict_frame, "两者持平", pairwise_verdict_var, "equal", bg=colors["panel"]).pack(side="left", padx=4)
        make_legacy_radio(verdict_frame, "基线更优", pairwise_verdict_var, "baseline_better", bg=colors["panel"]).pack(side="left", padx=4)
        make_legacy_radio(verdict_frame, "暂不判断", pairwise_verdict_var, "undecided", bg=colors["panel"]).pack(side="left", padx=4)

        make_legacy_label(verify_box, "验证结论 / 评估备注：", color=colors["fg"]).pack(anchor="w")
        verification_note_text = make_legacy_text(verify_box, height=2, width=80)
        verification_note_text.pack(fill="x")

        rating_frame = tk.Frame(verify_box, bg=colors["panel"])
        rating_frame.pack(fill="x", pady=(8, 0))
        make_label(rating_frame, "验证评分（1-5）", color=colors["dim"]).pack(side="left")
        rating_entry = make_entry(rating_frame, width=8)
        rating_entry.pack(side="left", padx=(8, 0))
        review_frame = tk.Frame(verify_box, bg=colors["panel"])
        review_frame.pack(fill="x", pady=(10, 0))

        def render_branch_decisions(branch_value: dict[str, Any] | None) -> None:
            for child in branch_decision_frame.winfo_children():
                child.destroy()
            branch_decision_vars.clear()
            if not branch_value:
                branch_decision_frame.pack_forget()
                return
            branch_decision_frame.pack(fill="x", pady=(8, 0))
            make_label(
                branch_decision_frame,
                "逐条人工裁决（写回前必须全部选择）：",
                color=colors["fg"], size=10, bold=True,
            ).pack(anchor="w")
            labels = {"unchanged": "保留", "modified": "修改", "merged": "合并", "split": "拆分", "added": "新增", "removed": "删除"}
            for item in branch_value.get("items", []):
                item_id = str(item.get("id") or "")
                if not item_id:
                    continue
                path = "/".join(item.get("category_path") or [])
                title = item.get("title") or "未命名提示词"
                header = f"[{labels.get(item.get('change_type'), item.get('change_type', '变更'))}] {path + '/' if path else ''}{title}"
                source_ids = ", ".join(item.get("source_ids") or []) or "无来源（新增）"
                make_label(
                    branch_decision_frame,
                    f"{header} · 来源：{source_ids}",
                    color=colors["fg"], anchor="w", wraplength=760,
                ).pack(anchor="w", pady=(6, 0))
                if item.get("change_reason"):
                    make_label(
                        branch_decision_frame, f"理由：{item['change_reason']}",
                        color=colors["dim"], anchor="w", wraplength=760,
                    ).pack(anchor="w")
                decision_var = tk.StringVar(value="")
                branch_decision_vars[item_id] = decision_var
                options = tk.Frame(branch_decision_frame, bg=colors["panel"])
                options.pack(fill="x")
                make_legacy_radio(options, "采纳", decision_var, "adopt", bg=colors["panel"]).pack(side="left")
                make_legacy_radio(options, "拒绝", decision_var, "reject", bg=colors["panel"]).pack(side="left", padx=(8, 0))
                make_legacy_radio(options, "保留原 Prompt", decision_var, "keep_original", bg=colors["panel"]).pack(side="left", padx=(8, 0))

        def collect_branch_decisions() -> None:
            if not (self.analysis and self.analysis.get("branch_candidate")):
                return
            self.set_branch_decisions({
                item_id: variable.get()
                for item_id, variable in branch_decision_vars.items()
            })

        def show_candidate() -> None:
            view = self.get_view()
            result_frame.pack(fill="x", pady=(0, 12))
            set_stage("候选")
            # ── 接通断线：候选生成后，把优化阶段实际使用的「业务背景」
            #    与「本次材料」带入验证面板，保证优化与验证走同一条路径。──
            sync_verification_input_from_optimization()
            baseline_context = getattr(self, "ui_context_text", None)
            if baseline_context:
                context_text.delete("1.0", tk.END)
                context_text.insert("1.0", baseline_context)
                if not ctx_label_var.get().strip():
                    ctx_label_var.set("提示词自带")
                refresh_context_info()
            diagnosis_label.config(text=view["diagnosis"] or "未提供额外诊断。")
            audit_label.config(
                text=_format_audit_summary(
                    view.get("audit"),
                    view.get("audit_status"),
                    view.get("quick_check"),
                    view.get("resolved_issue_codes"),
                    view.get("unresolved_issue_codes"),
                    view.get("mode"),
                )
            )
            candidate_text.config(state="normal")
            candidate_text.delete("1.0", tk.END)
            if view["candidate"]:
                candidate_text.insert("1.0", view["candidate"]["content"])
            candidate_text.config(state="disabled")
            branch_value = view.get("branch_candidate")
            if branch_value:
                baseline_title_var.set("原分支输出 (Baseline Branch)")
                candidate_title_var.set("重构分支输出 (Restructured Branch)")
            else:
                baseline_title_var.set("基线版本输出 (Baseline)")
                candidate_title_var.set("候选版本输出 (Candidate)")
            branch_text.config(state="normal")
            branch_text.delete("1.0", tk.END)
            if branch_value:
                branch_text.insert("1.0", format_branch_candidate(branch_value))
                branch_text.pack(fill="x", pady=(7, 0))
            else:
                branch_text.pack_forget()
            render_branch_decisions(branch_value)
            for child in review_frame.winfo_children():
                child.destroy()
            make_button(review_frame, "继续编辑", lambda: choose("edit")).pack(side="left")
            make_button(review_frame, "丢弃候选", lambda: choose("reject"), danger=True).pack(side="left", padx=(8, 0))
            make_button(review_frame, "运行比较", run_pairwise_verify_ui, primary=True).pack(side="right")

        last_pairwise_run: dict[str, Any] = {}

        def refresh_case_list() -> None:
            case_listbox.delete(0, tk.END)
            for case in self.get_pairwise_cases():
                state = "已运行" if case["run"] is not None else "待运行"
                verdict = case["verdict"] or "未裁决"
                case_listbox.insert(tk.END, f"{case['id']} · {state} · {verdict}")

        def add_case_ui() -> None:
            try:
                self.add_pairwise_case(
                    context_label=ctx_label_var.get(),
                    context_text=_strip_text_widget_trailing_newline(context_text.get("1.0", tk.END)),
                    user_input=_strip_text_widget_trailing_newline(verification_input_text.get("1.0", tk.END)),
                    source_type=source_type_var.get(),
                    source_label=source_label_var.get().strip(),
                    context_scope=context_scope_var.get().strip(),
                    user_confirmed=user_confirmed_var.get(),
                )
                refresh_case_list()
            except ValueError as exc:
                messagebox.showerror("验证案例", str(exc), parent=window)

        def remove_case_ui() -> None:
            selection = case_listbox.curselection()
            if not selection:
                return
            case = self.get_pairwise_cases()[selection[0]]
            self.remove_pairwise_case(case["id"])
            refresh_case_list()

        def select_case_ui(_event: Any = None) -> None:
            selection = case_listbox.curselection()
            if not selection:
                return
            case = self.select_pairwise_case(self.get_pairwise_cases()[selection[0]]["id"])
            if case is None:
                return
            context_text.delete("1.0", tk.END)
            context_text.insert("1.0", case["context_text"])
            ctx_label_var.set(case["context_label"])
            source_type_var.set(case["source_type"])
            source_label_var.set(case["source_label"])
            context_scope_var.set(case["context_scope"])
            user_confirmed_var.set(case["user_confirmed"])
            verification_input_text.delete("1.0", tk.END)
            verification_input_text.insert("1.0", case["user_input"])

        make_button(case_button_frame, "新增案例", add_case_ui).pack(side="left")
        make_button(case_button_frame, "删除案例", remove_case_ui, danger=True).pack(side="left", padx=(4, 0))
        case_listbox.bind("<<ListboxSelect>>", select_case_ui)

        def run_pairwise_verify_ui() -> None:
            """运行当前案例；若尚未建立案例则兼容旧版单案例流程。"""
            try:
                set_stage("验证")
                verify_toggle_var.set("收起")
                if not verify_box.winfo_ismapped():
                    verify_box.pack(fill="x", pady=(7, 0))

                if self.analysis is None or not self.analysis.get("candidate"):
                    raise ValueError("没有可验证的候选提示词，请先生成修复版本。")
                if not self.pairwise_cases:
                    self.add_pairwise_case(
                        context_label=ctx_label_var.get(),
                        context_text=_strip_text_widget_trailing_newline(context_text.get("1.0", tk.END)),
                        user_input=_strip_text_widget_trailing_newline(verification_input_text.get("1.0", tk.END)),
                        source_type=source_type_var.get(),
                        source_label=source_label_var.get().strip(),
                        context_scope=context_scope_var.get().strip(),
                        user_confirmed=user_confirmed_var.get(),
                    )
                case_id = self.active_pairwise_case_id or self.pairwise_cases[0]["id"]
                variables = {name: entry.get() for name, entry in variable_entries.items()}
                context_value = _strip_text_widget_trailing_newline(context_text.get("1.0", tk.END))
                overflow_action = None
                if len(context_value) > 80_000:
                    choice = messagebox.askyesno(
                        "上下文过长",
                        "上下文超过 80,000 字符。是否截断后发送？选择“否”将取消本次验证。",
                        parent=window,
                    )
                    overflow_action = "truncate" if choice else "cancel"
                result = self.run_pairwise_case(
                    case_id,
                    context_text=context_value,
                    user_input=_strip_text_widget_trailing_newline(verification_input_text.get("1.0", tk.END)),
                    context_label=ctx_label_var.get(),
                    variables=variables,
                    context_overflow_action=overflow_action,
                )
                if result.get("context_protection", {}).get("action") == "truncate":
                    messagebox.showinfo(
                        "上下文已截断",
                        "本次验证使用了截断后的上下文，记录中已保存原始长度和发送长度。",
                        parent=window,
                    )
                self.select_pairwise_case(case_id)

                baseline_title_var.set(f"基线版本输出 ({result['baseline_latency_ms']} ms)")
                baseline_output_text.config(state="normal")
                baseline_output_text.delete("1.0", tk.END)
                baseline_output_text.insert("1.0", result["baseline_output"])
                baseline_output_text.config(state="disabled")
                candidate_title_var.set(f"候选版本输出 ({result['candidate_latency_ms']} ms)")
                candidate_output_text.config(state="normal")
                candidate_output_text.delete("1.0", tk.END)
                candidate_output_text.insert("1.0", result["candidate_output"])
                candidate_output_text.config(state="disabled")
                last_pairwise_run.clear()
                last_pairwise_run.update(result)
                refresh_case_list()
            except ValueError as exc:
                messagebox.showerror("成对验证失败", str(exc), parent=window)
            except Exception as exc:
                messagebox.showerror("成对验证失败", str(exc), parent=window)

        def verify(passed: bool) -> None:
            try:
                collect_branch_decisions()
                raw_rating = rating_entry.get().strip()
                rating = int(raw_rating) if raw_rating else None
                input_content = _strip_text_widget_trailing_newline(verification_input_text.get("1.0", tk.END))
                note_content = _strip_text_widget_trailing_newline(verification_note_text.get("1.0", tk.END))

                # 多案例流程由控制器统一收集已运行案例，旧版无案例时保留单次验证兼容路径。
                if self.pairwise_cases:
                    selected_case = self._get_pairwise_case(self.active_pairwise_case_id or self.pairwise_cases[0]["id"])
                    if selected_case["run"] is not None:
                        self.set_pairwise_case_verdict(
                            selected_case["id"],
                            pairwise_verdict_var.get(),
                            note_content,
                        )
                    overall = pairwise_verdict_var.get()
                    self.record_pairwise_cases(
                        overall_conclusion=overall,
                        passed=passed,
                        summary_note=note_content,
                    )
                elif last_pairwise_run:
                    verdict_val = pairwise_verdict_var.get()
                    last_pairwise_run["verdict"] = verdict_val
                    last_pairwise_run["note"] = note_content
                    self.record_pairwise_verification(
                        runs=[last_pairwise_run],
                        overall_conclusion=verdict_val,
                        passed=passed,
                        summary_note=note_content,
                    )
                else:
                    self.record_verification(
                        input_content,
                        note_content,
                        rating,
                        passed,
                    )
                messagebox.showinfo("修复工作台", "验证结果与证据已保存", parent=window)
            except ValueError as exc:
                messagebox.showerror("修复工作台", str(exc), parent=window)
            except Exception as exc:
                messagebox.showerror("修复工作台", str(exc), parent=window)

        def choose(choice: str) -> None:
            try:
                self.record_choice(choice)
            except ValueError as exc:
                messagebox.showerror("修复工作台", str(exc), parent=window)
            except Exception as exc:
                messagebox.showerror("修复工作台", str(exc), parent=window)

        def generate() -> None:
            generate_button.config(state="disabled", text="正在优化…")
            window.update_idletasks()
            try:
                prompt = _strip_text_widget_trailing_newline(prompt_text.get("1.0", tk.END))
                if not prompt.strip():
                    raise ValueError("原提示词不能为空")
                self.ui_context_text = _strip_text_widget_trailing_newline(
                    context_text.get("1.0", tk.END)
                )
                if self.ui_context_pack:
                    self.ui_context_pack["text"] = self.ui_context_text
                self.start_optimization(
                    prompt=prompt,
                    output=_strip_text_widget_trailing_newline(output_text.get("1.0", tk.END)),
                    comparison_input=_strip_text_widget_trailing_newline(comparison_text.get("1.0", tk.END)),
                    task_goal=goal_entry.get(),
                )
                self.mode = mode_var.get()
                self.generate_candidate()
                show_candidate()
            except ValueError as exc:
                messagebox.showerror("修复工作台", str(exc), parent=window)
            except Exception as exc:
                messagebox.showerror("修复工作台", str(exc), parent=window)
            finally:
                generate_button.config(state="normal", text=_WORKBENCH_COPY["primary_action"])

        action_bar = tk.Frame(window, bg=colors["bg"], padx=16, pady=12)
        action_bar.pack(fill="x")
        generate_button = make_button(action_bar, _WORKBENCH_COPY["primary_action"], command=generate, primary=True)
        generate_button.pack(side="right")
        set_stage("输入")
        return window
