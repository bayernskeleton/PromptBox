from pathlib import Path


SOURCE = Path(__file__).parents[1] / "promptbox.py"


def _palette_source() -> str:
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("    def _open_palette(")
    end = source.index("    def _open_repair_workbench(", start)
    return source[start:end]


def test_palette_source_contains_required_visual_regions_and_theme_tokens():
    source = _palette_source()
    for token in [
        "快速取用",
        "搜索提示词",
        "历史版本",
        "复制原文",
        "填充并复制",
        "收藏",
        "打开完整工作台",
        "BG_INPUT",
        "BG_PANEL",
        "ACCENT",
    ]:
        assert token in source


def test_palette_source_contains_variable_and_history_actions():
    source = _palette_source()
    for token in [
        "_render_palette_detail",
        "_palette_toggle_favorite",
        "_palette_make_snapshot",
        "PromptTemplate.from_text",
        "复制成功",
    ]:
        assert token in source


def test_palette_copy_requires_clipboard_success_before_recording_or_closing():
    source = _palette_source()
    assert "if not content or not copy_to_clipboard(content):" in source
    assert "未记录调用" in source


def test_hotkey_routes_to_palette_and_reads_preference():
    source = SOURCE.read_text(encoding="utf-8")
    assert "quick_copy_hotkey" in source
    assert "app.toggle_palette" in source
    assert "Win32HotkeyManager(on_hotkey, hotkey)" in source


def test_palette_source_does_not_use_unstyled_native_button_or_text_widgets():
    source = _palette_source()
    assert "tk.Button(" not in source
    assert "tk.Text(" not in source


def test_hotkey_manager_parses_common_combinations():
    from promptbox import Win32HotkeyManager

    modifiers, virtual_key = Win32HotkeyManager._key_parts("ctrl+shift+space")
    assert modifiers & 0x0002
    assert modifiers & 0x0004
    assert virtual_key == 0x20


def test_hotkey_manager_rejects_missing_main_key():
    from promptbox import Win32HotkeyManager

    try:
        Win32HotkeyManager._key_parts("ctrl+shift")
    except ValueError as exc:
        assert "主键" in str(exc)
    else:
        raise AssertionError("missing main key should fail")
