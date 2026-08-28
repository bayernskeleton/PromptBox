from promptbox_mvp.quick_copy import (
    build_quick_copy_run,
    ensure_quick_copy_fields,
    get_selected_version,
    latest_run_times,
    search_prompts,
    version_content,
)


def test_palette_mutations_persist_to_authoritative_snippet(monkeypatch):
    from promptbox import PromptBox

    app = object.__new__(PromptBox)
    authoritative = make_snippet("a")
    displayed_copy = dict(authoritative)
    app.snippets = [authoritative]
    app.data = {"snippets": app.snippets, "runs": [], "preferences": {}}
    app.palette_selected = displayed_copy
    app.palette_selected_version_id = "a-v2"
    app.palette_search_var = type("SearchVar", (), {"get": lambda self: ""})()
    monkeypatch.setattr(app, "_render_palette_results", lambda _query: None)
    saved = []
    monkeypatch.setattr(app, "_save_data", lambda: saved.append(True))

    app._palette_toggle_favorite(displayed_copy)

    assert authoritative["is_favorite"] is True
    assert saved == [True]


def test_palette_snapshot_persists_to_authoritative_snippet(monkeypatch):
    from promptbox import PromptBox
    from promptbox_mvp.prompt_variables import PromptTemplate

    app = object.__new__(PromptBox)
    authoritative = make_snippet("a")
    authoritative["snapshots"] = []
    displayed_copy = dict(authoritative)
    app.snippets = [authoritative]
    app.data = {"snippets": app.snippets, "runs": [], "preferences": {}}
    app.palette_variable_entries = {}
    monkeypatch.setattr(app, "_save_data", lambda: None)
    template = PromptTemplate.from_text("整理 {topic}")

    snapshot = app._palette_make_snapshot(displayed_copy, "a-v2", template, "整理 周报")

    assert snapshot["id"]
    assert len(authoritative["snapshots"]) == 1
    assert authoritative["snapshots"][0]["rendered_prompt"] == "整理 周报"


def make_snippet(sid="a", *, title="写周报", favorite=False, deleted=False, tagged=True):
    return {
        "id": sid,
        "title": title,
        "category_id": "cat_work",
        "tag_ids": ["tag_doc"] if tagged else [],
        "is_favorite": favorite,
        "_deleted": deleted,
        "updated_at": "2026-08-26T10:00:00+00:00",
        "current_version_id": f"{sid}-v2",
        "stable_version_id": f"{sid}-v1",
        "versions": [
            {"id": f"{sid}-v1", "version_number": 1, "content": "旧内容"},
            {"id": f"{sid}-v2", "version_number": 2, "content": "根据输入整理周报"},
        ],
    }


def test_search_accepts_direct_tag_names_metadata():
    snippet = make_snippet("direct")
    snippet["tag_names"] = ["文档"]
    snippet["tag_ids"] = []
    result = search_prompts([snippet], "文档")
    assert [item["id"] for item in result] == ["direct"]


def test_search_returns_one_result_per_prompt_and_matches_metadata():
    result = search_prompts(
        [make_snippet(), make_snippet("b", title="会议纪要", tagged=False)],
        "文档",
        category_names={"cat_work": "工作"},
        tag_names={"tag_doc": "文档"},
    )
    assert [item["id"] for item in result] == ["a"]


def test_get_selected_version_defaults_to_current_not_stable():
    snippet = make_snippet()
    assert get_selected_version(snippet)["id"] == "a-v2"
    assert get_selected_version(snippet, "a-v1")["id"] == "a-v1"
    assert version_content(snippet) == "根据输入整理周报"


def test_ensure_quick_copy_fields_adds_favorite_without_changing_existing_value():
    snippets = [{"id": "a"}, {"id": "b", "is_favorite": True}]
    ensure_quick_copy_fields(snippets)
    assert snippets[0]["is_favorite"] is False
    assert snippets[1]["is_favorite"] is True


def test_search_excludes_deleted_and_orders_recent_before_un_called():
    snippets = [make_snippet("old", title="旧"), make_snippet("new", title="新")]
    results = search_prompts(
        snippets,
        category_names={"cat_work": "工作"},
        tag_names={"tag_doc": "文档"},
        runs=[{"snippet_id": "new", "created_at": "2026-08-26T12:00:00+00:00"}],
    )
    assert [item["id"] for item in results] == ["new", "old"]
    assert not search_prompts([make_snippet("gone", deleted=True)])


def test_favorite_sort_mode_puts_favorite_first_without_filtering():
    results = search_prompts(
        [make_snippet("plain"), make_snippet("fav", favorite=True)],
        sort_mode="favorite",
    )
    assert [item["id"] for item in results] == ["fav", "plain"]


def test_latest_run_times_keeps_newest_call_per_prompt():
    latest = latest_run_times([
        {"snippet_id": "a", "created_at": "2026-08-26T12:00:00+00:00"},
        {"snippet_id": "a", "created_at": "2026-08-26T11:00:00+00:00"},
    ])
    assert latest["a"] == "2026-08-26T12:00:00+00:00"


def test_build_quick_copy_run_has_trigger_and_nullable_rating():
    run = build_quick_copy_run("a", "a-v2", "snap_1", "2026-08-26T12:00:00+00:00", run_id="run_1")
    assert run == {
        "id": "run_1",
        "snippet_id": "a",
        "version_id": "a-v2",
        "rating": None,
        "note": "",
        "trigger": "quick_copy",
        "variable_snapshot_id": "snap_1",
        "created_at": "2026-08-26T12:00:00+00:00",
    }
