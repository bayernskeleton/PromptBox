from promptbox_mvp.prompt_audit import analyze_prompt


def issue_by_code(audit, code):
    return next(issue for issue in audit["issues"] if issue["code"] == code)


def test_analyze_prompt_extracts_task_constraints_and_output_contract():
    audit = analyze_prompt(
        "请提取会议待办。仅依据原文，不得补充；输出 JSON，包含 owner 和 action。"
    )

    assert audit["task"] == "请提取会议待办。"
    assert audit["constraints"] == ["仅依据原文", "不得补充"]
    assert audit["output_contract"] == ["输出 JSON", "包含 owner 和 action"]
    assert audit["issues"] == []


def test_analyze_prompt_flags_no_op_and_chinese_binary_pollution_with_actionable_rewrites():
    audit = analyze_prompt(
        "确保高质量输出。真正的问题不是摘要，而是洞察。请总结会议内容。"
    )

    assert audit["task"] == "请总结会议内容。"
    assert audit["constraints"] == []
    assert audit["output_contract"] == []
    assert issue_by_code(audit, "no_op_quality") == {
        "code": "no_op_quality",
        "severity": "info",
        "evidence": "确保高质量输出",
        "reason": "命中无验收条件的质量要求。",
        "action": "删除空泛质量要求，改为可检查的约束或输出格式。",
    }
    assert issue_by_code(audit, "binary_pollution") == {
        "code": "binary_pollution",
        "severity": "info",
        "evidence": "真正的问题",
        "reason": "命中中文二元对立表达。",
        "action": "删除二元对立句，直接陈述需要完成的任务或约束。",
    }


def test_analyze_prompt_does_not_flag_precise_contract_language():
    audit = analyze_prompt("将输入改写为三条要点，每条不超过 20 字。")

    assert audit["issues"] == []
    assert audit["output_contract"] == ["三条要点", "每条不超过 20 字"]


def test_analyze_prompt_reports_complete_coverage_for_bounded_structured_prompt():
    audit = analyze_prompt(
        "请提取会议待办。仅依据原文，不得补充；输出 JSON，包含 owner 和 action。"
    )

    assert audit["coverage"] == {
        "has_task": True,
        "has_constraints": True,
        "has_output_contract": True,
        "has_scope_boundary": True,
        "has_task_object": True,
        "has_input_boundary": False,
    }
    assert audit["issues"] == []


def test_analyze_prompt_flags_missing_output_contract_for_expansive_task():
    audit = analyze_prompt("请总结这份会议记录。仅依据原文，不得补充。")

    assert issue_by_code(audit, "missing_output_contract") == {
        "code": "missing_output_contract",
        "severity": "warning",
        "evidence": "请总结这份会议记录。",
        "reason": "已识别任务，但未识别可验证的输出契约。",
        "action": "补充至少一项格式、字段、条数或长度要求。",
    }


def test_analyze_prompt_flags_missing_scope_boundary_for_expansive_task():
    audit = analyze_prompt("请分析这份会议记录；输出三条要点。")

    assert issue_by_code(audit, "missing_scope_boundary") == {
        "code": "missing_scope_boundary",
        "severity": "warning",
        "evidence": "请分析这份会议记录。",
        "reason": "任务可能扩展输入以外的信息，但未识别范围边界。",
        "action": "补充事实来源、禁止补充或未知时的处理方式。",
    }


def test_analyze_prompt_does_not_flag_scope_boundary_for_format_conversion():
    audit = analyze_prompt("将以下 CSV 转为 JSON。输出 JSON，包含 records 字段。")

    assert audit["coverage"]["has_scope_boundary"] is False
    assert "missing_scope_boundary" not in {issue["code"] for issue in audit["issues"]}


def test_analyze_prompt_flags_only_provable_constraint_conflicts():
    audit = analyze_prompt(
        "请总结会议记录。仅依据原文；补充背景知识；输出三条要点。"
    )

    assert issue_by_code(audit, "constraint_conflict") == {
        "code": "constraint_conflict",
        "severity": "error",
        "evidence": "仅依据原文 / 补充背景知识",
        "reason": "两条约束要求的信息来源互斥。",
        "action": "删除其中一条，或写明两者的优先级。",
    }


def test_analyze_prompt_does_not_treat_compatible_constraints_as_conflict():
    audit = analyze_prompt(
        "请提取会议待办。仅依据原文；每条不超过 50 字；输出 JSON。"
    )

    assert "constraint_conflict" not in {issue["code"] for issue in audit["issues"]}


