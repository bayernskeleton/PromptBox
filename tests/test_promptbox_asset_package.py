import hashlib
import json
from pathlib import Path

import pytest

from promptbox_mvp.asset_package import (
    AssetPackageError,
    ImportItem,
    apply_import_plan,
    analyze_import,
    build_import_plan,
    build_manifest,
    export_asset_package,
    make_import_report,
    parse_prompt_markdown,
    render_prompt_markdown,
    select_export_snippets,
    sha256_text,
)


def snippet(sid="pbx_1", title="示例 Prompt", content="请总结 {topic}", category_id="cat_a"):
    return {
        "id": sid,
        "title": title,
        "category_id": category_id,
        "tag_ids": ["tag_one"],
        "status": "draft",
        "updated_at": "2026-08-27T10:00:00Z",
        "variable_definitions": {"topic": {"description": "主题", "example": "AI"}},
        "current_version_id": "ver_1",
        "stable_version_id": None,
        "versions": [{"id": "ver_1", "version_number": 1, "content": content}],
        "_deleted": False,
    }


def test_render_and_parse_markdown_round_trip_preserves_supported_metadata():
    source = snippet()
    text = render_prompt_markdown(source, category_name="写作")
    assert text.startswith("---\n")
    assert "id: pbx_1" in text
    assert "variable_definitions:" in text
    parsed = parse_prompt_markdown(text)
    assert parsed.id == "pbx_1"
    assert parsed.title == "示例 Prompt"
    assert parsed.category == "写作"
    assert parsed.tags == ["tag_one"]
    assert parsed.content == "请总结 {topic}"
    assert parsed.variable_definitions["topic"]["example"] == "AI"


def test_parse_reports_unknown_fields_and_rejects_empty_or_malformed_body():
    text = "---\nid: pbx_1\ntitle: 标题\nunknown: ignored\n---\n\n正文"
    parsed = parse_prompt_markdown(text)
    assert "unknown" in parsed.unknown_fields
    with pytest.raises(AssetPackageError):
        parse_prompt_markdown("---\nid: pbx_1\n---\n")
    with pytest.raises(AssetPackageError):
        parse_prompt_markdown("id: pbx_1\n---\n正文")


def test_manifest_contains_body_hash_and_validator_rejects_bad_shape():
    item = {
        "id": "pbx_1",
        "path": "写作/示例.md",
        "content": "正文",
        "current_version_id": "ver_1",
        "stable_version_id": None,
        "variable_definitions": {},
    }
    manifest = build_manifest([item], exported_at="2026-08-27T10:00:00+00:00")
    assert manifest["package_version"] == 1
    assert manifest["items"][0]["content_hash"] == "sha256:" + sha256_text("正文")
    assert manifest["items"][0]["path"] == "写作/示例.md"
    from promptbox_mvp.asset_package import validate_manifest
    assert validate_manifest(manifest) == manifest
    with pytest.raises(AssetPackageError):
        validate_manifest({"package_version": 99, "items": []})
    with pytest.raises(AssetPackageError):
        validate_manifest({"package_version": 1, "items": [{"id": "x", "path": "../x.md"}]})


def test_export_scopes_exclude_deleted_and_create_directory_and_zip(tmp_path: Path):
    first = snippet("pbx_1", "同名", category_id="cat_a")
    second = snippet("pbx_2", "另一个", category_id="cat_b")
    deleted = snippet("pbx_3", "删除", category_id="cat_a")
    deleted["_deleted"] = True
    snippets = [second, deleted, first]
    assert [s["id"] for s in select_export_snippets(snippets, "all")] == ["pbx_1", "pbx_2"]
    assert [s["id"] for s in select_export_snippets(snippets, "category", category="cat_a")] == ["pbx_1"]
    assert [s["id"] for s in select_export_snippets(snippets, "selected", selected_ids={"pbx_2"})] == ["pbx_2"]
    result = export_asset_package(
        snippets,
        tmp_path,
        category_names={"cat_a": "写作", "cat_b": "调研"},
        include_zip=True,
        exported_at="2026-08-27T10:00:00+00:00",
    )
    package_dir = Path(result.directory)
    assert package_dir.is_dir()
    assert Path(result.zip_path).is_file()
    assert (package_dir / "manifest.json").is_file()
    assert len(list(package_dir.rglob("*.md"))) == 2
    assert json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))["items"]


