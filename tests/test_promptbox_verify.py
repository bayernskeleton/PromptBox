"""P0 验证链路测试：用候选提示词跑真实 payload，返回模型原文供人工判断。

与 repair() 的边界：verify() 不做 JSON 契约校验，原文返回。
本文件不得修改 repair() 相关测试，用于证明新增能力未侵入原契约。
"""

import pytest

from promptbox_mvp.ai_service import RepairService, compose_user_payload


def echo_transport(messages):
    return "模型输出内容"


def capture_transport(store):
    def transport(messages):
        store["messages"] = messages
        return "模型输出内容"

    return transport


# ── compose_user_payload：纯函数层 ──────────────────────────────


def test_payload_without_context_sends_only_user_input():
    """上下文为空时不得留空的标记外壳，空标记会污染模型注意力。"""
    payload = compose_user_payload("", "请总结这段话")

    assert payload == "请总结这段话"
    assert "业务参考上下文" not in payload
    assert "本次任务输入" not in payload


def test_payload_with_context_uses_both_sections():
    payload = compose_user_payload("项目背景资料", "请提取风险项")

    assert "【业务参考上下文】\n项目背景资料" in payload
    assert "【本次任务输入】\n请提取风险项" in payload
    assert payload.index("业务参考上下文") < payload.index("本次任务输入")


def test_payload_with_context_label_marks_source():
    payload = compose_user_payload("资料正文", "任务", context_label="交付文档.md")

    assert "【业务参考上下文（交付文档.md）】" in payload


def test_payload_treats_whitespace_only_context_as_empty():
    payload = compose_user_payload("   \n\t  ", "任务输入")

    assert payload == "任务输入"


def test_payload_rejects_empty_user_input_with_context():
    """只发上下文不发任务输入是无意义调用。"""
    with pytest.raises(ValueError, match="user_input must not be empty"):
        compose_user_payload("一大段上下文", "   ")


def test_payload_rejects_empty_user_input_without_context():
    with pytest.raises(ValueError, match="user_input must not be empty"):
        compose_user_payload("", "")


# ── verify()：服务层 ───────────────────────────────────────────


def test_verify_puts_candidate_prompt_in_system_role():
    store = {}
    RepairService(capture_transport(store)).verify("候选提示词", "上下文", "任务输入")

    messages = store["messages"]
    assert messages[0] == {"role": "system", "content": "候选提示词"}
    assert messages[1]["role"] == "user"
    assert len(messages) == 2


def test_verify_returns_model_output_verbatim_without_json_contract():
    """verify 不解析 JSON、不校验契约，原文返回。"""
    service = RepairService(lambda _messages: "这不是 JSON，只是一段普通输出")

    result = service.verify("候选", "", "输入")

    assert result["output"] == "这不是 JSON，只是一段普通输出"


def test_verify_reports_payload_and_context_char_counts():
    """用户必须知道自己实际发出去了多少字符。"""
    candidate = "A" * 10
    context = "B" * 100
    user_input = "C" * 5

    result = RepairService(echo_transport).verify(candidate, context, user_input)

    expected_user = compose_user_payload(context, user_input)
    assert result["context_chars"] == 100
    assert result["payload_chars"] == len(candidate) + len(expected_user)


def test_verify_context_chars_is_zero_when_no_context():
    result = RepairService(echo_transport).verify("候选", "", "输入")

    assert result["context_chars"] == 0
    assert result["truncated"] is False


def test_verify_reports_elapsed_ms():
    result = RepairService(echo_transport).verify("候选", "", "输入")

    assert isinstance(result["elapsed_ms"], int)
    assert result["elapsed_ms"] >= 0


def test_verify_rejects_empty_candidate_prompt():
    with pytest.raises(ValueError, match="candidate_prompt must not be empty"):
        RepairService(echo_transport).verify("   ", "上下文", "输入")


def test_verify_rejects_empty_user_input():
    with pytest.raises(ValueError, match="user_input must not be empty"):
        RepairService(echo_transport).verify("候选", "上下文", "")


# ── 截断保护 ──────────────────────────────────────────────────


def test_verify_truncates_context_when_max_given_and_flags_it():
    """截断必须显式声明，静默截断等于测试结果作废却不告知。"""
    store = {}
    context = "X" * 500

    result = RepairService(capture_transport(store)).verify(
        "候选", context, "输入", max_context_chars=100
    )

    assert result["truncated"] is True
    assert result["context_chars"] == 100
    user_content = store["messages"][1]["content"]
    assert "X" * 100 in user_content
    assert "X" * 101 not in user_content


def test_verify_truncation_keeps_head_of_context():
    store = {}
    context = "开头保留" + "填充" * 100 + "尾部丢弃"

    RepairService(capture_transport(store)).verify(
        "候选", context, "输入", max_context_chars=20
    )

    user_content = store["messages"][1]["content"]
    assert "开头保留" in user_content
    assert "尾部丢弃" not in user_content


def test_verify_does_not_truncate_when_context_within_limit():
    result = RepairService(echo_transport).verify(
        "候选", "短上下文", "输入", max_context_chars=1000
    )

    assert result["truncated"] is False
    assert result["context_chars"] == len("短上下文")


def test_verify_rejects_non_positive_max_context_chars():
    with pytest.raises(ValueError, match="max_context_chars must be positive"):
        RepairService(echo_transport).verify("候选", "上下文", "输入", max_context_chars=0)


# ── 传输失败处理 ───────────────────────────────────────────────


def test_verify_propagates_value_error_from_transport():
    def failing(_messages):
        raise ValueError("未配置修复服务。")

    with pytest.raises(ValueError, match="未配置修复服务。"):
        RepairService(failing).verify("候选", "", "输入")


def test_verify_wraps_unexpected_transport_failure():
    def failing(_messages):
        raise RuntimeError("socket closed")

    with pytest.raises(ValueError, match="transport failed"):
        RepairService(failing).verify("候选", "", "输入")


def test_verify_rejects_non_string_transport_result():
    with pytest.raises(ValueError, match="verify result must be a string"):
        RepairService(lambda _messages: {"unexpected": "dict"}).verify("候选", "", "输入")
