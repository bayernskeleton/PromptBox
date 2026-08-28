"""Pure Quick Copy Palette search, version and usage helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def ensure_quick_copy_fields(snippets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for snippet in snippets:
        snippet.setdefault("is_favorite", False)
    return snippets


def get_selected_version(
    snippet: dict[str, Any], version_id: str | None = None
) -> dict[str, Any] | None:
    target_id = version_id or snippet.get("current_version_id")
    return next(
        (version for version in snippet.get("versions", []) if version.get("id") == target_id),
        None,
    )


def version_content(snippet: dict[str, Any], version_id: str | None = None) -> str:
    version = get_selected_version(snippet, version_id)
    if version is not None:
        return str(version.get("content", ""))
    return str(snippet.get("content", ""))


def _timestamp(value: str | None) -> float:
    if not value:
        return float("-inf")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return float("-inf")


def latest_run_times(runs: list[dict[str, Any]]) -> dict[str, str]:
    latest: dict[str, str] = {}
    for run in runs:
        snippet_id = run.get("snippet_id")
        created_at = str(run.get("created_at", ""))
        if not snippet_id:
            continue
        if _timestamp(created_at) > _timestamp(latest.get(snippet_id)):
            latest[snippet_id] = created_at
    return latest


def search_prompts(
    snippets: list[dict[str, Any]],
    query: str = "",
    *,
    category_names: dict[str, str] | None = None,
    tag_names: dict[str, str] | None = None,
    runs: list[dict[str, Any]] | None = None,
    run_times: dict[str, str] | None = None,
    sort_mode: str = "recent",
) -> list[dict[str, Any]]:
    query = query.casefold().strip()
    category_names = category_names or {}
    tag_names = tag_names or {}
    if run_times is None:
        run_times = latest_run_times(runs or [])

    result: list[dict[str, Any]] = []
    for snippet in snippets:
        if snippet.get("_deleted"):
            continue
        category = category_names.get(snippet.get("category_id"), snippet.get("category_name", ""))
        direct_tags = snippet.get("tag_names", [])
        tags = [tag_names.get(tag_id, tag_id) for tag_id in snippet.get("tag_ids", [])]
        if not tags and direct_tags:
            tags = [str(tag) for tag in direct_tags]
        fields = {
            "title": str(snippet.get("title", "")),
            "tags": " ".join(tags),
            "category": str(category),
            "content": version_content(snippet),
        }
        matches = [name for name, value in fields.items() if query and query in value.casefold()]
        if query and not matches:
            continue
        item = dict(snippet)
        item["_quick_copy_category_name"] = category
        item["_quick_copy_tag_names"] = tags
        item["_quick_copy_match_fields"] = matches
        item["_quick_copy_last_run_at"] = run_times.get(snippet.get("id"), "")
        result.append(item)

    def key(item: dict[str, Any]) -> tuple[Any, ...]:
        recent = _timestamp(item.get("_quick_copy_last_run_at"))
        updated = _timestamp(item.get("updated_at"))
        favorite = 0 if item.get("is_favorite") else 1
        mode_rank = {
            "favorite": favorite,
            "stable": 0 if item.get("stable_version_id") else 1,
            "recent": 0,
        }.get(sort_mode, 0)
        match_rank = min(
            ["title", "tags", "category", "content"].index(field)
            for field in item.get("_quick_copy_match_fields", [])
        ) if item.get("_quick_copy_match_fields") else 4
        return (
            match_rank if query else mode_rank,
            -recent if recent != float("-inf") else float("inf"),
            favorite,
            -updated if updated != float("-inf") else float("inf"),
            str(item.get("title", "")).casefold(),
        )

    result.sort(key=key)
    return result


def build_quick_copy_run(
    snippet_id: str,
    version_id: str,
    variable_snapshot_id: str | None,
    created_at: str,
    *,
    run_id: str,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "snippet_id": snippet_id,
        "version_id": version_id,
        "rating": None,
        "note": "",
        "trigger": "quick_copy",
        "variable_snapshot_id": variable_snapshot_id,
        "created_at": created_at,
    }