def test_export_accepts_legacy_unicode_ids_and_manifest_keeps_them_stable(tmp_path: Path):
    source = snippet("snip_中文_1", "兼容旧资产", "正文")
    result = export_asset_package([source], tmp_path, category_names={"cat_a": "写作"})
    manifest = json.loads((Path(result.directory) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["items"][0]["id"] == "snip_中文_1"
    preview = analyze_import(Path(result.directory), [])
    assert preview.items[0].status == "new"


def test_export_sanitizes_names_and_does_not_overwrite_existing_package(tmp_path: Path):
    source = snippet("pbx_a", '坏<>:"/\\|?*名. ', "正文")
    first = export_asset_package([source], tmp_path, category_names={"cat_a": "../逃逸"}, exported_at="2026-08-27T10:00:00+00:00")
    second = export_asset_package([source], tmp_path, category_names={"cat_a": "../逃逸"}, exported_at="2026-08-27T10:00:00+00:00")
    assert Path(first.directory) != Path(second.directory)
    assert all(tmp_path.resolve() in p.resolve().parents for p in Path(second.directory).rglob("*"))
    assert not any(".." in p.name for p in Path(second.directory).rglob("*"))


def test_folder_and_zip_import_have_same_preview(tmp_path: Path):
    source = snippet()
    exported = export_asset_package([source], tmp_path, category_names={"cat_a": "写作"}, include_zip=True)
    local = [snippet()]
    folder_preview = analyze_import(Path(exported.directory), local)
    with __import__("promptbox_mvp.asset_package", fromlist=["load_import_source"]).load_import_source(exported.zip_path) as root:
        zip_preview = analyze_import(root, local)
    assert [(i.status, i.external_id) for i in folder_preview.items] == [(i.status, i.external_id) for i in zip_preview.items]
    assert folder_preview.items[0].status == "unchanged"


def test_import_preview_classifies_new_updated_same_name_and_invalid(tmp_path: Path):
    local = [snippet("pbx_local", "重复名", "旧正文")]
    incoming = [snippet("pbx_new", "重复名", "新正文"), snippet("pbx_local", "重复名", "更新正文")]
    exported = export_asset_package(incoming, tmp_path, category_names={"cat_a": "写作"})
    preview = analyze_import(Path(exported.directory), local)
    statuses = {item.external_id: item.status for item in preview.items}
    assert statuses["pbx_new"] == "same_name_different_id"
    assert statuses["pbx_local"] == "updated"


def test_import_transaction_is_in_memory_and_creates_version_for_body_change(tmp_path: Path):
    local = {"version": 4, "snippets": [snippet("pbx_local", "标题", "旧正文")], "categories": [], "tags": []}
    incoming = snippet("pbx_local", "标题", "新正文")
    exported = export_asset_package([incoming], tmp_path, category_names={"cat_a": "写作"})
    preview = analyze_import(Path(exported.directory), local["snippets"])
    selected = {item.key for item in preview.items if item.status == "updated"}
    plan = build_import_plan(preview, selected, local, now="2026-08-27T11:00:00Z")
    result = apply_import_plan(local, plan)
    assert result is not local
    assert local["snippets"][0]["versions"][-1]["content"] == "旧正文"
    updated = result["snippets"][0]
    assert updated["versions"][-1]["content"] == "新正文"
    assert updated["current_version_id"] != "ver_1"


def test_import_transaction_adds_new_item_and_rejects_invalid_selection(tmp_path: Path):
    local = {"version": 4, "snippets": [], "categories": [], "tags": []}
    exported = export_asset_package([snippet("pbx_new")], tmp_path, category_names={"cat_a": "写作"})
    preview = analyze_import(Path(exported.directory), [])
    plan = build_import_plan(preview, {preview.items[0].key}, local, now="2026-08-27T11:00:00Z")
    result = apply_import_plan(local, plan)
    assert result["snippets"][0]["id"] == "pbx_new"
    with pytest.raises(AssetPackageError):
        build_import_plan(preview, {"missing-key"}, local)


def test_report_contains_counts_reasons_and_rollback_state():
    report = make_import_report(
        source_type="folder", source_path="/tmp/package", package_version=1,
        discovered=3, selected=1, skipped=2,
        counts={"new": 1, "updated": 0, "unchanged": 1, "invalid": 1},
        items=[{"path": "bad.md", "reason": "正文为空"}],
        ignored_fields={"bad.md": ["custom"]}, rolled_back=False, final_status="success",
    )
    assert report.source_type == "folder"
    assert report.discovered == 3
    assert report.counts["invalid"] == 1
    assert report.ignored_fields["bad.md"] == ["custom"]
    assert report.final_status == "success"


def test_import_uses_default_category_for_uncategorized_prompt(tmp_path: Path):
    exported = export_asset_package([snippet("pbx_uncat", "未分类项", "正文")], tmp_path, category_names={"cat_a": "未分类"})
    local = {"version": 4, "snippets": [], "categories": [{"id": "cat_inbox", "name": "未分类"}], "tags": []}
    preview = analyze_import(Path(exported.directory), [])
    plan = build_import_plan(preview, {preview.items[0].key}, local, now="2026-08-27T11:00:00Z")
    result = apply_import_plan(local, plan)
    assert result["snippets"][0]["category_id"] == "cat_inbox"
    assert [c["name"] for c in result["categories"]].count("未分类") == 1


def test_import_maps_tag_names_to_existing_and_new_tag_ids(tmp_path: Path):
    incoming = snippet("pbx_tags", "带标签", "正文")
    exported = export_asset_package([incoming], tmp_path, category_names={"cat_a": "写作"}, tag_names={"tag_one": "已有标签"})
    local = {"version": 4, "snippets": [], "categories": [], "tags": [{"id": "tag_existing", "name": "已有标签"}]}
    preview = analyze_import(Path(exported.directory), [])
    plan = build_import_plan(preview, {preview.items[0].key}, local, now="2026-08-27T11:00:00Z")
    result = apply_import_plan(local, plan)
    imported = result["snippets"][0]
    assert imported["tag_ids"] == ["tag_existing"]
    assert not any(tag["name"] == "已有标签" and tag["id"] != "tag_existing" for tag in result["tags"])
