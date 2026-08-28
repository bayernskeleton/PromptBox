import json
from datetime import datetime

import pytest

from promptbox import get_prompt_version_content, normalize_prompt_data
from promptbox_mvp.contract import (
    create_candidate,
    create_repair_case,
    record_verdict,
    record_verification,
    record_pairwise_verification,
)


def test_normalize_prompt_data_adds_favorite_and_keeps_current_version_for_quick_copy():
    data = normalize_prompt_data({
        "snippets": [{
            "id": "snip_1",
            "title": "迭代中的 Prompt",
            "content": "旧兼容正文",
            "current_version_id": "v2",
            "stable_version_id": "v1",
            "versions": [
                {"id": "v1", "version_number": 1, "content": "稳定版"},
                {"id": "v2", "version_number": 2, "content": "当前版"},
            ],
        }],
        "runs": [{"id": "run_old"}],
    })
    snippet = data["snippets"][0]
    assert snippet["is_favorite"] is False
    assert data["runs"] == [{"id": "run_old"}]
    assert get_prompt_version_content(snippet) == "当前版"
    assert get_prompt_version_content(snippet, "v1") == "稳定版"


def make_case():
    return create_repair_case(
        snippet_id="snip_email",
        base_version_id="ver_2",
        base_version_number=2,
        prompt="原 Prompt v2",
        output="失败输出",
        comparison_input="测试输入",
        task_goal="输出三条待办",
    )


def assert_utc_timestamp(value):
    assert value.endswith("Z")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_create_repair_case_binds_a_real_prompt_version_and_json_serializes():
    case = make_case()

    assert set(case) == {
        "id",
        "status",
        "created_at",
        "snippet_id",
        "task_goal",
        "base_version_id",
        "base_version_number",
        "failure",
        "comparison",
        "candidates",
        "verification",
        "adopted_version_id",
        "verdict",
    }
    assert case["id"].startswith("case_")
    assert case["snippet_id"] == "snip_email"
    assert case["base_version_id"] == "ver_2"
    assert case["base_version_number"] == 2
    assert case["failure"] == {"prompt": "原 Prompt v2", "output": "失败输出"}
    assert case["comparison"] == {"input": "测试输入"}
    assert case["status"] == "captured"
    assert case["candidates"] == []
    assert case["verification"] == {
        "status": "pending",
        "input": "",
        "output_note": "",
        "rating": None,
        "verified_at": None,
    }
    assert case["adopted_version_id"] is None
    assert case["verdict"] is None
    assert_utc_timestamp(case["created_at"])
    json.dumps(case)


def test_case_ids_are_unique_and_keep_prefixes():
    first = make_case()
    second = make_case()

    assert first["id"] != second["id"]
    assert first["id"].startswith("case_")
    assert second["id"].startswith("case_")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("snippet_id", ""),
        ("base_version_id", ""),
        ("base_version_number", 0),
        ("base_version_number", "2"),
    ],
)
def test_create_repair_case_rejects_invalid_real_baseline(field, value):
    values = {
        "snippet_id": "snip_email",
        "base_version_id": "ver_2",
        "base_version_number": 2,
        "prompt": "prompt",
        "output": "output",
        "comparison_input": "input",
        "task_goal": "goal",
    }
    values[field] = value

    with pytest.raises(ValueError):
        create_repair_case(**values)


@pytest.mark.parametrize("field", ["prompt", "output", "comparison_input", "task_goal"])
def test_create_repair_case_rejects_non_string_text(field):
    values = {
        "snippet_id": "snip_email",
        "base_version_id": "ver_2",
        "base_version_number": 2,
        "prompt": "prompt",
        "output": "output",
        "comparison_input": "input",
        "task_goal": "goal",
    }
    values[field] = None

    with pytest.raises(ValueError):
        create_repair_case(**values)


def test_create_candidate_has_complete_fields_and_sets_status():
    case = make_case()
    candidate = create_candidate(case, "candidate", ["reason"])

    assert set(candidate) == {
        "id",
        "parent_version_id",
        "content",
        "change_reasons",
        "created_at",
    }
    assert candidate["id"].startswith("candidate_")
    assert candidate["parent_version_id"] == case["base_version_id"]
    assert candidate["content"] == "candidate"
    assert candidate["change_reasons"] == ["reason"]
    assert_utc_timestamp(candidate["created_at"])
    assert case["status"] == "candidate_ready"
    assert case["candidates"] == [candidate]
    json.dumps(case)


