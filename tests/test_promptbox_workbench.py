import json
from pathlib import Path

import pytest

from promptbox_mvp.workbench import (
    RepairWorkbench,
    _format_audit_summary,
    _strip_text_widget_trailing_newline,
)


WORKBENCH_SOURCE = Path(__file__).parents[1] / "promptbox_mvp" / "workbench.py"


class FakeService:
    def __init__(self):
        self.calls = []

    def repair(self, prompt, output="", comparison_input="", task_goal="", mode="", context=""):
        self.calls.append((prompt, output, comparison_input, task_goal, mode, context))
        return {
            "diagnosis": "缺少格式约束",
            "mode": "B",
            "candidate": "修复后的提示词",
            "reasons": ["补回格式约束"],
            "audit": {
                "task": "原始提示词。",
                "constraints": [],
                "output_contract": [],
                "coverage": {
                    "has_task": True,
                    "has_constraints": False,
                    "has_output_contract": False,
                    "has_scope_boundary": False,
                    "has_task_object": True,
                    "has_input_boundary": False,
                },
                "issues": [],
            },
            "audit_status": {"status": "available", "message": ""},
            "quick_check": {
                "required_changes": ["补充输出条数"],
                "preserve": ["保留任务"],
                "forbidden_changes": ["不得编造事实"],
                "acceptance_checks": ["包含条数要求"],
            },
            "resolved_issue_codes": ["missing_output_contract"],
            "unresolved_issue_codes": [],
        }


def test_workbench_theme_accepts_shared_theme_tokens_and_styles_legacy_controls():
    source = WORKBENCH_SOURCE.read_text(encoding="utf-8")
    assert "def _workbench_theme(shared_theme=None)" in source
    assert "shared_theme[\"bg\"]" in source
    assert "make_legacy_label" in source
    assert "make_legacy_button" not in source
    assert "def open_toplevel(self, parent: Any, theme: dict[str, str] | None = None)" in source
    assert "colors = _workbench_theme(theme)" in source
    assert "make_legacy_text" in source
    assert "make_legacy_listbox" in source
    assert "make_legacy_radio" in source
    assert "make_legacy_checkbutton" in source
    assert "make_legacy_option_menu" in source
    assert "make_legacy_scrollbar" in source


def make_workbench(adopt_candidate=None, save_case=None):
    return RepairWorkbench(
        FakeService(),
        save_case=save_case,
        adopt_candidate=adopt_candidate,
    )


def capture_bound_case(workbench):
    return workbench.capture_case(
        snippet_id="snip_1",
        base_version_id="ver_2",
        base_version_number=2,
        prompt="原始提示词",
        output="失败输出",
        comparison_input="验证输入",
        task_goal="提取待办",
    )


class FakeVerifyService(FakeService):
    def __init__(self):
        super().__init__()
        self.verify_calls = []

    def verify(
        self,
        candidate_prompt,
        context_text,
        user_input,
        context_label="",
        max_context_chars=None,
    ):
        self.verify_calls.append(
            (candidate_prompt, context_text, user_input, context_label, max_context_chars)
        )
        return {
            "output": "模型验证输出",
            "payload_chars": len(candidate_prompt) + len(context_text) + len(user_input),
            "context_chars": len(context_text),
            "truncated": False,
            "elapsed_ms": 42,
        }


# ── run_verify：验证调用逻辑（GUI 只做胶水，这里可测）────────────


def test_run_pairwise_verify_requires_same_hard_limit_confirmation():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "候选"}}

    with pytest.raises(ValueError, match="超过硬限"):
        workbench.run_pairwise_verify(
            "基线",
            "候选",
            "X" * 101,
            "输入",
            context_hard_limit=100,
        )


def test_run_pairwise_verify_executes_both_baseline_and_candidate():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "修复后 Prompt"}}

    result = workbench.run_pairwise_verify(
        baseline_prompt="原始 Prompt",
        candidate_prompt="修复后 Prompt",
        context_text="上下文材料",
        user_input="测试输入",
        context_label="案例1",
    )

    assert len(workbench.service.verify_calls) == 2
    # 第一次跑基线，第二次跑候选
    assert workbench.service.verify_calls[0][0] == "原始 Prompt"
    assert workbench.service.verify_calls[1][0] == "修复后 Prompt"
    assert result["baseline"]["output"] == "模型验证输出"
    assert result["candidate"]["output"] == "模型验证输出"
    assert result["baseline_latency_ms"] == 42
    assert result["candidate_latency_ms"] == 42


