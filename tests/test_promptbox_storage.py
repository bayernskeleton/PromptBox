import json
from pathlib import Path

import pytest

from promptbox_mvp.storage import load_data, save_data


def test_load_missing_file_returns_default_envelope(tmp_path: Path) -> None:
    data = load_data(tmp_path / "data.json")
    assert data == {
        "version": 4,
        "snippets": [],
        "categories": [],
        "tags": [],
        "runs": [],
        "repair_cases": [],
        "preferences": {},
    }


def test_load_v3_dict_preserves_fields(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text(
        json.dumps({"version": 3, "snippets": ["a"], "categories": ["x"], "custom": 1}),
        encoding="utf-8",
    )
    data = load_data(path)
    assert data["version"] == 4
    assert data["snippets"] == ["a"]
    assert data["categories"] == ["x"]
    assert data["custom"] == 1
    assert data["tags"] == []
    assert data["runs"] == []
    assert data["repair_cases"] == []
    assert data["preferences"] == {}


def test_load_legacy_list_wraps_snippets(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text(json.dumps([{"name": "legacy"}]), encoding="utf-8")
    assert load_data(path)["snippets"] == [{"name": "legacy"}]


def test_save_empty_dict_writes_complete_envelope(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    save_data(path, {})

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 4
    assert raw["snippets"] == []
    assert raw["categories"] == []
    assert raw["tags"] == []
    assert raw["runs"] == []
    assert raw["repair_cases"] == []
    assert raw["preferences"] == {}


def test_repair_cases_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "data.json"
    repair_cases = [{"id": "case-1", "status": "open"}]
    save_data(path, {"snippets": [], "repair_cases": repair_cases})
    assert load_data(path)["repair_cases"] == repair_cases


def test_load_invalid_json_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_data(path)


def test_load_invalid_top_level_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text(json.dumps("invalid"), encoding="utf-8")
    with pytest.raises(ValueError):
        load_data(path)


def test_save_is_atomic_and_leaves_no_tmp(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    save_data(path, {"snippets": ["value"]})
    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 4


def test_save_rejects_non_serializable_data_and_cleans_tmp(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    with pytest.raises(ValueError):
        save_data(path, {"bad": object()})
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_unicode_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    value = "提示词演化"
    save_data(path, {"snippets": [value]})
    assert load_data(path)["snippets"] == [value]
    assert value in path.read_text(encoding="utf-8")
