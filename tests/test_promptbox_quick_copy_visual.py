from pathlib import Path


SOURCE = Path(__file__).parents[1] / "promptbox.py"


def _palette_source() -> str:
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("    def _open_palette(")
    end = source.index("    def _open_repair_workbench(", start)
    return source[start:end]


def test_palette_source_contains_compact_visual_regions_and_theme_tokens():
    source = _palette_source()
    for token in [
        "快速取用",
        "width, height = 520, 420",
        "_render_palette_row",
        "BG_INPUT",
        "BG_PANEL",
        "ACCENT",
    ]:
        assert token in source


def test_palette_source_contains_explicit_variable_and_history_actions():
    source = _palette_source()
    for token in [
        "_render_palette_detail",
        "_palette_show_versions",
        "_palette_start_variable_fill",
        "_palette_make_snapshot",
        "PromptTemplate.from_text",
        "复制原文",
        "填充并复制",
    ]:
        assert token in source
    assert "历史版本" not in source
    assert "填写变量（可选）" not in source


def test_palette_copy_requires_clipboard_success_before_recording_or_closing():
    source = _palette_source()
    assert "if not content or not copy_to_clipboard(content):" in source
    assert 'self._palette_show_feedback("已复制")' in source
    assert "已记录本次调用" not in source


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
    import sys
    sys.path.insert(0, str(SOURCE.parent))
    from promptbox import Win32HotkeyManager

    modifiers, virtual_key = Win32HotkeyManager._key_parts("ctrl+shift+space")
    assert modifiers & 0x0002
    assert modifiers & 0x0004
    assert virtual_key == 0x20


def test_hotkey_manager_rejects_missing_main_key():
    import sys
    sys.path.insert(0, str(SOURCE.parent))
    from promptbox import Win32HotkeyManager

    try:
        Win32HotkeyManager._key_parts("ctrl+shift")
    except ValueError as exc:
        assert "主键" in str(exc)
    else:
        raise AssertionError("missing main key should fail")


def test_hotkey_manager_registers_and_reads_messages_on_listener_thread(monkeypatch):
    import ctypes
    import threading

    import sys
    sys.path.insert(0, str(SOURCE.parent))
    from promptbox import Win32HotkeyManager

    calls = []
    stop_event = threading.Event()

    class FakeUser32:
        def PeekMessageW(self, *_args):
            calls.append(("peek", threading.get_ident()))
            return 0

        def RegisterHotKey(self, *_args):
            calls.append(("register", threading.get_ident()))
            return 1

        def GetMessageW(self, *_args):
            calls.append(("get", threading.get_ident()))
            stop_event.wait(1)
            return 0

        def PostThreadMessageW(self, thread_id, *_args):
            calls.append(("post", threading.get_ident(), thread_id))
            stop_event.set()
            return 1

        def UnregisterHotKey(self, *_args):
            calls.append(("unregister", threading.get_ident()))
            return 1

        def TranslateMessage(self, *_args):
            return 1

        def DispatchMessageW(self, *_args):
            return 1

    monkeypatch.setattr(ctypes.windll, "user32", FakeUser32(), raising=False)
    manager = Win32HotkeyManager(lambda: None)

    assert manager.start() is True
    deadline = threading.Event()
    for _ in range(100):
        if any(name == "get" for name, *rest in calls):
            break
        deadline.wait(0.01)
    listener_thread_id = next(thread_id for name, thread_id, *rest in calls if name == "register")
    assert listener_thread_id == next(thread_id for name, thread_id, *rest in calls if name == "get")

    manager.stop()
    manager.thread.join(timeout=2)
    assert not manager.thread.is_alive()
    assert any(name == "unregister" and thread_id == listener_thread_id for name, thread_id, *rest in calls)
