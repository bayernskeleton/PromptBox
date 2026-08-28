"""Variable-filled Prompt snapshots."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any

from .prompt_variables import PromptTemplate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def create_snapshot(
    snippet_id: str,
    version_id: str,
    trigger: str,
    template: str,
    variable_definitions: dict[str, dict[str, Any]] | None,
    variables: dict[str, str],
    rendered_prompt: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one immutable record of a filled Prompt operation."""
    if not isinstance(snippet_id, str) or not snippet_id:
        raise ValueError("快照缺少 Prompt 编号")
    if not isinstance(version_id, str) or not version_id:
        raise ValueError("快照缺少版本编号")
    if not isinstance(trigger, str) or not trigger.strip():
        raise ValueError("快照缺少触发来源")
    if not isinstance(template, str):
        raise ValueError("快照模板必须是字符串")
    if not isinstance(variables, dict):
        raise ValueError("快照变量必须是字典")

    definitions = deepcopy(variable_definitions or {})
    values = {str(key): str(value) for key, value in variables.items()}
    rendered = rendered_prompt
    if rendered is None:
        rendered = PromptTemplate.from_text(template, definitions).render(values)
    if not isinstance(rendered, str):
        raise ValueError("快照最终 Prompt 必须是字符串")

    snapshot = {
        "id": "snapshot_" + uuid4().hex,
        "snippet_id": snippet_id,
        "version_id": version_id,
        "trigger": trigger.strip(),
        "created_at": _now(),
        "template": template,
        "variable_definitions": definitions,
        "variables": values,
        "rendered_prompt": rendered,
    }
    if extra:
        snapshot.update(deepcopy(extra))
    return snapshot


def append_snapshot(snippet: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Append a snapshot to its Prompt without replacing previous records."""
    if not isinstance(snippet, dict):
        raise ValueError("Prompt 数据必须是字典")
    if not isinstance(snapshot, dict) or not snapshot.get("id"):
        raise ValueError("快照数据无效")
    snapshots = snippet.setdefault("snapshots", [])
    if not isinstance(snapshots, list):
        raise ValueError("Prompt 快照列表无效")
    snapshots.append(deepcopy(snapshot))
    return deepcopy(snapshot)


def get_snapshots(snippet: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the current Prompt's snapshots, newest first."""
    if not isinstance(snippet, dict):
        return []
    snapshots = snippet.get("snapshots", [])
    if not isinstance(snapshots, list):
        return []
    indexed = [
        (index, deepcopy(item))
        for index, item in enumerate(snapshots)
        if isinstance(item, dict)
    ]
    return [
        item
        for _index, item in sorted(
            indexed,
            key=lambda pair: (pair[1].get("created_at", ""), pair[0]),
            reverse=True,
        )
    ]


__all__ = ["append_snapshot", "create_snapshot", "get_snapshots"]