def test_analyze_prompt_sorts_issues_by_severity_then_registration_order():
    audit = analyze_prompt(
        "确保高质量输出。真正的问题不是摘要，而是洞察。"
        "请总结会议。仅依据原文；补充背景知识。"
    )

    assert [issue["code"] for issue in audit["issues"]] == [
        "constraint_conflict",
        "missing_output_contract",
        "no_op_quality",
        "binary_pollution",
    ]


def test_scope_boundaries_cover_prohibit_original_only_and_unknown_handling():
    for boundary in ("不得补充", "禁止补充", "只依据原文", "未知则说明"):
        audit = analyze_prompt(f"请分析会议记录；{boundary}；输出三条要点。")

        assert audit["coverage"]["has_scope_boundary"] is True
        assert "missing_scope_boundary" not in {issue["code"] for issue in audit["issues"]}


def test_expansive_tasks_require_scope_boundary_for_extract_rewrite_and_generate():
    for task in ("请提取会议待办", "请改写会议记录", "请生成会议摘要"):
        audit = analyze_prompt(f"{task}；输出三条要点。")

        assert issue_by_code(audit, "missing_scope_boundary")["evidence"] == f"{task}。"


def test_analyze_prompt_extracts_embedded_constraint():
    audit = analyze_prompt("请总结会议记录；内容不得补充；输出三条要点。")

    assert audit["constraints"] == ["内容不得补充"]


def test_analyze_prompt_recognizes_output_format_json_contract():
    audit = analyze_prompt("请总结会议记录；输出格式：JSON。")

    assert audit["output_contract"] == ["输出格式：JSON"]
    assert "missing_output_contract" not in {issue["code"] for issue in audit["issues"]}


def test_analyze_prompt_requires_binary_pairing():
    for prompt in ("请总结会议记录，不是最终结论；输出三条要点。", "请总结会议记录，而是提取待办；输出三条要点。"):
        audit = analyze_prompt(prompt)

        assert "binary_pollution" not in {issue["code"] for issue in audit["issues"]}

    paired = analyze_prompt("真正的问题不是摘要，而是洞察。请总结会议记录；输出三条要点。")

    assert issue_by_code(paired, "binary_pollution")["evidence"] == "真正的问题"


def test_analyze_prompt_flags_missing_task_object_only_for_supported_empty_task_verbs():
    for prompt in ("请分析。", "请总结一下。", "改写。"):
        audit = analyze_prompt(prompt)
        issue = issue_by_code(audit, "missing_task_object")

        assert issue == {
            "code": "missing_task_object",
            "severity": "warning",
            "evidence": audit["task"],
            "reason": "已识别任务动作，但未识别可处理的对象。",
            "action": "补充需要处理的对象，例如数据、文本、记录或具体主题。",
        }
        assert audit["coverage"]["has_task_object"] is False


def test_analyze_prompt_does_not_flag_task_object_when_supported_verb_has_direct_object():
    for prompt in ("请分析以下数据。", "生成招聘 JD。", "生成一段自我介绍，150 字以内。"):
        audit = analyze_prompt(prompt)

        assert audit["coverage"]["has_task_object"] is True
        assert "missing_task_object" not in {issue["code"] for issue in audit["issues"]}


def test_analyze_prompt_flags_missing_input_boundary_only_for_explicit_material_task():
    audit = analyze_prompt("请总结以下内容。输出三条要点。")

    assert issue_by_code(audit, "missing_input_boundary") == {
        "code": "missing_input_boundary",
        "severity": "warning",
        "evidence": "请总结以下内容。",
        "reason": "任务明确引用材料，但未识别材料范围或引用方式。",
        "action": "写明材料来源或处理边界，例如“仅依据以下文本”或“基于提供的 CSV”。",
    }
    assert audit["coverage"]["has_task_object"] is True
    assert audit["coverage"]["has_input_boundary"] is False


def test_analyze_prompt_recognizes_explicit_material_boundary_without_inferring_material_existence():
    for prompt in (
        "仅依据以下文本总结内容；输出三条要点。",
        "基于提供的 CSV 统计字段缺失率；输出 JSON。",
        "只处理输入 JSON，提取 records 字段；输出 JSON。",
        "根据给定的会议记录生成摘要；输出三条要点。",
    ):
        audit = analyze_prompt(prompt)

        assert audit["coverage"]["has_input_boundary"] is True
        assert "missing_input_boundary" not in {issue["code"] for issue in audit["issues"]}