def test_record_pairwise_verification_adopts_only_when_passed_and_calls_callback():
    adopted_versions = []

    def fake_adopt(case):
        new_id = f"ver_adopted_{len(adopted_versions) + 1}"
        adopted_versions.append(new_id)
        return new_id

    saved_cases = []

    def fake_save(case):
        saved_cases.append(case)

    workbench = make_workbench(adopt_candidate=fake_adopt, save_case=fake_save)
    capture_bound_case(workbench)
    workbench.generate_candidate()

    runs = [
        {
                "id": "run_1",
                "source_type": "designed_boundary",
                "source_label": "工作台契约测试样本",
                "user_confirmed": True,
                "context_label": "案例1",
            "context_text": "背景",
            "user_input": "输入1",
            "baseline_output": "基线1",
            "candidate_output": "候选1",
            "baseline_latency_ms": 100,
            "candidate_latency_ms": 90,
            "verdict": "candidate_better",
            "note": "结构更清晰",
        }
    ]

    # 1. 验证通过
    case = workbench.record_pairwise_verification(
        runs=runs,
        overall_conclusion="candidate_better",
        passed=True,
        summary_note="成对验证通过",
    )

    assert case["status"] == "validated"
    assert case["adopted_version_id"] == "ver_adopted_1"
    assert len(saved_cases) == 1
    assert case["verification"]["overall_conclusion"] == "candidate_better"

    # 2. 已通过的不能再次验证采纳
    with pytest.raises(ValueError, match="already adopted"):
        workbench.record_pairwise_verification(
            runs=runs,
            overall_conclusion="candidate_better",
            passed=True,
        )


def test_record_pairwise_verification_failed_does_not_adopt():
    saved_cases = []
    workbench = make_workbench(adopt_candidate=lambda c: "ver_x", save_case=lambda c: saved_cases.append(c))
    capture_bound_case(workbench)
    workbench.generate_candidate()

    runs = [
        {
            "id": "run_1",
            "verdict": "baseline_better",
            "note": "候选版本退化",
        }
    ]

    case = workbench.record_pairwise_verification(
        runs=runs,
        overall_conclusion="baseline_better",
        passed=False,
        summary_note="验证失败，退回修改",
    )

    assert case["status"] == "validation_failed"
    assert case["adopted_version_id"] is None
    assert len(saved_cases) == 1

    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "修复后提示词"}}

    result = workbench.run_verify("修复后提示词", "上下文材料", "任务输入")

    assert workbench.service.verify_calls == [
        ("修复后提示词", "上下文材料", "任务输入", "", 20_000)
    ]
    assert result["output"] == "模型验证输出"
    assert result["context_chars"] == len("上下文材料")
    assert result["warning"] is None
    assert result["note"] is None


def test_run_verify_passes_context_label_to_service():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "提示词"}}

    workbench.run_verify("提示词", "上下文", "输入", context_label="交付文档.md")

    assert workbench.service.verify_calls[0][3] == "交付文档.md"


def test_run_verify_uses_max_context_chars_parameter():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "提示词"}}

    workbench.run_verify("提示词", "上下文", "输入", max_context_chars=5_000)

    assert workbench.service.verify_calls[0][4] == 5_000


def test_run_verify_soft_limit_warning_uses_1_6_token_estimate():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "提示词"}}
    context = "甲" * 100

    result = workbench.run_verify("提示词", context, "输入", soft_limit=50)

    assert result["warning"] is not None
    assert "100" in result["warning"]
    assert "62" in result["warning"]
    assert result["truncated"] is False


def test_run_verify_no_warning_below_soft_limit():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "提示词"}}

    result = workbench.run_verify("提示词", "短上下文", "输入", soft_limit=50)

    assert result["warning"] is None


def test_run_verify_reports_truncation_note_when_service_truncated():
    class TruncatingVerifyService(FakeVerifyService):
        def verify(self, *args, **kwargs):
            return {
                "output": "输出",
                "payload_chars": 10,
                "context_chars": 100,
                "truncated": True,
                "elapsed_ms": 1,
            }

    workbench = RepairWorkbench(TruncatingVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "提示词"}}

    result = workbench.run_verify("提示词", "X" * 100, "输入", max_context_chars=100)

    assert result["note"] is not None
    assert "截断" in result["note"]


