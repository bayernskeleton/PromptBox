import importlib.util
import sys
import types
from pathlib import Path

import pytest

from promptbox_mvp.ai_service import RepairService
from promptbox_mvp.storage import load_data, save_data
from promptbox_mvp.workbench import RepairWorkbench


def _transport_result(messages, diagnosis, candidate, reasons):
    user_content = next(message["content"] for message in messages if message["role"] == "user")
    prompt = user_content.split("原始提示词:\n", 1)[1].split("\n\n失败输出:", 1)[0]
    return {
        "diagnosis": diagnosis,
        "mode": "B",
        "candidate": candidate,
        "reasons": reasons,
    }


def fake_transport(messages):
    return _transport_result(messages, "输出结构缺失", "修复后的 Prompt", ["补足输出结构"])


def audit_warning_error_transport(messages):
    return _transport_result(
        messages,
        "原 Prompt 缺少可验证输出契约",
        "请总结会议记录。仅依据原文；输出三条要点。",
        ["补充输出契约", "保留范围边界"],
    )


def structural_warning_transport(messages):
    return _transport_result(
        messages,
        "原 Prompt 缺少输入边界",
        "仅依据以下文本总结会议记录；输出三条要点。",
        ["补充材料边界", "补充输出契约"],
    )


def constraint_executability_transport(messages):
    return _transport_result(
        messages,
        "原 Prompt 存在直接约束冲突",
        "请总结会议记录；仅依据原文；输出三条要点。",
        ["移除互斥约束", "补充输出契约"],
    )


def unresolved_constraint_transport(_messages):
    return {
        "diagnosis": "存在约束问题，暂保留待人工处理",
        "mode": "B",
        "candidate": "请总结会议记录。仅依据原文；尽量简洁；输出三条要点。",
        "reasons": ["保留候选，等待人工验证"],
    }