def test_analyze_prompt_does_not_treat_output_contract_as_input_boundary():
    audit = analyze_prompt("总结以下内容并输出三条要点。")

    assert audit["coverage"]["has_input_boundary"] is False
    assert "missing_input_boundary" in {issue["code"] for issue in audit["issues"]}


def test_analyze_prompt_does_not_flag_input_boundary_without_explicit_material_reference():
    for prompt in (
        "生成一段自我介绍，150 字以内。",
        "将输入改写为三条要点，每条不超过 20 字。",
    ):
        audit = analyze_prompt(prompt)

        assert audit["coverage"]["has_input_boundary"] is False
        assert "missing_input_boundary" not in {issue["code"] for issue in audit["issues"]}

    assert analyze_prompt("将输入改写为三条要点，每条不超过 20 字。")["issues"] == []


def test_analyze_prompt_keeps_new_warning_rules_in_registration_order():
    audit = analyze_prompt("请分析。确保高质量输出。")

    assert [issue["code"] for issue in audit["issues"]] == [
        "missing_output_contract",
        "missing_scope_boundary",
        "missing_task_object",
        "no_op_quality",
    ]


def test_analyze_prompt_flags_only_registered_vague_constraint_terms():
    for term in ("适当", "必要", "酌情", "尽量"):
        audit = analyze_prompt(f"请总结会议记录；{term}简洁；输出三条要点。")
        issue = issue_by_code(audit, "vague_constraint")

        assert issue == {
            "code": "vague_constraint",
            "severity": "warning",
            "evidence": f"{term}简洁",
            "reason": "约束使用了不可直接验证的程度词。",
            "action": "改为可检查条件，例如最大字数、条数、字段或明确禁止项。",
        }

    audit = analyze_prompt("请总结会议记录；不得补充；每条不超过 50 字。")
    assert "vague_constraint" not in {issue["code"] for issue in audit["issues"]}


def test_analyze_prompt_flags_only_direct_literal_constraint_conflicts():
    for opposite in ("不得保留原文", "禁止保留原文"):
        audit = analyze_prompt(
            f"请总结会议记录；必须保留原文；{opposite}；输出三条要点。"
        )
        issue = issue_by_code(audit, "direct_constraint_conflict")

        assert issue == {
            "code": "direct_constraint_conflict",
            "severity": "error",
            "evidence": f"必须保留原文 / {opposite}",
            "reason": "两条约束对同一对象提出了直接相反的要求。",
            "action": "删除其中一条，或明确冲突时的优先级。",
        }

    audit = analyze_prompt("请总结会议记录；必须输出 JSON；不得输出 XML。")
    assert "direct_constraint_conflict" not in {
        issue["code"] for issue in audit["issues"]
    }


def test_analyze_prompt_flags_multiple_must_output_formats_without_priority_only():
    audit = analyze_prompt("请总结会议记录；必须输出 JSON；必须输出 YAML。")

    assert issue_by_code(audit, "missing_constraint_priority") == {
        "code": "missing_constraint_priority",
        "severity": "warning",
        "evidence": "必须输出 JSON / 必须输出 YAML",
        "reason": "同一对象存在多条必须限制，但未识别到优先级。",
        "action": "删除重复限制，或补充“若冲突，以…为准”等优先级规则。",
    }

    for prompt in (
        "请总结会议记录；必须输出 JSON；必须包含 owner。",
        "请总结会议记录；必须输出 JSON；若冲突，以 JSON 为准；必须输出 YAML。",
        "请总结会议记录；必须输出 JSON；不得输出 YAML。",
    ):
        audit = analyze_prompt(prompt)
        assert "missing_constraint_priority" not in {
            issue["code"] for issue in audit["issues"]
        }


def test_analyze_prompt_sorts_constraint_executability_issues_stably():
    audit = analyze_prompt(
        "确保高质量输出；请总结会议记录；尽量简洁；"
        "必须保留原文；不得保留原文；必须输出 JSON；必须输出 YAML。"
    )

    assert [issue["code"] for issue in audit["issues"]] == [
        "direct_constraint_conflict",
        "missing_scope_boundary",
        "vague_constraint",
        "missing_constraint_priority",
        "no_op_quality",
    ]
