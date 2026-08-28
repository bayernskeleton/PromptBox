import pytest

from promptbox_mvp.prompt_variables import PromptTemplate
from promptbox_mvp.snapshots import create_snapshot, get_snapshots
from promptbox_mvp.workbench import RepairWorkbench


class FakeVerifyService:
    def __init__(self):
        self.calls = []

    def verify(
        self,
        candidate_prompt,
        context_text,
        user_input,
        context_label="",
        max_context_chars=None,
    ):
        self.calls.append(
            {
                "prompt": candidate_prompt,
                "context": context_text,
                "input": user_input,
                "label": context_label,
                "max_context_chars": max_context_chars,
            }
        )
        return {
            "output": "输出",
            "payload_chars": len(candidate_prompt),
            "context_chars": len(context_text),
            "truncated": False,
            "elapsed_ms": 1,
        }


def test_prompt_template_exposes_variable_description_and_example():
    template = PromptTemplate.from_text(
        "请围绕 {主题} 写给 {读者}。",
        definitions={
            "主题": {"description": "文章要处理的主题", "example": "AI 工具"},
            "读者": {"description": "文章面对的人", "example": "大学生"},
        },
    )

    assert template.variable_names == ["主题", "读者"]
    assert template.variables == [
        {"name": "主题", "description": "文章要处理的主题", "example": "AI 工具"},
        {"name": "读者", "description": "文章面对的人", "example": "大学生"},
    ]


def test_prompt_template_requires_every_variable_and_renders_values():
    template = PromptTemplate.from_text("围绕 {主题} 写给 {读者}。")

    with pytest.raises(ValueError, match="变量未填写：读者"):
        template.render({"主题": "AI 工具"})

    assert template.render({"主题": "AI 工具", "读者": "大学生"}) == (
        "围绕 AI 工具 写给 大学生。"
    )


def test_prompt_template_rejects_empty_variable_value():
    template = PromptTemplate.from_text("围绕 {主题} 写作。")

    with pytest.raises(ValueError, match="变量未填写：主题"):
        template.render({"主题": "   "})


def test_pairwise_verification_uses_one_variable_filled_result_for_both_prompts():
    service = FakeVerifyService()
    workbench = RepairWorkbench(service)
    workbench.analysis = {"candidate": {"content": "请改写 {主题}，并补充三条建议。"}}

    result = workbench.run_pairwise_verify(
        baseline_prompt="请总结 {主题}。",
        candidate_prompt="请改写 {主题}，并补充三条建议。",
        context_text="背景",
        user_input="执行",
        variables={"主题": "AI 工具"},
    )

    assert [call["prompt"] for call in service.calls] == [
        "请总结 AI 工具。",
        "请改写 AI 工具，并补充三条建议。",
    ]
    assert result["baseline"]["output"] == "输出"
    assert result["candidate"]["output"] == "输出"


def test_pairwise_verification_returns_saved_snapshot_id():
    service = FakeVerifyService()
    saved = []
    workbench = RepairWorkbench(service, save_snapshot=lambda payload: saved.append(payload) or "snapshot_1")
    workbench.analysis = {"candidate": {"content": "请改写 {主题}。"}}

    result = workbench.run_pairwise_verify(
        baseline_prompt="请总结 {主题}。",
        candidate_prompt="请改写 {主题}。",
        context_text="背景",
        user_input="执行",
        variables={"主题": "AI 工具"},
    )

    assert result["snapshot_id"] == "snapshot_1"
    assert saved[0]["variables"] == {"主题": "AI 工具"}


def test_single_candidate_verification_uses_filled_variable_result():
    service = FakeVerifyService()
    workbench = RepairWorkbench(service)
    workbench.analysis = {"candidate": {"content": "候选 {主题}"}}

    workbench.run_verify(
        candidate_prompt="候选 {主题}",
        context_text="背景",
        user_input="执行",
        variables={"主题": "AI 工具"},
    )

    assert service.calls[0]["prompt"] == "候选 AI 工具"


def test_pairwise_case_passes_the_same_variables_to_both_versions():
    service = FakeVerifyService()
    workbench = RepairWorkbench(service)
    workbench.capture_case("snip_1", "ver_1", 1, "请总结 {主题}。", "失败", "输入")
    workbench.analysis = {"candidate": {"content": "请改写 {主题}。"}}
    case = workbench.add_pairwise_case(user_input="执行")

    workbench.run_pairwise_case(case["id"], variables={"主题": "AI 工具"})

    assert [call["prompt"] for call in service.calls] == [
        "请总结 AI 工具。",
        "请改写 AI 工具。",
    ]


def test_snapshot_keeps_filled_values_and_rendered_prompt():
    snapshot = create_snapshot(
        snippet_id="snip_1",
        version_id="ver_1",
        trigger="填充并复制",
        template="请围绕 {主题} 写给 {读者}。",
        variable_definitions={
            "主题": {"description": "文章主题", "example": "AI 工具"},
            "读者": {"description": "目标读者", "example": "大学生"},
        },
        variables={"主题": "Prompt 工程", "读者": "产品经理"},
    )

    assert snapshot["variables"] == {"主题": "Prompt 工程", "读者": "产品经理"}
    assert snapshot["template"] == "请围绕 {主题} 写给 {读者}。"
    assert snapshot["rendered_prompt"] == "请围绕 Prompt 工程 写给 产品经理。"
    assert snapshot["trigger"] == "填充并复制"


def test_snapshot_list_uses_append_order_when_timestamps_tie():
    from copy import deepcopy

    first = create_snapshot(
        snippet_id="snip_1",
        version_id="ver_1",
        trigger="填充并复制",
        template="{主题}",
        variable_definitions={},
        variables={"主题": "第一次"},
    )
    second = create_snapshot(
        snippet_id="snip_1",
        version_id="ver_1",
        trigger="填充并粘贴",
        template="{主题}",
        variable_definitions={},
        variables={"主题": "第二次"},
    )
    second["created_at"] = first["created_at"]
    snippet = {"id": "snip_1", "snapshots": [first, second]}

    assert [item["variables"]["主题"] for item in get_snapshots(snippet)] == ["第二次", "第一次"]


def test_snapshot_list_is_scoped_to_current_prompt_and_new_snapshot_is_not_overwritten():
    data = {
        "snippets": [
            {"id": "snip_1", "snapshots": []},
            {"id": "snip_2", "snapshots": []},
        ]
    }
    first = create_snapshot(
        snippet_id="snip_1",
        version_id="ver_1",
        trigger="填充并复制",
        template="{主题}",
        variable_definitions={},
        variables={"主题": "第一次"},
    )
    second = create_snapshot(
        snippet_id="snip_1",
        version_id="ver_1",
        trigger="填充并粘贴",
        template="{主题}",
        variable_definitions={},
        variables={"主题": "第二次"},
    )
    data["snippets"][0]["snapshots"].extend([first, second])

    assert [item["variables"]["主题"] for item in get_snapshots(data["snippets"][0])] == [
        "第二次",
        "第一次",
    ]
    assert get_snapshots(data["snippets"][1]) == []