def test_run_verify_requires_confirmation_when_context_exceeds_hard_limit():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "提示词"}}

    with pytest.raises(ValueError, match="超过硬限"):
        workbench.run_verify(
            "提示词",
            "X" * 101,
            "输入",
            soft_limit=50,
            max_context_chars=200,
            context_hard_limit=100,
        )

    assert workbench.service.verify_calls == []


def test_run_verify_truncates_after_explicit_hard_limit_confirmation():
    class TruncatingService(FakeVerifyService):
        def verify(self, candidate_prompt, context_text, user_input, context_label="", max_context_chars=None):
            limited = context_text[:max_context_chars]
            self.verify_calls.append((candidate_prompt, limited, user_input, context_label, max_context_chars))
            return {
                "output": "输出",
                "payload_chars": len(candidate_prompt) + len(limited) + len(user_input),
                "context_chars": len(limited),
                "truncated": len(limited) < len(context_text),
                "elapsed_ms": 1,
            }

    workbench = RepairWorkbench(TruncatingService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "提示词"}}

    result = workbench.run_verify(
        "提示词",
        "X" * 101,
        "输入",
        max_context_chars=200,
        context_hard_limit=100,
        context_overflow_action="truncate",
    )

    assert result["context_protection"]["action"] == "truncate"
    assert result["context_protection"]["source_chars"] == 101
    assert result["context_protection"]["hard_limit"] == 100
    assert workbench.service.verify_calls[0][1] == "X" * 100


def test_run_verify_rejects_invalid_context_overflow_action():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "提示词"}}

    with pytest.raises(ValueError, match="截断或取消"):
        workbench.run_verify(
            "提示词",
            "上下文",
            "输入",
            context_overflow_action="继续发送",
        )


def test_run_verify_requires_a_candidate():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = None

    with pytest.raises(ValueError, match="no candidate"):
        workbench.run_verify("提示词", "", "输入")


def test_run_verify_requires_candidate_content():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": ""}}

    with pytest.raises(ValueError, match="no candidate"):
        workbench.run_verify("", "", "输入")


def test_run_verify_propagates_transport_value_error():
    class FailingService(FakeVerifyService):
        def verify(self, *args, **kwargs):
            raise ValueError("未配置修复服务。")

    workbench = RepairWorkbench(FailingService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "提示词"}}

    with pytest.raises(ValueError, match="未配置修复服务。"):
        workbench.run_verify("提示词", "上下文", "输入")


def test_run_verify_falls_back_to_generic_input_label_when_empty():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "提示词"}}

    workbench.run_verify("提示词", "上下文", "   ")

    assert workbench.service.verify_calls[0][2] == "（本次输入）"


def test_strip_text_widget_trailing_newline_preserves_intentional_multiple_newlines():
    assert _strip_text_widget_trailing_newline("line\n\n") == "line\n"


def test_strip_text_widget_trailing_newline_preserves_backslash_and_n():
    assert _strip_text_widget_trailing_newline("ending\\n") == "ending\\n"
    assert _strip_text_widget_trailing_newline("endingn") == "endingn"


def test_strip_text_widget_trailing_newline_removes_tk_terminal_newline():
    assert _strip_text_widget_trailing_newline("line\n") == "line"


def test_capture_case_preserves_real_prompt_version_fields():
    workbench = make_workbench()
    case = capture_bound_case(workbench)

    assert case["status"] == "captured"
    assert case["snippet_id"] == "snip_1"
    assert case["base_version_id"] == "ver_2"
    assert case["base_version_number"] == 2
    assert case["failure"] == {"prompt": "原始提示词", "output": "失败输出"}
    assert case["comparison"] == {"input": "验证输入"}
    assert case["task_goal"] == "提取待办"
    assert workbench.case is case


def test_capture_rejects_invalid_contract_input():
    with pytest.raises(ValueError):
        make_workbench().capture_case(
            "snip_1", "ver_2", 2, "prompt", None, "input"
        )


def test_candidate_generation_does_not_call_adopt_callback():
    adopted = []
    workbench = make_workbench(adopted.append)
    capture_bound_case(workbench)

    candidate = workbench.generate_candidate()

    assert workbench.service.calls == [
        ("原始提示词", "失败输出", "验证输入", "提取待办", "", "")
    ]
    assert candidate["content"] == "修复后的提示词"
    assert workbench.analysis["diagnosis"] == "缺少格式约束"
    assert workbench.analysis["mode"] == "B"
    assert workbench.case["status"] == "candidate_ready"
    assert adopted == []