def _load_promptbox_module():
    tkinter = types.ModuleType("tkinter")
    tkinter.ttk = types.SimpleNamespace()
    tkinter.messagebox = types.SimpleNamespace()
    tkinter.simpledialog = types.SimpleNamespace()
    sys.modules.setdefault("tkinter", tkinter)
    sys.modules.setdefault("tkinter.ttk", tkinter.ttk)
    sys.modules.setdefault("tkinter.messagebox", tkinter.messagebox)
    sys.modules.setdefault("tkinter.simpledialog", tkinter.simpledialog)
    sys.modules.setdefault("keyboard", types.ModuleType("keyboard"))
    sys.modules.setdefault("pyperclip", types.ModuleType("pyperclip"))

    module_path = Path(__file__).resolve().parents[1] / "promptbox.py"
    spec = importlib.util.spec_from_file_location("promptbox_for_e2e", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_validated_version_exposes_evidence_action_only_when_record_exists():
    promptbox = _load_promptbox_module()
    snippet = {
        "versions": [
            {"id": "ver_2", "repair_case_id": None},
            {"id": "ver_3", "repair_case_id": "case_1"},
        ]
    }

    assert promptbox.version_evidence_action(snippet, "ver_3") == "view_evidence"
    assert promptbox.version_evidence_action(snippet, "ver_2") is None
    assert promptbox.version_evidence_action(snippet, "ver_missing") is None


def test_verified_repair_creates_traceable_new_version_and_persists_bidirectional_links(tmp_path):
    promptbox = _load_promptbox_module()
    path = tmp_path / "snippets.json"
    promptbox.SNIPPETS_FILE = str(path)
    app = promptbox.PromptBox(repair_transport=fake_transport)
    app.data = promptbox.normalize_prompt_data(
        {
            "version": 4,
            "snippets": [
                {
                    "id": "snip_1",
                    "title": "待办提取",
                    "category_id": "cat_1",
                    "tag_ids": ["tag_1"],
                    "current_version_id": "ver_2",
                    "stable_version_id": "ver_1",
                    "versions": [
                        {
                            "id": "ver_1",
                            "version_number": 1,
                            "content": "旧 Prompt",
                            "status": promptbox.VER_STABLE,
                        },
                        {
                            "id": "ver_2",
                            "version_number": 2,
                            "content": "当前 Prompt",
                            "status": promptbox.VER_DRAFT,
                        },
                    ],
                }
            ],
            "categories": [{"id": "cat_1", "name": "分类", "children": []}],
            "tags": [{"id": "tag_1", "name": "标签"}],
        }
    )
    app.snippets = app.data["snippets"]

    workbench = app._create_repair_workbench(app.snippets[0])
    workbench.capture_case(
        "snip_1", "ver_2", 2, "当前 Prompt", "坏输出", "同一输入", "提取待办"
    )
    workbench.generate_candidate()
    case = workbench.record_verification("同一输入", "输出完整", 5, passed=True)

    snippet = app.snippets[0]
    new_version = snippet["versions"][-1]
    assert len(snippet["versions"]) == 3
    assert new_version["version_number"] == 3
    assert new_version["parent_version_id"] == "ver_2"
    assert new_version["repair_case_id"] == case["id"]
    assert snippet["current_version_id"] == new_version["id"]
    assert snippet["stable_version_id"] == "ver_1"
    assert case["adopted_version_id"] == new_version["id"]
    assert app.data["repair_cases"] == [case]

    with pytest.raises(ValueError, match="already adopted"):
        workbench.record_verification("同一输入", "重复保存", 5, passed=True)

    saved = load_data(path)
    saved_case = saved["repair_cases"][0]
    saved_version = saved["snippets"][0]["versions"][-1]
    assert saved_case["adopted_version_id"] == saved_version["id"]
    assert saved_version["repair_case_id"] == saved_case["id"]


    promptbox = _load_promptbox_module()

    assert Path(promptbox.SNIPPETS_FILE).parent.name == ".promptbox"
    assert Path(promptbox.SNIPPETS_FILE).name == "snippets.json"


def test_data_directory_can_be_overridden_for_safe_gui_verification(tmp_path, monkeypatch):
    isolated_data_dir = tmp_path / "promptbox-data"
    monkeypatch.setenv("PROMPTBOX_DATA_DIR", str(isolated_data_dir))

    promptbox = _load_promptbox_module()

    assert Path(promptbox.DATA_DIR) == isolated_data_dir
    assert Path(promptbox.SNIPPETS_FILE) == isolated_data_dir / "snippets.json"


def test_load_migrates_legacy_repository_data_to_user_directory(tmp_path, monkeypatch):
    promptbox = _load_promptbox_module()
    legacy_path = tmp_path / "legacy-snippets.json"
    target_path = tmp_path / ".promptbox" / "snippets.json"
    legacy_path.write_text(
        '{"version": 3, "snippets": [{"id": "s1", "content": "old"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(promptbox, "SNIPPETS_FILE", str(target_path))
    monkeypatch.setattr(promptbox, "LEGACY_SNIPPETS_FILE", str(legacy_path))

    migrated = promptbox.load_prompt_data()

    assert target_path.exists()
    assert migrated["snippets"][0]["id"] == "s1"
    assert migrated["version"] == 4


def test_normalization_preserves_validated_repair_case_collection():
    promptbox = _load_promptbox_module()
    raw = {"version": 3, "snippets": [], "repair_cases": [{"id": "case_1"}]}

    data = promptbox.normalize_prompt_data(raw)

    assert data["version"] == 4
    assert data["repair_cases"] == [{"id": "case_1"}]
    assert data["preferences"] == {}


def test_promptbox_without_configured_transport_explains_how_to_enable_repair():
    promptbox = _load_promptbox_module()
    app = promptbox.PromptBox()

    with __import__("pytest").raises(ValueError, match="PROMPTBOX_REPAIR_API_BASE"):
        app.repair_service.repair("旧 Prompt", "坏输出", "输入")


def test_promptbox_keeps_legacy_snippets_and_runs_when_saving_repair_case(tmp_path):
    promptbox = _load_promptbox_module()
    path = tmp_path / "snippets.json"
    promptbox.SNIPPETS_FILE = str(path)
    legacy = {
        "version": 3,
        "snippets": [{"id": "s1", "versions": [{"id": "v1", "content": "old"}]}],
        "categories": [{"id": "cat_1", "name": "分类", "children": []}],
        "tags": [{"id": "tag_1", "name": "标签"}],
        "runs": [{"id": "run_1"}],
    }
    save_data(path, legacy)
    app = promptbox.PromptBox(repair_transport=fake_transport)
    app.data = promptbox.load_prompt_data()
    app.snippets = app.data["snippets"]
    workbench = app._create_repair_workbench(app.snippets[0])
    workbench.capture_case("s1", "v1", 1, "旧", "坏", "输入")
    workbench.generate_candidate()
    workbench.record_verification("输入", "输出完整", 5, passed=True)

    saved = load_data(path)
    assert saved["snippets"][0]["versions"][0]["content"] == "old"
    assert saved["categories"][0]["id"] == "cat_1"
    assert saved["tags"][0]["id"] == "tag_1"
    assert saved["runs"][0]["id"] == "run_1"


def test_promptbox_saves_validated_repair_case_through_its_data_envelope(tmp_path):
    promptbox = _load_promptbox_module()
    path = tmp_path / "snippets.json"
    promptbox.SNIPPETS_FILE = str(path)
    app = promptbox.PromptBox(repair_transport=fake_transport)
    app.data = promptbox.normalize_prompt_data(
        {
            "version": 4,
            "snippets": [
                {
                    "id": "s1",
                    "title": "测试 Prompt",
                    "category_id": "cat_1",
                    "tag_ids": [],
                    "current_version_id": "v1",
                    "stable_version_id": "v1",
                    "versions": [{"id": "v1", "version_number": 1, "content": "旧 Prompt"}],
                }
            ],
        }
    )
    app.snippets = app.data["snippets"]

    workbench = app._create_repair_workbench(app.snippets[0])
    workbench.capture_case("s1", "v1", 1, "旧 Prompt", "坏输出", "同一输入")
    workbench.generate_candidate()
    workbench.record_verification("同一输入", "输出完整", 5, passed=True)

    restarted = load_data(path)
    assert restarted["repair_cases"][0]["status"] == "validated"
    assert restarted["repair_cases"][0]["adopted_version_id"] == restarted["snippets"][0]["current_version_id"]


def test_audit_warnings_and_errors_do_not_block_verified_repair_version_creation(tmp_path):
    promptbox = _load_promptbox_module()
    path = tmp_path / "snippets.json"
    promptbox.SNIPPETS_FILE = str(path)
    app = promptbox.PromptBox(repair_transport=audit_warning_error_transport)
    app.data = promptbox.normalize_prompt_data(
        {
            "version": 4,
            "snippets": [
                {
                    "id": "s1",
                    "title": "会议总结",
                    "category_id": "cat_1",
                    "tag_ids": [],
                    "current_version_id": "v1",
                    "stable_version_id": "v1",
                    "versions": [
                        {
                            "id": "v1",
                            "version_number": 1,
                            "content": "请总结会议。仅依据原文；补充背景知识。",
                        }
                    ],
                }
            ],
        }
    )
    app.snippets = app.data["snippets"]
    workbench = app._create_repair_workbench(app.snippets[0])
    workbench.capture_case(
        "s1",
        "v1",
        1,
        "请总结会议。仅依据原文；补充背景知识。",
        "坏输出",
        "输入",
    )
    workbench.generate_candidate()

    assert {issue["code"] for issue in workbench.get_view()["audit"]["issues"]} >= {
        "constraint_conflict",
        "missing_output_contract",
    }
    case = workbench.record_verification("输入", "候选通过", 5, passed=True)
    assert case["status"] == "validated"
    assert len(app.snippets[0]["versions"]) == 2
    assert app.snippets[0]["versions"][-1]["repair_case_id"] == case["id"]


def test_structural_audit_warnings_do_not_block_verified_repair_version_creation(tmp_path):
    promptbox = _load_promptbox_module()
    path = tmp_path / "snippets.json"
    promptbox.SNIPPETS_FILE = str(path)
    app = promptbox.PromptBox(repair_transport=structural_warning_transport)
    app.data = promptbox.normalize_prompt_data(
        {
            "version": 4,
            "snippets": [
                {
                    "id": "s1",
                    "title": "会议总结",
                    "category_id": "cat_1",
                    "tag_ids": [],
                    "current_version_id": "v1",
                    "stable_version_id": "v1",
                    "versions": [
                        {
                            "id": "v1",
                            "version_number": 1,
                            "content": "请总结以下内容。",
                        }
                    ],
                }
            ],
        }
    )
    app.snippets = app.data["snippets"]
    workbench = app._create_repair_workbench(app.snippets[0])
    workbench.capture_case("s1", "v1", 1, "请总结以下内容。", "坏输出", "输入")
    workbench.generate_candidate()

    codes = {issue["code"] for issue in workbench.get_view()["audit"]["issues"]}
    assert {"missing_input_boundary", "missing_output_contract"} <= codes

    case = workbench.record_verification("输入", "候选通过", 5, passed=True)
    new_version = app.snippets[0]["versions"][-1]
    assert case["status"] == "validated"
    assert new_version["repair_case_id"] == case["id"]
    assert case["adopted_version_id"] == new_version["id"]


def test_constraint_executability_issues_do_not_block_verified_repair_version_creation(tmp_path):
    promptbox = _load_promptbox_module()
    path = tmp_path / "snippets.json"
    promptbox.SNIPPETS_FILE = str(path)
    app = promptbox.PromptBox(repair_transport=constraint_executability_transport)
    app.data = promptbox.normalize_prompt_data(
        {
            "version": 4,
            "snippets": [
                {
                    "id": "s1",
                    "title": "会议总结",
                    "category_id": "cat_1",
                    "tag_ids": [],
                    "current_version_id": "v1",
                    "stable_version_id": "v1",
                    "versions": [
                        {
                            "id": "v1",
                            "version_number": 1,
                            "content": "请总结会议记录；必须保留原文；禁止保留原文；尽量简洁。",
                        }
                    ],
                }
            ],
        }
    )
    app.snippets = app.data["snippets"]
    workbench = app._create_repair_workbench(app.snippets[0])
    workbench.capture_case(
        "s1",
        "v1",
        1,
        "请总结会议记录；必须保留原文；禁止保留原文；尽量简洁。",
        "坏输出",
        "输入",
    )
    workbench.generate_candidate()

    codes = {issue["code"] for issue in workbench.get_view()["audit"]["issues"]}
    assert {"direct_constraint_conflict", "vague_constraint"} <= codes

    case = workbench.record_verification("输入", "候选通过", 5, passed=True)
    new_version = app.snippets[0]["versions"][-1]
    assert case["status"] == "validated"
    assert new_version["repair_case_id"] == case["id"]
    assert case["adopted_version_id"] == new_version["id"]


def test_unresolved_issue_declaration_does_not_block_verified_repair_version_creation(tmp_path):
    promptbox = _load_promptbox_module()
    path = tmp_path / "snippets.json"
    promptbox.SNIPPETS_FILE = str(path)
    app = promptbox.PromptBox(repair_transport=unresolved_constraint_transport)
    app.data = promptbox.normalize_prompt_data(
        {
            "version": 4,
            "snippets": [
                {
                    "id": "s1",
                    "title": "会议总结",
                    "category_id": "cat_1",
                    "tag_ids": [],
                    "current_version_id": "v1",
                    "stable_version_id": "v1",
                    "versions": [
                        {
                            "id": "v1",
                            "version_number": 1,
                            "content": "请总结会议记录。仅依据原文；尽量简洁；输出三条要点。",
                        }
                    ],
                }
            ],
        }
    )
    app.snippets = app.data["snippets"]
    workbench = app._create_repair_workbench(app.snippets[0])
    workbench.capture_case(
        "s1",
        "v1",
        1,
        "请总结会议记录。仅依据原文；尽量简洁；输出三条要点。",
        "坏输出",
        "输入",
    )
    workbench.generate_candidate()

    assert workbench.get_view()["mode"] == "B"
    case = workbench.record_verification("输入", "候选通过", 5, passed=True)
    new_version = app.snippets[0]["versions"][-1]
    assert case["status"] == "validated"
    assert new_version["repair_case_id"] == case["id"]
    assert case["adopted_version_id"] == new_version["id"]



    path = tmp_path / "snippets.json"
    data = load_data(path)
    saved = []

    def save_case(case):
        saved[:] = [case]
        data["repair_cases"] = saved
        save_data(path, data)

    workbench = RepairWorkbench(RepairService(fake_transport), save_case=save_case)
    workbench.capture_case("s1", "v1", 1, "旧 Prompt", "坏输出", "同一输入", "完成任务")
    workbench.generate_candidate()
    workbench.record_verification("同一输入", "结果仍缺字段", 2, passed=False)

    restarted = load_data(path)
    saved_case = restarted["repair_cases"][0]
    assert saved_case["status"] == "validation_failed"
    assert saved_case["adopted_version_id"] is None
    assert saved_case["failure"]["prompt"] == "旧 Prompt"
    assert saved_case["candidates"][0]["content"] == "修复后的 Prompt"


def test_category_selection_can_start_unselected_and_invalid_selection_is_cleared():
    promptbox = _load_promptbox_module()
    app = promptbox.PromptBox()
    app.data = {
        "categories": [{"id": "cat_work", "name": "工作"}],
        "snippets": [],
    }
    app.active_category_id = None
    app._restore_category_selection()
    assert app.active_category_id is None

    app.active_category_id = "cat_missing"
    app._restore_category_selection()
    assert app.active_category_id is None


def test_new_prompt_action_delegates_to_editor_while_category_is_selected():
    promptbox = _load_promptbox_module()
    app = promptbox.PromptBox()
    app.active_category_id = "cat_work"
    opened = []
    app._editor = lambda snippet: opened.append((snippet, app.active_category_id))

    app._add()

    assert opened == [(None, "cat_work")]


def test_deselect_category_returns_to_all_prompts_view():
    promptbox = _load_promptbox_module()
    app = promptbox.PromptBox()
    app.active_category_id = "cat_work"
    app._clear_frames = lambda: None
    app._filter = lambda: setattr(app, "filtered", ["all-prompts"])

    app._deselect_category()

    assert app.active_category_id is None
    assert app.filtered == ["all-prompts"]


def test_all_prompts_action_is_exposed_for_clearing_category_filter():
    promptbox = _load_promptbox_module()
    app = promptbox.PromptBox()

    assert callable(app._deselect_category)
    assert app._category_clear_label() == "查看全部"


def test_three_redacted_prompts_complete_repair_compare_and_manual_decision_flow(tmp_path):
    promptbox = _load_promptbox_module()
    path = tmp_path / "snippets.json"
    promptbox.SNIPPETS_FILE = str(path)
    decisions = []

    def transport(messages):
        if "原始提示词:" in messages[1]["content"]:
            source = messages[1]["content"]
            if "输出 3 个开头" in source:
                candidate = "请输出 3 个开头，主题是 {主题}，风格是 {风格}；每个不超过 80 字。"
            elif "代码评审" in source:
                candidate = "请做代码评审，按严重程度分级输出问题和可执行修改建议。"
            else:
                candidate = "请润色原文，保留原意和口语感；删除空泛表达后输出修改稿。"
            return {
                "diagnosis": "输出要求不够明确",
                "mode": "B",
                "candidate": candidate,
                "reasons": ["补充可检查的输出要求"],
            }
        return "脱敏验证输出：结果可供人工比较。"

    raw = {
        "version": 4,
        "snippets": [
            {
                "id": f"s{i}",
                "title": title,
                "category_id": "cat_1",
                "tag_ids": [],
                "current_version_id": f"v{i}",
                "stable_version_id": f"v{i}",
                "versions": [{"id": f"v{i}", "version_number": 1, "content": content}],
            }
            for i, (title, content) in enumerate([
                ("博客开头", "输出 3 个开头，主题是 {主题}，风格是 {风格}。"),
                ("代码评审", "请做代码评审，按严重程度输出修改建议。"),
                ("文字润色", "请润色原文，保留原意，避免空泛表达。"),
            ], start=1)
        ],
        "categories": [{"id": "cat_1", "name": "分类", "children": []}],
    }
    app = promptbox.PromptBox(repair_transport=transport)
    app.data = promptbox.normalize_prompt_data(raw)
    app.snippets = app.data["snippets"]

    for index, snippet in enumerate(app.snippets, start=1):
        workbench = app._create_repair_workbench(snippet)
        workbench.capture_case(snippet["id"], snippet["current_version_id"], 1, snippet["versions"][0]["content"], "失败输出", "真实输入")
        workbench.generate_candidate()
        sample = workbench.add_pairwise_case(
            context_label=f"脱敏案例{index}",
            context_text=f"脱敏业务上下文{index}",
            user_input=f"脱敏任务输入{index}",
            source_type="designed_boundary",
            source_label="脱敏产品点验",
            user_confirmed=True,
        )
        run = workbench.run_pairwise_case(sample["id"])
        workbench.set_pairwise_case_verdict(sample["id"], "candidate_better" if index != 2 else "baseline_better", "人工比较记录")
        case = workbench.record_pairwise_cases(
            overall_conclusion="candidate_better" if index != 2 else "baseline_better",
            passed=index != 2,
            summary_note="候选采纳" if index != 2 else "保留基线",
        )
        decisions.append((run["id"], case["status"], case["verification"]["overall_conclusion"]))

    assert decisions == [
        ("run_1", "validated", "candidate_better"),
        ("run_1", "validation_failed", "baseline_better"),
        ("run_1", "validated", "candidate_better"),
    ]


def test_verification_browser_uses_flattened_records_and_filters():
    promptbox = _load_promptbox_module()
    app = promptbox.PromptBox()
    app.data = {"snippets": [], "repair_cases": []}
    app._verification_records = lambda **filters: [{"id": "run_1", "verdict": "candidate_better", **filters}]

    records = app._get_verification_records(verdict="candidate_better")

    assert records[0]["id"] == "run_1"
    assert records[0]["verdict"] == "candidate_better"


def test_verification_browser_button_is_configured_in_window_footer():
    promptbox = _load_promptbox_module()
    app = promptbox.PromptBox()
    created = []
    app._btn = lambda parent, label, command, *args, **kwargs: created.append((label, command)) or types.SimpleNamespace(pack=lambda **pack_kwargs: None)
    class Label:
        def pack(self, **pack_kwargs):
            return None
    promptbox.tk.Label = lambda *args, **kwargs: Label()

    app._build_footer_actions(types.SimpleNamespace())

    labels = [label for label, _ in created]
    assert "验证记录" in labels
