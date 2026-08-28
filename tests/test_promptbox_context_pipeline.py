import pytest

from promptbox_mvp.context_pipeline import build_context_from_selection


def test_selection_pipeline_returns_manifest_and_pack(tmp_path):
    path = tmp_path / "README.md"
    path.write_text("说明", encoding="utf-8")

    manifest, pack = build_context_from_selection([path])

    assert manifest.entries[0].relative_path == "README.md"
    assert pack.file_count == 1
    assert "README.md" in pack.text


def test_selection_pipeline_rejects_pack_when_all_files_fail(tmp_path):
    path = tmp_path / "legacy.doc"
    path.write_bytes(b"old office")

    with pytest.raises(ValueError, match="没有文件成功解析"):
        build_context_from_selection([path])
