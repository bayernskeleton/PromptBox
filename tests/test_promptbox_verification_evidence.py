import pytest

from promptbox_mvp.contract import create_candidate, create_repair_case, record_pairwise_verification
from promptbox_mvp.verification_evidence import VerificationEvidenceError, get_version_evidence


def make_data_with_validated_version():
    case = create_repair_case("snip_1", "ver_2", 2, "基线 Prompt", "失败输出", "输入")
    create_candidate(case, "候选 Prompt", ["补充输出契约"])
    record_pairwise_verification(
        case,
        runs=[{
            "id": "run_1",
            "source_type": "real_business_replay",
            "source_label": "历史会议纪要",
            "context_scope": "第 2 节",
            "source_chars": 1000,
            "context_chars": 1000,
            "context_text": "上下文",
            "context_hash": "sha256:abc",
            "truncated": False,
            "user_confirmed": True,
            "captured_at": "2026-08-19T10:00:00Z",
            "user_input": "提取待办",
            "baseline_output": "基线输出",
            "candidate_output": "候选输出",
            "baseline_latency_ms": 100,
            "candidate_latency_ms": 90,
            "verdict": "candidate_better",
            "note": "候选更清晰",
        }],
        overall_conclusion="candidate_better",
        passed=True,
        summary_note="候选更优",
    )
    version = {
        "id": "ver_3",
        "version_number": 3,
        "content": "候选 Prompt",
        "parent_version_id": "ver_2",
        "repair_case_id": case["id"],
    }
    return {
        "snippets": [{
            "id": "snip_1",
            "title": "待办提取",
            "versions": [
                {"id": "ver_2", "version_number": 2, "content": "基线 Prompt"},
                version,
            ],
        }],
        "repair_cases": [case],
    }


def test_get_version_evidence_returns_read_only_view_with_provenance():
    data = make_data_with_validated_version()

    view = get_version_evidence(data, "snip_1", "ver_3")

    assert view["version"]["id"] == "ver_3"
    assert view["baseline_version"]["id"] == "ver_2"
    assert view["verification"]["overall_conclusion"] == "candidate_better"
    assert view["source_counts"] == {
        "real_business_replay": 1,
        "historical_failure": 0,
        "designed_boundary": 0,
        "ai_synthetic": 0,
    }
    assert view["missing_provenance_fields"] == []
    view["verification"]["pairwise_runs"][0]["user_input"] = "外部修改"
    assert data["repair_cases"][0]["verification"]["pairwise_runs"][0]["user_input"] != "外部修改"


def test_get_version_evidence_rejects_missing_baseline_version():
    data = make_data_with_validated_version()
    data["snippets"][0]["versions"] = [data["snippets"][0]["versions"][1]]

    with pytest.raises(VerificationEvidenceError, match="基线版本缺失"):
        get_version_evidence(data, "snip_1", "ver_3")


def test_get_version_evidence_rejects_missing_candidate():
    data = make_data_with_validated_version()
    data["repair_cases"][0]["candidates"] = []

    with pytest.raises(VerificationEvidenceError, match="候选版本缺失"):
        get_version_evidence(data, "snip_1", "ver_3")


def test_get_version_evidence_preserves_diagnostic_summary_when_recorded():
    data = make_data_with_validated_version()
    data["repair_cases"][0]["analysis"] = {
        "diagnosis": "缺少输出契约",
        "mode": "B",
    }

    view = get_version_evidence(data, "snip_1", "ver_3")

    assert view["analysis"] == {
        "diagnosis": "缺少输出契约",
        "mode": "B",
    }


    data = {
        "snippets": [{"id": "snip_1", "versions": [{"id": "ver_2", "repair_case_id": "case_missing"}]}],
        "repair_cases": [],
    }

    with pytest.raises(VerificationEvidenceError, match="验证记录缺失"):
        get_version_evidence(data, "snip_1", "ver_2")


def test_get_version_evidence_reports_legacy_provenance_gaps():
    data = make_data_with_validated_version()
    run = data["repair_cases"][0]["verification"]["pairwise_runs"][0]
    for field in ("source_type", "context_hash", "user_confirmed"):
        run.pop(field)

    view = get_version_evidence(data, "snip_1", "ver_3")

    assert "source_type" in view["missing_provenance_fields"]
    assert "context_hash" in view["missing_provenance_fields"]
    assert "user_confirmed" in view["missing_provenance_fields"]


def test_list_verification_records_flattens_runs_and_filters_without_mutating_data():
    from promptbox_mvp.verification_evidence import list_verification_records

    data = make_data_with_validated_version()
    case = data["repair_cases"][0]
    case["verification"]["pairwise_runs"][0]["captured_at"] = "2026-08-19T10:00:00Z"
    case["verification"]["pairwise_runs"][0]["context_label"] = "会议"
    case["verification"]["pairwise_runs"][0]["verdict"] = "candidate_better"

    records = list_verification_records(data)

    assert len(records) == 1
    assert records[0]["repair_case_id"] == case["id"]
    assert records[0]["snippet_id"] == "snip_1"
    assert records[0]["version_id"] == "ver_3"
    assert records[0]["verdict"] == "candidate_better"
    assert records[0]["context_label"] == "会议"
    assert list_verification_records(data, verdict="baseline_better") == []
    assert len(list_verification_records(data, context_label="会议")) == 1

    records[0]["candidate_output"] = "外部修改"
    assert data["repair_cases"][0]["verification"]["pairwise_runs"][0]["candidate_output"] == "候选输出"