def test_start_optimization_accepts_prompt_only_and_generates_candidate():
    workbench = make_workbench()

    case = workbench.start_optimization("只有一条已有提示词")

    assert case["status"] == "captured"
    assert case["failure"]["prompt"] == "只有一条已有提示词"
    assert case["failure"]["output"] == ""
    assert case["comparison"]["input"] == ""

    candidate = workbench.generate_candidate()

    assert candidate["content"] == "修复后的提示词"
    assert workbench.case["status"] == "candidate_ready"
    assert workbench.service.calls == [
        ("只有一条已有提示词", "", "", "", "", "")
    ]


def test_start_optimization_uses_ui_baseline_when_available():
    workbench = make_workbench()
    workbench.ui_baseline = {
        "snippet_id": "snip_selected",
        "base_version_id": "ver_9",
        "base_version_number": 9,
    }

    case = workbench.start_optimization("提示词", output="失败", comparison_input="输入")

    assert case["snippet_id"] == "snip_selected"
    assert case["base_version_id"] == "ver_9"
    assert case["base_version_number"] == 9
    assert case["failure"]["output"] == "失败"
    assert case["comparison"]["input"] == "输入"


def test_generate_candidate_persists_diagnostic_summary_on_repair_case():
    workbench = make_workbench()
    workbench.capture_case("s1", "v1", 1, "原始提示词", "失败输出", "验证输入")

    workbench.generate_candidate()

    assert workbench.case["analysis"]["diagnosis"] == "缺少格式约束"
    assert workbench.case["analysis"]["mode"] == "B"


    with pytest.raises(ValueError):
        make_workbench().generate_candidate()


def test_generate_passes_selected_mode_to_service():
    workbench = make_workbench()
    workbench.mode = "A"
    capture_bound_case(workbench)

    workbench.generate_candidate()

    assert workbench.service.calls == [
        ("原始提示词", "失败输出", "验证输入", "提取待办", "A", "")
    ]


def test_generate_cannot_be_repeated():
    workbench = make_workbench()
    capture_bound_case(workbench)
    workbench.generate_candidate()

    with pytest.raises(ValueError):
        workbench.generate_candidate()


def test_failed_verification_saves_case_without_calling_adopt_callback():
    adopted = []
    saved = []
    workbench = make_workbench(adopted.append, saved.append)
    capture_bound_case(workbench)
    workbench.generate_candidate()

    case = workbench.record_verification("验证输入", "输出仍不完整", 2, passed=False)

    assert case["status"] == "validation_failed"
    assert case["adopted_version_id"] is None
    assert adopted == []
    assert saved == [case]


def test_passing_verification_calls_adopt_once_and_records_version_id():
    adopted = []
    saved = []

    def adopt(case):
        adopted.append(case["id"])
        return "ver_3"

    workbench = make_workbench(adopt, saved.append)
    capture_bound_case(workbench)
    workbench.generate_candidate()

    case = workbench.record_verification("验证输入", "输出完整", 5, passed=True)

    assert adopted == [case["id"]]
    assert case["status"] == "validated"
    assert case["adopted_version_id"] == "ver_3"
    assert saved == [case]



def test_failed_adoption_keeps_case_ready_for_a_retry():
    def fail_adoption(_case):
        raise RuntimeError("storage unavailable")

    workbench = make_workbench(fail_adoption)
    capture_bound_case(workbench)
    workbench.generate_candidate()

    with pytest.raises(RuntimeError, match="storage unavailable"):
        workbench.record_verification("输入", "通过", 5, passed=True)

    assert workbench.case["status"] == "candidate_ready"
    assert workbench.case["verification"]["status"] == "pending"
    assert workbench.case["adopted_version_id"] is None


def test_passing_verification_cannot_create_a_second_version():
    calls = []

    def adopt(_case):
        calls.append("called")
        return "ver_3"

    workbench = make_workbench(adopt)
    capture_bound_case(workbench)
    workbench.generate_candidate()
    workbench.record_verification("输入", "通过", 5, passed=True)

    with pytest.raises(ValueError, match="already adopted"):
        workbench.record_verification("输入", "再次通过", 5, passed=True)

    assert calls == ["called"]


