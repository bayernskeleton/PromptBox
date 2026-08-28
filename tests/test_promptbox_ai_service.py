import json

import pytest

import promptbox_mvp.ai_service as ai_service
from promptbox_mvp.ai_service import RepairService
from promptbox_mvp.prompt_audit import analyze_prompt


VALID_RESULT = {
    "diagnosis": "输出漏掉了格式约束",
    "mode": "B",
    "candidate": "请按指定格式输出内容",
    "reasons": ["补回输出契约", "保留任务目标"],
}


def prompt_from_messages(messages):
    user_content = next(message["content"] for message in messages if message["role"] == "user")
    return user_content.split("原始提示词:\n", 1)[1].split("\n\n失败输出:", 1)[0]


def valid_transport(messages):
    return dict(VALID_RESULT)


def test_repair_accepts_json_string_and_returns_standard_result():
    service = RepairService(lambda messages: json.dumps(valid_transport(messages), ensure_ascii=False))

    result = service.repair("原始提示词", "失败输出", "对照输入", "任务目标")

    assert {key: result[key] for key in VALID_RESULT} == VALID_RESULT
    assert result["audit"] == {
        "task": "原始提示词。",
        "constraints": [],
        "output_contract": [],
        "coverage": {
            "has_task": True,
            "has_constraints": False,
            "has_output_contract": False,
            "has_scope_boundary": False,
            "has_task_object": False,
            "has_input_boundary": False,
        },
        "issues": [
            {
                "code": "missing_output_contract",
                "severity": "warning",
                "evidence": "原始提示词。",
                "reason": "已识别任务，但未识别可验证的输出契约。",
                "action": "补充至少一项格式、字段、条数或长度要求。",
            }
        ],
    }
    assert result["audit_status"] == {"status": "available", "message": ""}
    assert result["quick_check"]["required_changes"] == ["补充至少一项可验证的输出格式、字段、条数或长度要求。"]


def test_repair_accepts_dict_result():
    service = RepairService(valid_transport)

    result = service.repair("p", "o", "i")
    assert {key: result[key] for key in VALID_RESULT} == VALID_RESULT
    assert result["audit"]["task"] == "p。"


def test_repair_accepts_prompt_only_without_optional_failure_material():
    captured = {}
    service = RepairService(
        lambda messages: (
            captured.setdefault("messages", messages),
            valid_transport(messages),
        )[1]
    )

    result = service.repair("仅一条已有提示词")

    assert {key: result[key] for key in VALID_RESULT} == VALID_RESULT
    user_content = next(
        message["content"] for message in captured["messages"] if message["role"] == "user"
    )
    assert "原始提示词:\n仅一条已有提示词" in user_content
    assert "失败输出" not in user_content
    assert "对照输入" not in user_content
    assert "任务目标" not in user_content
    assert "业务上下文" not in user_content


def test_repair_includes_only_provided_optional_sections():
    captured = {}
    service = RepairService(
        lambda messages: (
            captured.setdefault("messages", messages),
            valid_transport(messages),
        )[1]
    )

    service.repair(
        "提示词",
        output="输出失败",
        task_goal="提炼要点",
        context="用于小红书，面向 25–35 岁女性",
    )

    user_content = next(
        message["content"] for message in captured["messages"] if message["role"] == "user"
    )
    assert "失败输出:\n输出失败" in user_content
    assert "任务目标:\n提炼要点" in user_content
    assert "业务上下文:\n用于小红书，面向 25–35 岁女性" in user_content
    assert "对照输入" not in user_content


def test_messages_include_system_and_all_four_input_values():
    captured = {}

    def transport(messages):
        captured["messages"] = messages
        return valid_transport(messages)

    service = RepairService(transport)
    service.repair("原始 prompt", "失败 output", "对照 input", "任务 goal")

    messages = captured["messages"]
    assert isinstance(messages, list)
    assert any(message["role"] == "system" for message in messages)
    user_messages = [message["content"] for message in messages if message["role"] == "user"]
    assert len(user_messages) == 1
    user_content = user_messages[0]
    for value in ("原始 prompt", "失败 output", "对照 input", "任务 goal"):
        assert value in user_content
    system_content = next(message["content"] for message in messages if message["role"] == "system")
    assert "JSON" in system_content
    assert "diagnosis" in system_content
    assert "mode" in system_content
    assert "candidate" in system_content
    assert "reasons" in system_content


