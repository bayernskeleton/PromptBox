from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_asset_package_actions_use_shared_visual_tokens_and_have_handlers():
    source = (ROOT / "promptbox.py").read_text(encoding="utf-8")
    assert '"导入资产包", self._import_asset_package, BTN_SECONDARY, BTN_SECONDARY_FG' in source
    assert '"导出资产包", self._export_asset_package, BTN_SECONDARY, BTN_SECONDARY_FG' in source
    assert 'def _export_asset_package(self):' in source
    assert 'def _import_asset_package(self):' in source
    assert 'def _apply_import_asset_package(self):' in source
    assert 'bg=BG, fg=FG' in source
    assert 'style="PB.Treeview"' in source


def test_asset_package_gui_keeps_import_preview_explicit_and_non_silent():
    source = (ROOT / "promptbox.py").read_text(encoding="utf-8")
    assert "逐条选择要应用的变化" in source
    assert "未变化和非法项不会写回" in source
    assert "确认写回" in source
    assert "原版本已保留" in source
