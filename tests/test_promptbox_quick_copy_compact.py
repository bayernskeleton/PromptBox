from pathlib import Path


SOURCE = Path(__file__).parents[1] / "promptbox.py"


def palette_source() -> str:
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("    def _open_palette(")
    end = source.index("    def _open_repair_workbench(", start)
    return source[start:end]


def test_palette_uses_compact_geometry_and_result_rows():
    source = palette_source()
    assert "width, height = 520, 420" in source
    assert "_render_palette_row" in source
    assert "preview[:100]" not in source


def test_palette_does_not_expand_variables_or_history_by_default():
    source = palette_source()
    assert "填写变量（可选）" not in source
    assert "历史版本" not in source
    assert "_palette_show_versions" in source
    assert "_palette_start_variable_fill" in source


def test_palette_keeps_direct_enter_copy_for_unfilled_variables():
    source = palette_source()
    assert "_palette_copy_selected" in source
    assert "复制原文" in source


def test_palette_has_keyboard_navigation_and_direct_copy_bindings():
    source = palette_source()
    assert "<Up>" in source
    assert "<Down>" in source
    assert "<Return>" in source
    assert "palette_selected_index" in source
    assert "_palette_copy_selected" in source


def test_palette_exposes_version_and_variable_actions_only_as_explicit_methods():
    source = palette_source()
    assert "def _palette_show_versions(" in source
    assert "def _palette_start_variable_fill(" in source
    assert "PromptTemplate.from_text" in source
    assert "复制原文" in source


def test_palette_version_number_click_expands_versions_without_only_selecting_row():
    source = palette_source()
    assert "_palette_show_versions_for_item" in source


def test_palette_never_routes_default_copy_to_workbench_or_paste():
    source = palette_source()
    assert "do_paste(" not in source
    assert "RepairService" not in source
    assert "_open_full_from_palette" in source
