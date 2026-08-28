"""P2 Markdown 点选工具栏纯函数层测试（方案 C）。

每个操作覆盖三态：无选中 / 单行选中 / 多行选中。
"""

import pytest

from promptbox_mvp.md_toolbar import (
    EditResult,
    insert_fence,
    insert_placeholder,
    insert_rule,
    insert_table,
    toggle_blockquote,
    toggle_heading,
    toggle_list,
    insert_task,
    wrap_selection,
)


# ── 标题（幂等切换）───────────────────────────────────────────


def test_heading_adds_marker_at_cursor_line():
    result = toggle_heading("一行文字", 0, 0, 1)
    assert result.text == "# 一行文字"
    assert result.start == 0 and result.end == 0


def test_heading_same_level_twice_removes_marker():
    once = toggle_heading("一行文字", 0, 0, 2)
    assert once.text == "## 一行文字"

    twice = toggle_heading(once.text, 0, 0, 2)
    assert twice.text == "一行文字"


def test_heading_different_level_rewrites_marker():
    h2 = toggle_heading("一行文字", 0, 0, 2)
    h3 = toggle_heading(h2.text, 0, 0, 3)
    assert h3.text == "### 一行文字"


def test_heading_on_middle_line_keeps_other_lines():
    text = "第一行\n第二行\n第三行"
    result = toggle_heading(text, 4, 4, 1)
    assert result.text == "第一行\n# 第二行\n第三行"


def test_heading_multi_line_selection_toggles_each_line():
    text = "甲\n乙\n丙"
    result = toggle_heading(text, 0, len(text), 1)
    assert result.text == "# 甲\n# 乙\n# 丙"


def test_heading_does_not_touch_indented_non_heading_line_content():
    result = toggle_heading("    代码缩进行", 0, 0, 3)
    assert result.text == "    ### 代码缩进行"


def test_heading_rejects_invalid_level():
    with pytest.raises(ValueError, match="between 1 and 6"):
        toggle_heading("x", 0, 0, 7)


# ── 行内包裹（加粗/斜体/代码）─────────────────────────────────


def test_wrap_selection_wraps_selected_text():
    result = wrap_selection("选中这段文字", 0, 2, "**")
    assert result.text == "**选中**这段文字"
    assert result.start == 0 and result.end == len("**选中**")


def test_wrap_selection_no_selection_places_cursor_inside():
    result = wrap_selection("abc", 1, 1, "**")
    assert result.text == "a****bc"
    assert result.start == 3 and result.end == 3


def test_wrap_selection_reorders_reversed_selection():
    result = wrap_selection("abcdef", 4, 2, "*")
    assert result.text == "ab*cd*ef"
    assert result.start == 2 and result.end == 6


# ── 引用块（幂等）─────────────────────────────────────────────


def test_blockquote_quotes_current_line():
    result = toggle_blockquote("引用我", 0, 0)
    assert result.text == "> 引用我"


def test_blockquote_is_noop_when_already_quoted():
    """行级幂等：已引用的行重复调用不叠加。"""
    once = toggle_blockquote("引用我", 0, 0)
    twice = toggle_blockquote(once.text, 0, 0)
    assert twice.text == "> 引用我"


def test_blockquote_multi_line_quotes_each_line():
    text = "甲\n乙"
    result = toggle_blockquote(text, 0, len(text))
    assert result.text == "> 甲\n> 乙"


def test_blockquote_mixed_selection_quotes_only_unquoted_lines():
    text = "> 已引用\n未引用"
    result = toggle_blockquote(text, 0, len(text))
    assert result.text == "> 已引用\n> 未引用"


# ── 代码块 ────────────────────────────────────────────────────


def test_fence_wraps_selection_with_language():
    result = insert_fence("print(1)", 0, 8, "python")
    assert result.text == "```python\nprint(1)\n```"


def test_fence_no_selection_places_cursor_at_language_slot():
    result = insert_fence("abc", 1, 1)
    assert result.text == "a```\n\n```bc"
    assert result.start == 4 and result.end == 4


# ── 分割线 ────────────────────────────────────────────────────


def test_rule_inserts_on_own_line():
    result = insert_rule("头部文字", 4, 4)
    assert result.text == "头部文字\n---\n"


# ── 列表（有序自动递增）───────────────────────────────────────


def test_unordered_list_toggles_current_line():
    result = toggle_list("待办事项", 0, 0)
    assert result.text == "- 待办事项"

    back = toggle_list(result.text, 0, 0)
    assert back.text == "待办事项"


def test_ordered_list_auto_increments():
    text = "第一项\n第二项\n第三项"
    result = toggle_list(text, 0, len(text), ordered=True)
    assert result.text == "1. 第一项\n2. 第二项\n3. 第三项"


def test_ordered_list_toggle_strips_numbers():
    text = "1. 甲\n2. 乙"
    result = toggle_list(text, 0, len(text), ordered=True)
    assert result.text == "甲\n乙"


def test_list_multi_line_unordered():
    text = "甲\n乙"
    result = toggle_list(text, 0, len(text))
    assert result.text == "- 甲\n- 乙"


# ── 任务清单（幂等）───────────────────────────────────────────


def test_task_toggles_current_line():
    result = insert_task("做事", 0, 0)
    assert result.text == "- [ ] 做事"

    back = insert_task(result.text, 0, 0)
    assert back.text == "做事"


def test_task_multi_line_toggles_each():
    text = "甲\n乙"
    result = insert_task(text, 0, len(text))
    assert result.text == "- [ ] 甲\n- [ ] 乙"


# ── 表格骨架 ──────────────────────────────────────────────────


def test_table_inserts_skeleton():
    result = insert_table("前置", 2, 2, rows=2, cols=3)
    lines = result.text.split("\n")
    assert lines[0] == "前置| 列1 | 列2 | 列3 |"
    assert lines[1] == "| --- | --- | --- |"
    assert lines[2] == "|  |  |  |"
    assert lines[3] == "|  |  |  |"
    assert result.start == 4


def test_table_rejects_oversized_dimensions():
    with pytest.raises(ValueError, match="between 1 and 10"):
        insert_table("x", 0, 0, rows=11, cols=2)


# ── 占位符 ────────────────────────────────────────────────────


def test_placeholder_inserts_braces_and_places_cursor():
    result = insert_placeholder("使用", 2, 2)
    assert result.text == "使用{}"
    assert result.start == 3 and result.end == 3