def test_create_candidate_rejects_invalid_case_content_and_reasons():
    with pytest.raises(ValueError):
        create_candidate([], "candidate", [])
    with pytest.raises(ValueError):
        create_candidate({"status": "captured"}, "candidate", [])
    with pytest.raises(ValueError):
        create_candidate(make_case(), 1, [])
    with pytest.raises(ValueError):
        create_candidate(make_case(), "candidate", "reason")
    with pytest.raises(ValueError):
        create_candidate(make_case(), "candidate", ["ok", 1])


def test_record_failed_verification_keeps_case_without_adopted_version():
    case = make_case()
    create_candidate(case, "修复版", ["补足输出格式"])

    result = record_verification(
        case,
        input_text="验证输入",
        output_note="仍缺少第二条待办",
        rating=2,
        passed=False,
    )

    assert result["status"] == "failed"
    assert result["rating"] == 2
    assert_utc_timestamp(result["verified_at"])
    assert case["status"] == "validation_failed"
    assert case["adopted_version_id"] is None


def test_record_passing_verification_moves_case_to_validation_pending():
    case = make_case()
    create_candidate(case, "修复版", ["补足输出格式"])

    result = record_verification(
        case,
        input_text="验证输入",
        output_note="输出完整",
        rating=5,
        passed=True,
    )

    assert result["status"] == "passed"
    assert case["status"] == "validation_pending"
    assert case["adopted_version_id"] is None


@pytest.mark.parametrize("rating", [0, 6, "5"])
def test_record_verification_rejects_invalid_rating(rating):
    case = make_case()
    create_candidate(case, "修复版", ["补足输出格式"])

    with pytest.raises(ValueError):
        record_verification(case, "输入", "说明", rating, True)


def test_record_verification_requires_a_ready_candidate():
    case = make_case()

    with pytest.raises(ValueError):
        record_verification(case, "输入", "说明", None, True)


@pytest.mark.parametrize(
    ("choice", "status"),
    [("adopt", "validation_pending"), ("edit", "editing"), ("reject", "discarded")],
)
def test_record_verdict_keeps_compatibility_statuses(choice, status):
    case = make_case()
    candidate = create_candidate(case, "candidate", ["reason"])

    verdict = record_verdict(case, candidate["id"], choice, "review note")

    assert verdict["candidate_id"] == candidate["id"]
    assert verdict["choice"] == choice
    assert verdict["note"] == "review note"
    assert_utc_timestamp(verdict["created_at"])
    assert case["status"] == status
    assert case["verdict"] == verdict


def test_contract_validation_rejects_malformed_verification():
    case = make_case()
    case["verification"] = {"status": "pending"}

    with pytest.raises(ValueError):
        create_candidate(case, "candidate", [])


def test_record_pairwise_verification_stores_pairwise_runs_and_verdicts():
    case = make_case()
    candidate = create_candidate(case, "候选 Prompt", ["补足输出约束"])

    runs = [
        {
            "id": "case_1",
            "source_type": "designed_boundary",
            "source_label": "契约测试样本",
            "user_confirmed": True,
            "context_label": "案例1",
            "context_text": "上下文背景1",
            "user_input": "测试输入1",
            "baseline_output": "基线输出1",
            "candidate_output": "候选输出1",
            "baseline_latency_ms": 120,
            "candidate_latency_ms": 95,
            "verdict": "candidate_better",
            "note": "候选输出格式更符合要求",
        },
        {
            "id": "case_2",
            "source_type": "designed_boundary",
            "source_label": "契约测试样本",
            "user_confirmed": True,
            "context_label": "案例2",
            "context_text": "",
            "user_input": "测试输入2",
            "baseline_output": "基线输出2",
            "candidate_output": "候选输出2",
            "baseline_latency_ms": 110,
            "candidate_latency_ms": 115,
            "verdict": "equal",
            "note": "两者均符合要求",
        },
    ]

    verification = record_pairwise_verification(
        case=case,
        runs=runs,
        overall_conclusion="candidate_better",
        passed=True,
        summary_note="2组测试中1组更优1组持平，建议采纳",
    )

    assert verification["status"] == "passed"
    assert verification["overall_conclusion"] == "candidate_better"
    assert len(verification["pairwise_runs"]) == 2
    assert verification["pairwise_runs"][0]["verdict"] == "candidate_better"
    assert verification["pairwise_runs"][1]["verdict"] == "equal"
    assert verification["summary_note"] == "2组测试中1组更优1组持平，建议采纳"
    assert_utc_timestamp(verification["verified_at"])
    assert case["status"] == "validation_pending"
    assert case["verification"] == verification
    json.dumps(case)


