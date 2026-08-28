from promptbox_mvp.workbench import RepairWorkbench

from test_promptbox_workbench import FakeVerifyService, capture_bound_case


def test_pairwise_run_keeps_context_pack_manifest_budget_and_hash():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "修复后 Prompt"}}
    pack = {
        "text": "[文件：README.md]\n说明",
        "manifest": {"rules_version": "context-rules-v1", "entries": []},
        "budget": {"capacity_status": "unknown", "estimated_tokens": 10},
        "actions": [],
    }

    result = workbench.run_pairwise_verify(
        "基线", "候选", pack["text"], "输入", context_pack=pack
    )

    assert result["context_pack"]["budget"]["capacity_status"] == "unknown"
    assert result["context_pack"]["manifest"]["rules_version"] == "context-rules-v1"
    assert result["context_hash"].startswith("sha256:")
    assert result["baseline"]["context_hash"] == result["candidate"]["context_hash"]


def test_unknown_model_capacity_does_not_raise_before_transport():
    workbench = RepairWorkbench(FakeVerifyService())
    capture_bound_case(workbench)
    workbench.analysis = {"candidate": {"content": "候选"}}

    result = workbench.run_verify("候选", "上下文", "输入", model_context_window=None)

    assert result["budget"]["capacity_status"] == "unknown"
    assert result["budget"]["can_send"] is True