def test_edit_and_reject_do_not_save_cases():
    for choice, expected_status in (("edit", "editing"), ("reject", "discarded")):
        saved = []
        workbench = make_workbench(save_case=saved.append)
        capture_bound_case(workbench)
        workbench.generate_candidate()

        case = workbench.record_choice(choice)

        assert case["status"] == expected_status
        assert saved == []


def test_get_view_contains_summary_and_does_not_expose_internal_references():
    workbench = make_workbench()
    capture_bound_case(workbench)
    workbench.generate_candidate()

    view = workbench.get_view()

    json.dumps(view, ensure_ascii=False)
    assert view == {
        "status": "candidate_ready",
        "diagnosis": "缺少格式约束",
        "mode": "B",
        "candidate": {
            "content": "修复后的提示词",
            "reasons": ["补回格式约束"],
        },
        "verdict": None,
        "audit": {
            "task": "原始提示词。",
            "constraints": [],
            "output_contract": [],
            "coverage": {
                "has_task": True,
                "has_constraints": False,
                "has_output_contract": False,
                "has_scope_boundary": False,
                "has_task_object": True,
                "has_input_boundary": False,
            },
            "issues": [],
        },
        "audit_status": {"status": "available", "message": ""},
        "quick_check": {
            "required_changes": ["补充输出条数"],
            "preserve": ["保留任务"],
            "forbidden_changes": ["不得编造事实"],
            "acceptance_checks": ["包含条数要求"],
        },
        "resolved_issue_codes": ["missing_output_contract"],
        "unresolved_issue_codes": [],
    }
    view["candidate"]["reasons"].append("外部修改")
    assert workbench.case["candidates"][0]["change_reasons"] == ["补回格式约束"]



def test_format_audit_summary_shows_quick_check_mode_and_model_issue_declarations():
    summary = _format_audit_summary(
        {
            "task": "总结会议。",
            "constraints": [],
            "output_contract": [],
            "coverage": {},
            "issues": [
                {
                    "code": "missing_output_contract",
                    "severity": "warning",
                    "evidence": "总结会议。",
                    "action": "补充输出契约",
                }
            ],
        },
        {"status": "available", "message": ""},
        {
            "required_changes": ["补充输出条数"],
            "preserve": ["保留任务"],
            "forbidden_changes": ["不得编造事实"],
            "acceptance_checks": ["包含条数要求"],
        },
        ["missing_output_contract"],
        [],
        "B",
    )

    assert "修复档位：意图保真微调（B 档）" in summary
    assert "本地速检参考（代码规则，可能漏检或误判，仅参考）：" in summary
    assert "本地速检建议（同上，仅参考）" in summary
    assert "建议必改：补充输出条数" in summary
    assert "建议保留：保留任务" in summary
    assert "建议禁止：不得编造事实" in summary
    assert "建议验收：包含条数要求" in summary
    assert "[模型声明已处理] 缺失输出契约" in summary
    assert "模型声明不等于人工验证结论。" in summary



    summary = _format_audit_summary(
        {
            "task": "总结会议。",
            "constraints": ["仅依据原文"],
            "output_contract": [],
            "coverage": {
                "has_task": True,
                "has_constraints": True,
                "has_output_contract": False,
                "has_scope_boundary": True,
                "has_task_object": False,
                "has_input_boundary": True,
            },
            "issues": [
                {
                    "code": "no_op_quality",
                    "severity": "info",
                    "evidence": "确保高质量输出",
                    "reason": "命中无验收条件的质量要求。",
                    "action": "删除空泛质量要求，改为可检查的约束或输出格式。",
                },
                {
                    "code": "constraint_conflict",
                    "severity": "error",
                    "evidence": "仅依据原文 / 补充背景知识",
                    "reason": "两条约束要求的信息来源互斥。",
                    "action": "删除其中一条，或写明两者的优先级。",
                },
            ],
        },
        {"status": "available", "message": ""},
    )

    assert "任务：总结会议。" in summary
    assert "范围边界：已识别" in summary
    assert "任务对象：未识别" in summary
    assert "输入边界：已识别" in summary
    assert summary.index("[错误] 约束冲突") < summary.index("[提示] 空泛质量要求")
    assert "证据：仅依据原文 / 补充背景知识" in summary
    assert "处理：删除其中一条，或写明两者的优先级。" in summary