@pytest.mark.parametrize(
    "result",
    [
        {"candidate": "c", "reasons": ["r"]},
        {"diagnosis": "d", "reasons": ["r"]},
        {"diagnosis": "d", "candidate": "c"},
        {"diagnosis": "d", "candidate": "c", "reasons": ["r"]},
    ],
)
def test_repair_rejects_missing_fields(result):
    with pytest.raises(ValueError):
        RepairService(lambda messages: result).repair("p", "o", "i")


def test_repair_rejects_invalid_json():
    with pytest.raises(ValueError):
        RepairService(lambda messages: "not json").repair("p", "o", "i")


@pytest.mark.parametrize("reasons", ["reason", [], [""], ["ok", 1]])
def test_repair_rejects_non_nonempty_string_reasons(reasons):
    result = {"diagnosis": "d", "mode": "B", "candidate": "c", "reasons": reasons}
    with pytest.raises(ValueError):
        RepairService(lambda messages: result).repair("p", "o", "i")


@pytest.mark.parametrize("candidate", ["", "   ", 1, None])
def test_repair_rejects_invalid_candidate(candidate):
    result = {"diagnosis": "d", "mode": "B", "candidate": candidate, "reasons": ["r"]}
    with pytest.raises(ValueError):
        RepairService(lambda messages: result).repair("p", "o", "i")


@pytest.mark.parametrize("mode", [None, "", "C", "AB", 1, ["B"]])
def test_repair_rejects_invalid_mode(mode):
    result = {"diagnosis": "d", "mode": mode, "candidate": "c", "reasons": ["r"]}
    with pytest.raises(ValueError, match='mode must be either "B" or "A"'):
        RepairService(lambda messages: result).repair("p", "o", "i")


def test_repair_accepts_mode_a():
    result = {**VALID_RESULT, "mode": "A"}
    repaired = RepairService(lambda _messages: result).repair("p", "o", "i")
    assert repaired["mode"] == "A"


def test_repair_wraps_transport_exception_and_preserves_value_error():
    with pytest.raises(ValueError, match="transport failed"):
        RepairService(lambda messages: (_ for _ in ()).throw(RuntimeError("transport failed"))).repair(
            "p", "o", "i"
        )

    expected = ValueError("already a value error")

    def raises_value_error(messages):
        raise expected

    with pytest.raises(ValueError) as caught:
        RepairService(raises_value_error).repair("p", "o", "i")
    assert caught.value is expected


def test_repair_preserves_unicode_in_messages():
    captured = []
    service = RepairService(lambda messages: captured.append(messages) or valid_transport(messages))

    service.repair("提示词演化 🚀", "失败输出：格式错", "输入示例：你好", "目标：保留中文")

    user_content = next(message["content"] for message in captured[0] if message["role"] == "user")
    assert "提示词演化 🚀" in user_content
    assert "失败输出：格式错" in user_content
    assert "输入示例：你好" in user_content
    assert "目标：保留中文" in user_content


def test_repair_returns_local_prompt_only_audit_with_standard_result():
    service = RepairService(valid_transport)

    result = service.repair(
        "请提取待办。仅依据原文；输出 JSON，包含 owner。",
        "失败输出",
        "测试输入",
    )

    assert result["audit"] == {
        "task": "请提取待办。",
        "constraints": ["仅依据原文"],
        "output_contract": ["输出 JSON", "包含 owner"],
        "coverage": {
            "has_task": True,
            "has_constraints": True,
            "has_output_contract": True,
            "has_scope_boundary": True,
            "has_task_object": True,
            "has_input_boundary": False,
        },
        "issues": [],
    }
    assert result["audit_status"] == {"status": "available", "message": ""}
    assert result["quick_check"]["required_changes"] == []


def test_repair_audit_flags_prompt_quality_no_op_without_rejecting_candidate():
    service = RepairService(valid_transport)

    result = service.repair("确保高质量输出。请总结会议。", "失败输出", "测试输入")

    assert result["candidate"] == VALID_RESULT["candidate"]
    assert next(issue for issue in result["audit"]["issues"] if issue["code"] == "no_op_quality") == {
        "code": "no_op_quality",
        "severity": "info",
        "evidence": "确保高质量输出",
        "reason": "命中无验收条件的质量要求。",
        "action": "删除空泛质量要求，改为可检查的约束或输出格式。",
    }
    assert result["audit_status"] == {"status": "available", "message": ""}
    with pytest.raises(ValueError):
        RepairService(lambda messages: []).repair("p", "o", "i")


