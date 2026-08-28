import json
import zipfile
from pathlib import Path

import pytest

from promptbox_mvp.asset_package import (
    AssetPackageError,
    MAX_ZIP_ENTRIES,
    analyze_import,
    load_import_source,
    parse_prompt_markdown,
    safe_relative_path,
    validate_manifest,
)


def test_safe_relative_path_rejects_traversal_absolute_drive_and_unc():
    for value in ["../x.md", "a/../../x.md", "/tmp/x.md", "C:/x.md", "\\\\server\\share\\x.md"]:
        with pytest.raises(AssetPackageError):
            safe_relative_path(value)


def test_folder_import_rejects_symlink_escape(tmp_path: Path):
    root = tmp_path / "package"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = root / "escape.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")
    (root / "manifest.json").write_text(json.dumps({"package_version": 1, "exported_at": "x", "items": []}), encoding="utf-8")
    with pytest.raises(AssetPackageError):
        analyze_import(root, [])


def test_zip_import_rejects_traversal_and_entry_count_limit(tmp_path: Path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("../escape.md", "x")
    with pytest.raises(AssetPackageError):
        with load_import_source(bad):
            pass

    too_many = tmp_path / "many.zip"
    with zipfile.ZipFile(too_many, "w") as archive:
        for index in range(MAX_ZIP_ENTRIES + 1):
            archive.writestr(f"x{index}.txt", "x")
    with pytest.raises(AssetPackageError):
        with load_import_source(too_many):
            pass


def test_manifest_rejects_duplicate_ids_paths_and_bad_hashes():
    base = {"package_version": 1, "exported_at": "2026-08-27T10:00:00Z", "items": []}
    item = {"id": "pbx_1", "path": "a.md", "content_hash": "sha256:" + "a" * 64, "current_version_id": "v1", "stable_version_id": None, "variable_definitions": {}}
    validate_manifest({**base, "items": [item]})
    with pytest.raises(AssetPackageError):
        validate_manifest({**base, "items": [item, dict(item, path="b.md")]})
    with pytest.raises(AssetPackageError):
        validate_manifest({**base, "items": [item, dict(item, id="pbx_2")]})
    with pytest.raises(AssetPackageError):
        validate_manifest({**base, "items": [dict(item, content_hash="bad")]})


def test_parser_rejects_invalid_utf8_and_empty_body():
    with pytest.raises(AssetPackageError):
        parse_prompt_markdown(b"\xff\xfe")
    with pytest.raises(AssetPackageError):
        parse_prompt_markdown("---\nid: pbx_1\ntitle: x\n---\n  \n")


def test_import_marks_unregistered_and_missing_manifest_files(tmp_path: Path):
    root = tmp_path / "package"
    root.mkdir()
    (root / "extra.md").write_text("---\nid: pbx_extra\ntitle: Extra\n---\n正文", encoding="utf-8")
    manifest = {"package_version": 1, "exported_at": "2026-08-27T10:00:00Z", "items": [{"id": "pbx_missing", "path": "missing.md", "content_hash": "sha256:" + "a" * 64, "current_version_id": "v1", "stable_version_id": None, "variable_definitions": {}}]}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    preview = analyze_import(root, [])
    assert {item.status for item in preview.items} >= {"unregistered", "invalid"}
