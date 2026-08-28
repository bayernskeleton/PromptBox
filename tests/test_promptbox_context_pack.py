from promptbox_mvp.context_manifest import ManifestEntry
from promptbox_mvp.context_pack import ContextBudget, assemble_context_pack, estimate_tokens
from promptbox_mvp.context_parsers import ParserResult


def entry(path, suffix, text, *, category=None):
    return (
        ManifestEntry(
            absolute_path=path,
            relative_path=path,
            suffix=suffix,
            size_bytes=len(text.encode("utf-8")),
            status="included",
        ),
        ParserResult(status="success", text=text, chars=len(text)),
    )


def test_assembly_orders_readme_config_code_data_office_and_marks_boundaries():
    pack = assemble_context_pack([
        entry("src/main.py", ".py", "print(1)"),
        entry("README.md", ".md", "说明"),
        entry("config.json", ".json", "{}"),
    ])

    assert pack.text.index("README.md") < pack.text.index("config.json") < pack.text.index("src/main.py")
    assert "[正文开始]" in pack.text
    assert "[正文结束]" in pack.text
    assert pack.file_count == 3


def test_failed_entries_are_reported_but_never_rendered_as正文():
    failed = ManifestEntry("bad.doc", "bad.doc", ".doc", 10, "failed", "unsupported_format", "请另存为 .docx")
    pack = assemble_context_pack([entry("README.md", ".md", "说明")], failed_entries=[failed])

    assert pack.file_count == 1
    assert "无法读取" not in pack.text
    assert pack.failed_count == 1
    assert pack.failure_summary[0]["reason_code"] == "unsupported_format"


def test_all_entries_failed_cannot_create_pack():
    failed = ManifestEntry("bad.doc", "bad.doc", ".doc", 10, "failed", "unsupported_format", "请另存为 .docx")
    try:
        assemble_context_pack([], failed_entries=[failed])
    except ValueError as exc:
        assert "没有文件成功解析" in str(exc)
    else:
        raise AssertionError("expected all-failed assembly to fail")


def test_budget_unknown_capacity_warns_without_blocking():
    budget = ContextBudget.from_pack("甲" * 100, model_context_window=None)

    assert budget.capacity_status == "unknown"
    assert budget.can_send is True
    assert budget.estimated_tokens == estimate_tokens(100)


def test_budget_known_capacity_reserves_prompt_output_and_safety_margin():
    budget = ContextBudget.from_pack(
        "甲" * 200,
        model_context_window=200,
        system_prompt_tokens=20,
        current_prompt_tokens=20,
        reserved_output_tokens=50,
        safety_margin_tokens=10,
    )

    assert budget.available_input_tokens == 100
    assert budget.capacity_status == "known"
    assert budget.can_send is False


def test_explicit_file_truncation_keeps_header_and_records_action():
    pack = assemble_context_pack(
        [entry("README.md", ".md", "标题\n" + "正文" * 100)],
        file_char_limit=20,
    )

    assert pack.truncated is True
    assert "README.md" in pack.text
    assert pack.actions[0]["action"] == "truncate"
    assert pack.actions[0]["source_chars"] > pack.actions[0]["sent_chars"]