def test_repair_keeps_valid_candidate_when_local_audit_is_unavailable(monkeypatch):
    def fail_audit(_prompt):
        raise RuntimeError("audit registry unavailable")

    monkeypatch.setattr(ai_service, "analyze_prompt", fail_audit)
    result = RepairService(lambda _messages: dict(VALID_RESULT)).repair(
        "请总结会议记录。", "失败输出", "测试输入"
    )

    assert {key: result[key] for key in VALID_RESULT} == VALID_RESULT
    assert result["audit"] is None
    assert result["audit_status"] == {
        "status": "unavailable",
        "message": "audit registry unavailable",
    }
    assert result["quick_check"] is None


def test_repair_rejects_non_string_diagnosis():
    result = {"diagnosis": [], "mode": "B", "candidate": "c", "reasons": ["r"]}
    with pytest.raises(ValueError):
        RepairService(lambda messages: result).repair("p", "o", "i")


def test_repair_prompt_requires_mode_and_four_predictability_laws():
    system_prompt = ai_service._SYSTEM_PROMPT

    assert '仅 "B" 或 "A"' in system_prompt
    assert "模式 B（意图保真微调）" in system_prompt
    assert "模式 A（结构规范重构）" in system_prompt
    assert "消灭 No-op" in system_prompt
    assert "防幻觉边界" in system_prompt
    assert "化解隐性冲突" in system_prompt
    assert "语言纯化" in system_prompt
    assert "本地速检" in system_prompt
    assert "可能漏检或误判" in system_prompt


def test_repair_injects_local_quick_check_as_advisory_only():
    captured = {}

    def transport(messages):
        captured["messages"] = messages
        return dict(VALID_RESULT)

    result = RepairService(transport).repair(
        "请总结会议记录。仅依据原文；尽量简洁；输出三条要点。", "失败", "输入"
    )

    assert result["quick_check"]["required_changes"] == [
        "将本次证据中的空泛约束词改为字数、条数、字段或明确禁止项。"
    ]
    user_content = next(
        message["content"] for message in captured["messages"] if message["role"] == "user"
    )
    assert "本地速检（仅作参考线索，可能漏检或误判，勿被其限制）:" in user_content
    assert "本地速检建议（同上，仅参考）:" in user_content
    assert "vague_constraint" in user_content
    assert "尽量简洁" in user_content


def test_repair_passes_user_selected_mode_into_messages():
    captured = {}

    def transport(messages):
        captured["messages"] = messages
        return dict(VALID_RESULT)

    RepairService(transport).repair("p", "o", "i", mode="A")

    user_content = next(
        message["content"] for message in captured["messages"] if message["role"] == "user"
    )
    assert "用户指定：结构规范重构（A 档）" in user_content


def test_repair_defaults_to_autonomous_mode_selection():
    captured = {}

    def transport(messages):
        captured["messages"] = messages
        return dict(VALID_RESULT)

    RepairService(transport).repair("p", "o", "i")

    user_content = next(
        message["content"] for message in captured["messages"] if message["role"] == "user"
    )
    assert "未指定，请按诊断结果自主选择档位" in user_content


def test_repair_rejects_invalid_request_mode():
    with pytest.raises(ValueError, match='mode must be "", "B" or "A"'):
        RepairService(valid_transport).repair("p", "o", "i", mode="C")


def test_repair_passes_through_model_issue_declarations_without_validation():
    result = {
        **VALID_RESULT,
        "resolved_issue_codes": ["vague_constraint"],
        "unresolved_issue_codes": ["missing_output_contract"],
    }

    repaired = RepairService(lambda _messages: result).repair("请总结会议记录；尽量简洁。", "输出", "输入")



def test_ai_service_exposes_one_canonical_payload_composer_and_verify_method():
    source = ai_service.__file__
    source_text = open(source, encoding="utf-8").read()

    assert source_text.count("def compose_user_payload(") == 1
    assert source_text.count("    def verify(") == 1
