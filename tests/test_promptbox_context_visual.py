from pathlib import Path


WORKBENCH = Path(__file__).parents[1] / "promptbox_mvp" / "workbench.py"


def _context_source() -> str:
    source = WORKBENCH.read_text(encoding="utf-8")
    start = source.index("        # ── 业务上下文")
    end = source.index("        mode_frame =", start)
    return source[start:end]


def test_context_controls_reuse_workbench_theme_helpers():
    source = _context_source()

    assert "make_button(ctx_buttons" in source
    assert "make_text(context_frame" in source
    assert "make_label(provenance_frame" in source
    assert "make_entry(provenance_frame" in source
    assert "bg=colors[\"panel\"]" in source
    assert "manifest_text" in source


def test_context_controls_do_not_use_unstyled_native_widgets():
    source = _context_source()

    assert "tk.Button(ctx_buttons" not in source
    assert "tk.Label(context_frame" not in source
    assert "tk.Entry(provenance_frame" not in source
    assert "context_text = tk.Text" not in source