def test_record_pairwise_verification_rejects_invalid_inputs():
    case = make_case()
    candidate = create_candidate(case, "候选 Prompt", ["修复"])

    # 1. runs 数量超过上限（最大支持 5 组）或为空
    with pytest.raises(ValueError):
        record_pairwise_verification(case, runs=[], overall_conclusion="equal", passed=True)

    too_many_runs = [{"user_input": f"in_{i}", "verdict": "equal"} for i in range(6)]
    with pytest.raises(ValueError):
        record_pairwise_verification(case, runs=too_many_runs, overall_conclusion="equal", passed=True)

    # 2. verdict 非法
    invalid_verdict_runs = [
        {"user_input": "in_1", "verdict": "auto_win"}
    ]
    with pytest.raises(ValueError):
        record_pairwise_verification(case, runs=invalid_verdict_runs, overall_conclusion="equal", passed=True)

    # 3. 未就绪状态
    fresh_case = make_case()
    valid_runs = [{"user_input": "in_1", "verdict": "candidate_better"}]
    with pytest.raises(ValueError):
        record_pairwise_verification(fresh_case, runs=valid_runs, overall_conclusion="candidate_better", passed=True)


def test_pairwise_run_preserves_source_and_context_snapshot_metadata():
    case = make_case()
    create_candidate(case, "候选 Prompt", ["修复"])

    verification = record_pairwise_verification(
        case,
        runs=[
            {
                "id": "run_1",
                "source_type": "real_business_replay",
                "source_label": "2026-08-18 教研会议纪要",
                "context_scope": "第 2-5 节",
                "source_chars": 38_000,
                "context_chars": 8_600,
                "context_text": "脱敏后的实际上下文",
                "context_hash": "sha256:abc",
                "truncated": False,
                "user_confirmed": True,
                "captured_at": "2026-08-18T10:00:00Z",
                "user_input": "提取未完成事项",
                "verdict": "candidate_better",
            }
        ],
        overall_conclusion="candidate_better",
        passed=True,
    )

    run = verification["pairwise_runs"][0]
    assert run["source_type"] == "real_business_replay"
    assert run["source_label"] == "2026-08-18 教研会议纪要"
    assert run["source_chars"] == 38_000
    assert run["context_chars"] == 8_600
    assert run["context_hash"] == "sha256:abc"
    assert run["truncated"] is False
    assert run["user_confirmed"] is True


def test_pairwise_run_rejects_unknown_source_type():
    case = make_case()
    create_candidate(case, "候选 Prompt", ["修复"])

    with pytest.raises(ValueError, match="invalid pairwise source type"):
        record_pairwise_verification(
            case,
            runs=[{"source_type": "unknown", "verdict": "equal"}],
            overall_conclusion="equal",
            passed=False,
        )


def test_ai_synthetic_run_cannot_be_the_only_formal_adoption_evidence():
    case = make_case()
    create_candidate(case, "候选 Prompt", ["修复"])

    with pytest.raises(ValueError, match="AI 合成样本不能单独作为正式采纳证据"):
        record_pairwise_verification(
            case,
            runs=[
                {
                    "source_type": "ai_synthetic",
                    "user_input": "测试",
                    "verdict": "candidate_better",
                    "user_confirmed": False,
                }
            ],
            overall_conclusion="candidate_better",
            passed=True,
        )