def test_format_audit_summary_uses_labels_for_structural_completeness_rules():
    summary = _format_audit_summary(
        {
            "task": "请总结以下内容。",
            "constraints": [],
            "output_contract": ["输出三条要点"],
            "coverage": {
                "has_task": True,
                "has_constraints": False,
                "has_output_contract": True,
                "has_scope_boundary": False,
                "has_task_object": True,
                "has_input_boundary": False,
            },
            "issues": [
                {
                    "code": "missing_input_boundary",
                    "severity": "warning",
                    "evidence": "请总结以下内容。",
                    "reason": "任务明确引用材料，但未识别材料范围或引用方式。",
                    "action": "写明材料来源或处理边界，例如“仅依据以下文本”或“基于提供的 CSV”。",
                }
            ],
        }
    )

    assert "[警告] 缺失输入边界" in summary
    assert "任务对象：已识别" in summary
    assert "输入边界：未识别" in summary


def test_format_audit_summary_uses_constraint_executability_labels():
    for code, severity, label in (
        ("vague_constraint", "warning", "空泛约束词"),
        ("direct_constraint_conflict", "error", "直接约束冲突"),
        ("missing_constraint_priority", "warning", "缺失约束优先级"),
    ):
        summary = _format_audit_summary(
            {
                "task": "请总结会议记录。",
                "constraints": [],
                "output_contract": [],
                "coverage": {},
                "issues": [
                    {
                        "code": code,
                        "severity": severity,
                        "evidence": "证据",
                        "action": "处理",
                    }
                ],
            }
        )

        severity_label = "错误" if severity == "error" else "警告"
        assert f"[{severity_label}] {label}" in summary


def test_format_audit_summary_shows_only_unavailable_message_when_audit_failed():
    summary = _format_audit_summary(
        None,
        {"status": "unavailable", "message": "audit registry unavailable"},
    )

    assert summary == "本地速检暂不可用：audit registry unavailable"


def test_get_view_preserves_audit_unavailable_status_without_blocking_candidate():
    class UnavailableAuditService:
        def repair(self, *_args):
            return {
                "diagnosis": "缺少格式约束",
                "mode": "B",
                "candidate": "修复后的提示词",
                "reasons": ["补回格式约束"],
                "audit": None,
                "audit_status": {
                    "status": "unavailable",
                    "message": "audit registry unavailable",
                },
            }

    workbench = RepairWorkbench(UnavailableAuditService())
    capture_bound_case(workbench)
    workbench.generate_candidate()
    view = workbench.get_view()

    assert view["candidate"]["content"] == "修复后的提示词"
    assert view["audit"] is None
    assert view["audit_status"] == {
        "status": "unavailable",
        "message": "audit registry unavailable",
    }


def test_workbench_preserves_unicode_case_inputs():
    workbench = make_workbench()
    workbench.capture_case(
        "snip_unicode",
        "ver_unicode",
        1,
        "提示词 🚀",
        "失败：你好",
        "输入：世界",
        "目标：中文",
    )
    workbench.generate_candidate()

    assert workbench.service.calls[0] == (
        "提示词 🚀",
        "失败：你好",
        "输入：世界",
        "目标：中文",
        "",
        "",
    )
    assert workbench.get_view()["candidate"]["content"] == "修复后的提示词"


# ── V2.2 多案例成对验证编排 ────────────────────────────────────────


def test_run_pairwise_case_records_same_context_snapshot_for_both_prompts():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "修复后 Prompt"}}
    sample = workbench.add_pairwise_case(
        source_type="historical_failure",
        source_label="历史运行 run_03",
        context_scope="输入正文",
        context_text="上下文材料",
        user_input="提取待办",
        user_confirmed=True,
    )

    run = workbench.run_pairwise_case(sample["id"])

    assert run["source_type"] == "historical_failure"
    assert run["source_label"] == "历史运行 run_03"
    assert run["context_chars"] == len("上下文材料")
    assert run["source_chars"] == len("上下文材料")
    assert run["context_hash"].startswith("sha256:")
    assert run["truncated"] is False



    workbench = RepairWorkbench(FakeVerifyService())

    cases = [workbench.add_pairwise_case(user_input=f"输入{i}") for i in range(5)]

    assert len(cases) == 5
    assert len(workbench.get_pairwise_cases()) == 5
    assert [case["user_input"] for case in workbench.get_pairwise_cases()] == [
        f"输入{i}" for i in range(5)
    ]


