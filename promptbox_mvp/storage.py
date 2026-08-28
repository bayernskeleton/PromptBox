"""JSON storage helpers for PromptBox MVP data."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


_DEFAULT_DATA = {
    "version": 4,
    "snippets": [],
    "categories": [],
    "tags": [],
    "runs": [],
    "repair_cases": [],
    "preferences": {},
}


def _default_data() -> dict[str, Any]:
    return {
        "version": 4,
        "snippets": [],
        "categories": [],
        "tags": [],
        "runs": [],
        "repair_cases": [],
        "preferences": {},
    }


def load_data(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load PromptBox data and migrate legacy JSON into the v4 envelope."""
    path = Path(path)
    if not path.exists():
        return _default_data()

    try:
        with path.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON data") from exc

    if isinstance(loaded, list):
        data = _default_data()
        data["snippets"] = loaded
        return data
    if not isinstance(loaded, dict):
        raise ValueError("stored data must be a JSON object or array")

    data = dict(loaded)
    data["version"] = 4
    data.setdefault("snippets", [])
    data.setdefault("categories", [])
    data.setdefault("tags", [])
    data.setdefault("runs", [])
    data.setdefault("repair_cases", [])
    data.setdefault("preferences", {})
    return data


def save_data(path: str | os.PathLike[str], data: Any) -> None:
    """Atomically save PromptBox data as UTF-8 JSON."""
    if not isinstance(data, dict):
        raise ValueError("data must be a dictionary")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    payload = dict(data)
    payload.setdefault("version", 4)
    payload.setdefault("snippets", [])
    payload.setdefault("categories", [])
    payload.setdefault("tags", [])
    payload.setdefault("runs", [])
    payload.setdefault("repair_cases", [])
    payload.setdefault("preferences", {})

    try:
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
        os.replace(temporary_path, path)
    except (TypeError, ValueError) as exc:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise ValueError("data is not JSON serializable") from exc
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


__all__ = ["load_data", "save_data"]
