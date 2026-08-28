"""Markdown asset package export/import for PromptBox."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

PACKAGE_VERSION = 1
MAX_ZIP_ENTRIES = 1000
MAX_ZIP_MEMBER_BYTES = 10 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 100 * 1024 * 1024
_SUPPORTED_FIELDS = {"id", "title", "category", "tags", "status", "updated_at", "current_version_id", "stable_version_id", "variable_definitions"}
# Existing PromptBox assets may use Chinese IDs; preserve them during migration.
_ID_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9_.\-\u4e00-\u9fff]{0,127}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UNSAFE = re.compile(r'[<>:"/\\|?*]')


class AssetPackageError(ValueError):
    pass


@dataclass
class ParsedPrompt:
    id: str | None
    title: str
    category: str
    tags: list[str]
    status: str
    updated_at: str
    current_version_id: str | None
    stable_version_id: str | None
    variable_definitions: dict[str, Any]
    content: str
    unknown_fields: list[str] = field(default_factory=list)


@dataclass
class ImportItem:
    key: str
    status: str
    external_id: str | None
    path: str
    title: str = ""
    category: str = ""
    content: str = ""
    parsed: ParsedPrompt | None = None
    reason: str = ""
    unknown_fields: list[str] = field(default_factory=list)
    metadata_changed: bool = False
    content_changed: bool = False
    selectable: bool = True


@dataclass
class ImportPreview:
    items: list[ImportItem]
    package_version: int = PACKAGE_VERSION
    source_type: str = "folder"
    source_path: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    source_type: str
    source_path: str
    package_version: int
    discovered: int
    selected: int
    skipped: int
    counts: dict[str, int]
    items: list[dict[str, Any]]
    ignored_fields: dict[str, list[str]]
    rolled_back: bool
    final_status: str


@dataclass
class ExportResult:
    directory: str
    zip_path: str | None
    manifest: dict[str, Any]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def safe_filename(name: str, stable_id: str | None = None) -> str:
    name = str(name or "").strip()
    name = _UNSAFE.sub("_", name)
    name = "".join(ch if ord(ch) >= 32 else "_" for ch in name).rstrip(" .")
    name = name.replace("..", "__")
    if not name:
        name = "未命名Prompt"
    if stable_id and name.lower().endswith(".md"):
        name = name[:-3]
    return name


def safe_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AssetPackageError("path is empty")
    value = value.replace("\\", "/")
    if value.startswith("/") or value.startswith("//") or re.match(r"^[A-Za-z]:", value):
        raise AssetPackageError("absolute path is not allowed")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise AssetPackageError("path traversal is not allowed")
    if not parts[-1].lower().endswith(".md"):
        raise AssetPackageError("path must point to Markdown")
    return "/".join(parts)


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _category_name(snippet: dict[str, Any], category_names: dict[str, str] | None) -> str:
    cid = snippet.get("category_id") or snippet.get("category") or ""
    if category_names and cid in category_names:
        return category_names[cid]
    return str(cid or "未分类")


def _tag_names(snippet: dict[str, Any], tag_names: dict[str, str] | None) -> list[str]:
    tags = snippet.get("tags")
    if isinstance(tags, list) and all(isinstance(x, str) for x in tags):
        return list(tags)
    result = []
    for tid in snippet.get("tag_ids", []) or []:
        result.append((tag_names or {}).get(tid, str(tid)))
    return result


def _current_version(snippet: dict[str, Any]) -> dict[str, Any]:
    versions = snippet.get("versions") or []
    current_id = snippet.get("current_version_id")
    for version in versions:
        if version.get("id") == current_id:
            return version
    return versions[-1] if versions else {"id": current_id or "ver_1", "version_number": 1, "content": snippet.get("content", "")}


def _metadata_from_snippet(snippet: dict[str, Any], category_names=None, tag_names=None) -> dict[str, Any]:
    version = _current_version(snippet)
    return {
        "id": snippet.get("id"),
        "title": snippet.get("title", ""),
        "category": _category_name(snippet, category_names),
        "tags": _tag_names(snippet, tag_names),
        "status": snippet.get("status") or version.get("status") or "draft",
        "updated_at": snippet.get("updated_at", ""),
        "current_version_id": snippet.get("current_version_id") or version.get("id"),
        "stable_version_id": snippet.get("stable_version_id"),
        "variable_definitions": copy.deepcopy(snippet.get("variable_definitions") or {}),
    }


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\\", "\\\\").replace("\n", "\\n")


def render_prompt_markdown(snippet: dict[str, Any], *, category_name: str | None = None, tag_names: dict[str, str] | None = None) -> str:
    meta = _metadata_from_snippet(snippet, {snippet.get("category_id"): category_name} if category_name else None, tag_names)
    lines = ["---"]
    for key in ("id", "title", "category", "status", "updated_at", "current_version_id", "stable_version_id"):
        value = meta[key]
        if value is not None and value != "":
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("tags:")
    for tag in meta["tags"]:
        lines.append(f"  - {_yaml_scalar(tag)}")
    lines.append("variable_definitions:")
    for name, definition in meta["variable_definitions"].items():
        if not isinstance(definition, dict):
            definition = {"description": str(definition)}
        lines.append(f"  {name}:")
        for field_name in ("description", "example"):
            if field_name in definition:
                lines.append(f"    {field_name}: {_yaml_scalar(definition[field_name])}")
    lines.extend(["---", "", _current_version(snippet).get("content", snippet.get("content", "")), ""])
    return "\n".join(lines)


def _unescape(value: str) -> str:
    return value.replace("\\n", "\n").replace("\\\\", "\\")


def parse_prompt_markdown(source: str | bytes) -> ParsedPrompt:
    if isinstance(source, bytes):
        try:
            source = source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AssetPackageError("文件不是 UTF-8 编码") from exc
    if not isinstance(source, str) or not source.startswith("---\n"):
        raise AssetPackageError("front matter must start at file beginning")
    match = re.match(r"^---\n(.*?)\n---\n?(.*)$", source, re.S)
    if not match:
        raise AssetPackageError("front matter is incomplete")
    raw, body = match.groups()
    values: dict[str, Any] = {}
    unknown: list[str] = []
    current_list: str | None = None
    current_var: str | None = None
    current_var_field: str | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if stripped.startswith("- ") and current_list:
            values.setdefault(current_list, []).append(_unescape(stripped[2:]))
            continue
        if indent >= 4 and current_var and ":" in stripped:
            key, value = stripped.split(":", 1)
            if key in ("description", "example"):
                values.setdefault("variable_definitions", {}).setdefault(current_var, {})[key] = _unescape(value.strip())
                current_var_field = key
            continue
        if indent == 2 and stripped.endswith(":") and current_list == "variable_definitions":
            current_var = stripped[:-1].strip()
            values.setdefault("variable_definitions", {})[current_var] = {}
            continue
        if ":" not in stripped:
            raise AssetPackageError("malformed front matter")
        key, value = stripped.split(":", 1)
        value = _unescape(value.strip())
        if key not in _SUPPORTED_FIELDS:
            unknown.append(key)
            current_list = None
            current_var = None
            continue
        if key in ("tags", "variable_definitions") and not value:
            values[key] = [] if key == "tags" else {}
            current_list = key
            current_var = None
        else:
            values[key] = value
            current_list = None
            current_var = None
    body = body.lstrip("\n")
    if not body.strip():
        raise AssetPackageError("正文为空")
    identifier = values.get("id")
    if identifier is not None and not _valid_id(identifier):
        raise AssetPackageError("invalid prompt id")
    return ParsedPrompt(
        id=identifier,
        title=values.get("title", "").strip(),
        category=values.get("category", "未分类").strip() or "未分类",
        tags=values.get("tags", []) if isinstance(values.get("tags", []), list) else [],
        status=values.get("status", "draft"),
        updated_at=values.get("updated_at", ""),
        current_version_id=values.get("current_version_id"),
        stable_version_id=values.get("stable_version_id") or None,
        variable_definitions=values.get("variable_definitions", {}) if isinstance(values.get("variable_definitions", {}), dict) else {},
        content=body.rstrip("\n"),
        unknown_fields=unknown,
    )


def build_manifest(items: list[dict[str, Any]], *, exported_at: str | None = None) -> dict[str, Any]:
    manifest = {"package_version": PACKAGE_VERSION, "exported_at": exported_at or datetime.now(timezone.utc).isoformat(), "items": []}
    for item in items:
        path = safe_relative_path(item["path"])
        manifest["items"].append({
            "id": item.get("id"), "path": path,
            "content_hash": "sha256:" + sha256_text(item.get("content", "")),
            "current_version_id": item.get("current_version_id"),
            "stable_version_id": item.get("stable_version_id"),
            "variable_definitions": copy.deepcopy(item.get("variable_definitions") or {}),
        })
    return validate_manifest(manifest)


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("package_version") != PACKAGE_VERSION or not isinstance(manifest.get("items"), list):
        raise AssetPackageError("invalid manifest")
    ids: set[str] = set(); paths: set[str] = set()
    for item in manifest["items"]:
        if not isinstance(item, dict) or not _valid_id(item.get("id")):
            raise AssetPackageError("manifest item has invalid id")
        path = safe_relative_path(item.get("path", ""))
        if path in paths or item["id"] in ids:
            raise AssetPackageError("duplicate manifest id or path")
        if not isinstance(item.get("content_hash"), str) or not _HASH_RE.fullmatch(item["content_hash"]):
            raise AssetPackageError("invalid content hash")
        paths.add(path); ids.add(item["id"])
    return manifest


def select_export_snippets(snippets, scope, *, category=None, selected_ids=None, filtered_snippets=None, snippet_id=None):
    pool = list(snippets or [])
    if scope == "category":
        pool = [s for s in pool if s.get("category_id") == category or s.get("category") == category]
    elif scope == "selected":
        pool = [s for s in pool if s.get("id") in (selected_ids or set())]
    elif scope == "filtered":
        pool = list(filtered_snippets or [])
    elif scope == "single":
        pool = [s for s in pool if s.get("id") == snippet_id]
    return sorted([s for s in pool if not s.get("_deleted")], key=lambda s: str(s.get("id", "")))


def _fresh_dir(parent: Path, stamp: str | None = None) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    base = "PromptBox-资产包-" + (stamp or datetime.now().strftime("%Y%m%d-%H%M"))
    candidate = parent / base
    index = 0
    while candidate.exists():
        index += 1; candidate = parent / f"{base}-{index}"
    candidate.mkdir()
    return candidate


def export_asset_package(snippets, target_parent, *, category_names=None, tag_names=None, include_zip=False, exported_at=None, scope="all", category=None, selected_ids=None, filtered_snippets=None, snippet_id=None):
    chosen = select_export_snippets(snippets, scope, category=category, selected_ids=selected_ids, filtered_snippets=filtered_snippets, snippet_id=snippet_id)
    target_parent = Path(target_parent)
    stamp = (exported_at or "").replace("-", "").replace(":", "").replace("+00:00", "")[:13].replace("T", "-") or None
    root = _fresh_dir(target_parent, stamp)
    used: dict[str, set[str]] = {}
    manifest_items = []
    for snippet in chosen:
        category = safe_filename(_category_name(snippet, category_names))
        directory = root / category
        directory.mkdir(parents=True, exist_ok=True)
        used.setdefault(category, set())
        base = safe_filename(snippet.get("title", ""))
        filename = base + ".md"
        if filename.lower() in used[category]:
            filename = f"{base}--{snippet.get('id')}.md"
        counter = 1; original = filename
        while filename.lower() in used[category] or (directory / filename).exists():
            stem = Path(original).stem
            filename = f"{stem}-{counter}.md"; counter += 1
        used[category].add(filename.lower())
        rel = f"{category}/{filename}"
        text = render_prompt_markdown(snippet, category_name=category, tag_names=tag_names)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
        version = _current_version(snippet)
        manifest_items.append({"id": snippet.get("id"), "path": rel, "content": version.get("content", snippet.get("content", "")), "current_version_id": snippet.get("current_version_id") or version.get("id"), "stable_version_id": snippet.get("stable_version_id"), "variable_definitions": snippet.get("variable_definitions") or {}})
    manifest = build_manifest(manifest_items, exported_at=exported_at)
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    zip_path = None
    if include_zip:
        zip_path = str(root.with_suffix(".zip"))
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in root.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
    return ExportResult(str(root), zip_path, manifest)


def _validate_zip_member(info: zipfile.ZipInfo, total: int) -> int:
    name = info.filename.replace("\\", "/")
    if len(name) > 4096 or name.startswith("/") or re.match(r"^[A-Za-z]:", name) or any(p in ("..", "") for p in name.split("/")):
        raise AssetPackageError("ZIP contains unsafe path")
    if info.is_dir():
        return total
    if info.file_size > MAX_ZIP_MEMBER_BYTES or total + info.file_size > MAX_ZIP_TOTAL_BYTES:
        raise AssetPackageError("ZIP exceeds import limits")
    mode = (info.external_attr >> 16) & 0o170000
    if mode and mode != 0o100000:
        raise AssetPackageError("ZIP contains special file")
    return total + info.file_size


@contextmanager
def load_import_source(source_path) -> Iterator[Path]:
    path = Path(source_path)
    if path.is_dir():
        yield path
        return
    if not path.is_file() or path.suffix.lower() != ".zip":
        raise AssetPackageError("import source must be folder or ZIP")
    temp = Path(tempfile.mkdtemp(prefix="promptbox-import-"))
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise AssetPackageError("ZIP exceeds entry limit")
            total = 0
            for info in infos:
                total = _validate_zip_member(info, total)
            for info in infos:
                if info.is_dir():
                    continue
                destination = temp / info.filename.replace("\\", "/")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
        yield temp
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def _read_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssetPackageError("manifest.json missing") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssetPackageError("manifest.json invalid") from exc
    return validate_manifest(manifest)


def _local_metadata(snippet: dict[str, Any]) -> dict[str, Any]:
    return _metadata_from_snippet(snippet)


def analyze_import(root, local_snippets) -> ImportPreview:
    root = Path(root).resolve()
    manifest = _read_manifest(root)
    local_by_id = {s.get("id"): s for s in (local_snippets or []) if s.get("id")}
    local_by_title: dict[str, list[dict[str, Any]]] = {}
    for snippet in local_snippets or []:
        local_by_title.setdefault(str(snippet.get("title", "")), []).append(snippet)
    items: list[ImportItem] = []
    registered = set()
    for index, entry in enumerate(manifest["items"]):
        path = safe_relative_path(entry["path"]); registered.add(path)
        candidate = root / Path(path)
        key = f"{entry['id']}::{path}"
        if not _inside(root, candidate) or not candidate.is_file():
            items.append(ImportItem(key, "invalid", entry.get("id"), path, reason="manifest 登记文件不存在", selectable=False)); continue
        try:
            parsed = parse_prompt_markdown(candidate.read_bytes())
        except AssetPackageError as exc:
            items.append(ImportItem(key, "invalid", entry.get("id"), path, reason=str(exc), selectable=False)); continue
        if parsed.id != entry["id"]:
            items.append(ImportItem(key, "invalid", parsed.id, path, parsed.title, reason="Markdown ID 与 manifest 不一致", parsed=parsed, selectable=False)); continue
        expected_hash = entry["content_hash"].split(":", 1)[1]
        if sha256_text(parsed.content) != expected_hash:
            items.append(ImportItem(key, "invalid", parsed.id, path, parsed.title, reason="正文哈希不匹配", parsed=parsed, selectable=False)); continue
        local = local_by_id.get(parsed.id)
        status = "new"; content_changed = False; metadata_changed = False; reason = ""
        if local:
            local_content = _current_version(local).get("content", local.get("content", ""))
            content_changed = local_content != parsed.content
            lm = _local_metadata(local)
            incoming_meta = {"title": parsed.title, "tags": parsed.tags, "status": parsed.status, "variable_definitions": parsed.variable_definitions}
            # category_id is an internal identifier; without category-name mapping,
            # do not report a false metadata change against imported category text.
            if "category" in local:
                incoming_meta["category"] = parsed.category
            metadata_changed = any(lm.get(k) != v for k, v in incoming_meta.items())
            if not content_changed and not metadata_changed:
                status = "unchanged"
            else:
                status = "updated"
        if local_by_title.get(parsed.title) and not local:
            status = "same_name_different_id"; reason = "本地存在同名但不同 ID Prompt"
        if local and local.get("_deleted"):
            status = "deleted_id_conflict"; reason = "本地 ID 已删除，不自动复活"
        items.append(ImportItem(key, status, parsed.id, path, parsed.title, parsed.category, parsed.content, parsed, reason, parsed.unknown_fields, metadata_changed, content_changed, status not in {"invalid", "unregistered"}))
    for path in root.rglob("*.md"):
        rel = path.relative_to(root).as_posix()
        if rel not in registered:
            items.append(ImportItem(f"unregistered::{rel}", "unregistered", None, rel, reason="Markdown 未登记在 manifest.json", selectable=False))
    return ImportPreview(items, manifest["package_version"], "folder", str(root))


def _new_version_id() -> str:
    return "ver_import_" + uuid.uuid4().hex[:12]


def build_import_plan(preview: ImportPreview, selected_keys: set[str], local_data: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
    if not isinstance(preview, ImportPreview):
        raise AssetPackageError("invalid import preview")
    valid = {item.key: item for item in preview.items}
    if not selected_keys.issubset(valid):
        raise AssetPackageError("selected import item does not exist")
    for key in selected_keys:
        if not valid[key].selectable or valid[key].status in {"invalid", "unregistered", "unchanged"}:
            raise AssetPackageError("selected import item is not writable")
    return {"selected": [copy.deepcopy(valid[key]) for key in selected_keys], "local_data": copy.deepcopy(local_data), "now": now or datetime.now(timezone.utc).isoformat()}


def _category_id_for(data: dict[str, Any], name: str) -> str:
    for category in data.get("categories", []) or []:
        if category.get("name") == name:
            return category.get("id")
    category_id = "cat_" + uuid.uuid4().hex[:10]
    data.setdefault("categories", []).append({"id": category_id, "name": name, "parent_id": None, "children": []})
    return category_id


def _tag_ids_for(data: dict[str, Any], names: list[str]) -> list[str]:
    tags = data.setdefault("tags", [])
    by_name = {str(tag.get("name")): tag for tag in tags if tag.get("name")}
    result = []
    for name in names:
        clean = str(name).strip()
        if not clean:
            continue
        tag = by_name.get(clean)
        if tag is None:
            tag = {"id": "tag_import_" + uuid.uuid4().hex[:10], "name": clean}
            tags.append(tag)
            by_name[clean] = tag
        result.append(tag["id"])
    return list(dict.fromkeys(result))


def apply_import_plan(local_data: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(local_data)
    snippets = result.setdefault("snippets", [])
    by_id = {s.get("id"): s for s in snippets}
    for item in plan.get("selected", []):
        parsed = item.parsed
        if not parsed:
            raise AssetPackageError("selected item has no parsed content")
        sid = parsed.id or "pbx_import_" + uuid.uuid4().hex[:10]
        if sid in by_id and by_id[sid].get("_deleted"):
            raise AssetPackageError("cannot auto-restore deleted prompt")
        category_id = _category_id_for(result, parsed.category or "未分类")
        tag_ids = _tag_ids_for(result, parsed.tags)
        if sid not in by_id:
            version_id = parsed.current_version_id or _new_version_id()
            new_snippet = {"id": sid, "title": parsed.title or Path(item.path).stem, "category_id": category_id, "tag_ids": tag_ids, "status": parsed.status, "updated_at": plan["now"], "variable_definitions": copy.deepcopy(parsed.variable_definitions), "current_version_id": version_id, "stable_version_id": parsed.stable_version_id, "versions": [{"id": version_id, "version_number": 1, "content": parsed.content, "created_at": plan["now"], "parent_version_id": None}], "content": "", "_deleted": False}
            snippets.append(new_snippet); by_id[sid] = new_snippet; continue
        target = by_id[sid]
        current = _current_version(target)
        if item.content_changed:
            version_id = _new_version_id()
            target.setdefault("versions", []).append({"id": version_id, "version_number": len(target.get("versions", [])) + 1, "content": parsed.content, "created_at": plan["now"], "parent_version_id": current.get("id")})
            target["current_version_id"] = version_id
        target.update({"title": parsed.title or target.get("title", ""), "category_id": category_id, "tag_ids": tag_ids, "status": parsed.status, "updated_at": plan["now"], "variable_definitions": copy.deepcopy(parsed.variable_definitions)})
    result["version"] = 4
    return result


def make_import_report(*, source_type, source_path, package_version, discovered, selected, skipped, counts, items, ignored_fields, rolled_back, final_status):
    return ImportReport(source_type, source_path, package_version, discovered, selected, skipped, dict(counts), list(items), dict(ignored_fields), rolled_back, final_status)


__all__ = [name for name in globals() if not name.startswith("_")]