def test_pairwise_workbench_rejects_more_than_five_cases():
    workbench = RepairWorkbench(FakeVerifyService())
    for _ in range(5):
        workbench.add_pairwise_case()

    with pytest.raises(ValueError, match="最多支持 5 条"):
        workbench.add_pairwise_case()


def test_pairwise_workbench_keeps_each_case_run_independent():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "修复后 Prompt"}}
    first = workbench.add_pairwise_case(context_label="案例1", user_input="输入1")
    second = workbench.add_pairwise_case(context_label="案例2", user_input="输入2")

    first_run = workbench.run_pairwise_case(first["id"], context_text="背景1")

    cases = workbench.get_pairwise_cases()
    assert cases[0]["run"] == first_run
    assert cases[1]["run"] is None
    assert cases[1]["user_input"] == "输入2"


def test_pairwise_workbench_only_records_executed_cases():
    adopted = []
    workbench = RepairWorkbench(
        FakeVerifyService(),
        adopt_candidate=lambda case: adopted.append(case["id"]) or "ver_3",
    )
    capture_bound_case(workbench)
    workbench.generate_candidate()
    first = workbench.add_pairwise_case(
        user_input="输入1",
        source_type="designed_boundary",
        source_label="工作台测试样本",
        user_confirmed=True,
    )
    workbench.run_pairwise_case(first["id"], context_text="背景")
    workbench.set_pairwise_case_verdict(first["id"], "candidate_better", "更符合输出契约")

    case = workbench.record_pairwise_cases(
        overall_conclusion="candidate_better",
        passed=True,
        summary_note="只纳入已运行案例",
    )

    assert len(case["verification"]["pairwise_runs"]) == 1
    assert case["verification"]["pairwise_runs"][0]["user_input"] == "输入1"
    assert adopted == [case["id"]]


def test_pairwise_workbench_requires_verdict_for_completed_case():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "修复后 Prompt"}}
    pairwise_case = workbench.add_pairwise_case(user_input="输入1")
    workbench.run_pairwise_case(pairwise_case["id"])

    with pytest.raises(ValueError, match="人工裁决"):
        workbench.record_pairwise_cases(
            overall_conclusion="candidate_better",
            passed=False,
        )


def test_pairwise_workbench_can_update_selected_case_inputs():
    workbench = RepairWorkbench(FakeVerifyService())
    pairwise_case = workbench.add_pairwise_case(user_input="旧输入")

    updated = workbench.update_pairwise_case(
        pairwise_case["id"],
        context_label="文档",
        context_text="背景",
        user_input="新输入",
    )

    assert updated["context_label"] == "文档"
    assert updated["context_text"] == "背景"
    assert updated["user_input"] == "新输入"
    assert workbench.get_pairwise_cases()[0]["user_input"] == "新输入"


def test_pairwise_workbench_rerun_clears_stale_verdict_and_note():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "修复后 Prompt"}}
    pairwise_case = workbench.add_pairwise_case(user_input="输入1")
    workbench.run_pairwise_case(pairwise_case["id"])
    workbench.set_pairwise_case_verdict(pairwise_case["id"], "candidate_better", "旧裁决")

    workbench.run_pairwise_case(pairwise_case["id"], user_input="更新后的输入")

    updated = workbench.get_pairwise_cases()[0]
    assert updated["verdict"] is None
    assert updated["note"] == ""
    assert updated["run"]["user_input"] == "更新后的输入"


def test_pairwise_workbench_assigns_unique_ids_after_deleting_a_case():
    workbench = RepairWorkbench(FakeVerifyService())
    first = workbench.add_pairwise_case()
    second = workbench.add_pairwise_case()
    workbench.remove_pairwise_case(first["id"])

    third = workbench.add_pairwise_case()

    assert len({case["id"] for case in workbench.get_pairwise_cases()}) == 2
    assert third["id"] != second["id"]


def test_pairwise_workbench_can_remove_and_select_cases_without_mutating_state():
    workbench = RepairWorkbench(FakeVerifyService())
    first = workbench.add_pairwise_case(user_input="输入1")
    second = workbench.add_pairwise_case(user_input="输入2")

    selected = workbench.select_pairwise_case(second["id"])
    assert selected["id"] == second["id"]
    assert workbench.active_pairwise_case_id == second["id"]

    removed = workbench.remove_pairwise_case(first["id"])
    assert removed["id"] == first["id"]
    assert [case["id"] for case in workbench.get_pairwise_cases()] == [second["id"]]
    assert workbench.select_pairwise_case("missing") is None
