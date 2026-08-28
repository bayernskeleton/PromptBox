from datetime import datetime, timezone
from uuid import uuid4


_REQUIRED_CASE_FIELDS = {
    "status",
    "snippet_id",
    "base_version_id",
    "base_version_number",
    "candidates",
    "verification",
    "adopted_version_id",
    "verdict",
}
_CASE_STATUSES = {
    "captured",
    "candidate_ready",
    "editing",
    "validation_pending",
    "validated",
    "validation_failed",
    "discarded",
}
_CANDIDATE_STATUSES = {"captured", "candidate_ready"}
_VERDICT_STATUSES = {"candidate_ready"}
_CHOICES = {"adopt", "edit", "reject"}
_VERIFICATION_STATUSES = {"pending", "passed", "failed"}
_PAIRWISE_VERDICTS = {"candidate_better", "baseline_better", "equal", "undecided"}
_PAIRWISE_OVERALL_CONCLUSIONS = {"candidate_better", "baseline_better", "equal", "undecided"}
_PAIRWISE_SOURCE_TYPES = {
    "real_business_replay",
    "historical_failure",
    "designed_boundary",
    "ai_synthetic",
    "legacy_unknown",
}


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _id(prefix):
    return prefix + uuid4().hex


def _validate_verification(verification):
    if not isinstance(verification, dict):
        raise ValueError("invalid verification")
    if verification.get("status") not in _VERIFICATION_STATUSES:
        raise ValueError("invalid verification status")
    for field in ("input", "output_note"):
        if not isinstance(verification.get(field), str):
            raise ValueError("invalid verification text")
    rating = verification.get("rating")
    if rating is not None and (not isinstance(rating, int) or not 1 <= rating <= 5):
        raise ValueError("invalid verification rating")
    pairwise_runs = verification.get("pairwise_runs")
    if pairwise_runs is not None:
        if not isinstance(pairwise_runs, list) or not (1 <= len(pairwise_runs) <= 5):
            raise ValueError("invalid pairwise runs")
        for run in pairwise_runs:
            if not isinstance(run, dict):
                raise ValueError("invalid pairwise run")
            if run.get("verdict") is not None and run.get("verdict") not in _PAIRWISE_VERDICTS:
                raise ValueError("invalid pairwise verdict")
    overall_conclusion = verification.get("overall_conclusion")
    if overall_conclusion is not None and overall_conclusion not in _PAIRWISE_OVERALL_CONCLUSIONS:
        raise ValueError("invalid overall conclusion")
    verified_at = verification.get("verified_at")
    if verified_at is not None and not isinstance(verified_at, str):
        raise ValueError("invalid verification timestamp")
    if verification["status"] == "pending" and verified_at is not None:
        raise ValueError("pending verification cannot have a timestamp")
    if verification["status"] != "pending" and not isinstance(verified_at, str):
        raise ValueError("completed verification needs a timestamp")


def _validate_case(case):
    if not isinstance(case, dict) or not _REQUIRED_CASE_FIELDS.issubset(case):
        raise ValueError("invalid case")
    if case["status"] not in _CASE_STATUSES:
        raise ValueError("invalid status")
    if not isinstance(case["snippet_id"], str) or not case["snippet_id"]:
        raise ValueError("invalid snippet id")
    if not isinstance(case["base_version_id"], str) or not case["base_version_id"]:
        raise ValueError("invalid base version id")
    if not isinstance(case["base_version_number"], int) or case["base_version_number"] < 1:
        raise ValueError("invalid base version number")
    if case["verdict"] is not None and not isinstance(case["verdict"], dict):
        raise ValueError("invalid verdict")
    if case["adopted_version_id"] is not None and (
        not isinstance(case["adopted_version_id"], str) or not case["adopted_version_id"]
    ):
        raise ValueError("invalid adopted version id")
    _validate_verification(case["verification"])

    candidates = case["candidates"]
    if not isinstance(candidates, list):
        raise ValueError("invalid candidates")
    candidate_ids = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("invalid candidate")
        for field in ("id", "content", "parent_version_id"):
            if not isinstance(candidate.get(field), str) or not candidate[field]:
                raise ValueError("invalid candidate")
        if not isinstance(candidate.get("change_reasons"), list) or not all(
            isinstance(reason, str) for reason in candidate["change_reasons"]
        ):
            raise ValueError("invalid candidate")
        if not isinstance(candidate.get("created_at"), str):
            raise ValueError("invalid candidate")
        if candidate["id"] in candidate_ids:
            raise ValueError("duplicate candidate id")
        candidate_ids.add(candidate["id"])


def create_repair_case(
    snippet_id,
    base_version_id,
    base_version_number,
    prompt,
    output,
    comparison_input,
    task_goal="",
):
    if not isinstance(snippet_id, str) or not snippet_id:
        raise ValueError("invalid snippet id")
    if not isinstance(base_version_id, str) or not base_version_id:
        raise ValueError("invalid base version id")
    if not isinstance(base_version_number, int) or base_version_number < 1:
        raise ValueError("invalid base version number")
    if not all(
        isinstance(value, str)
        for value in (prompt, output, comparison_input, task_goal)
    ):
        raise ValueError("repair case inputs must be strings")
    return {
        "id": _id("case_"),
        "status": "captured",
        "created_at": _now(),
        "snippet_id": snippet_id,
        "task_goal": task_goal,
        "base_version_id": base_version_id,
        "base_version_number": base_version_number,
        "failure": {"prompt": prompt, "output": output},
        "comparison": {"input": comparison_input},
        "candidates": [],
        "verification": {
            "status": "pending",
            "input": "",
            "output_note": "",
            "rating": None,
            "verified_at": None,
        },
        "adopted_version_id": None,
        "verdict": None,
    }


def create_candidate(case, content, change_reasons):
    _validate_case(case)
    if case["status"] not in _CANDIDATE_STATUSES:
        raise ValueError("cannot create candidate from terminal case")
    if not isinstance(content, str):
        raise ValueError("candidate content must be a string")
    if not isinstance(change_reasons, list) or not all(
        isinstance(reason, str) for reason in change_reasons
    ):
        raise ValueError("change_reasons must be a list of strings")

    candidate = {
        "id": _id("candidate_"),
        "parent_version_id": case["base_version_id"],
        "content": content,
        "change_reasons": list(change_reasons),
        "created_at": _now(),
    }
    case["candidates"].append(candidate)
    case["status"] = "candidate_ready"
    return candidate


def record_verification(case, input_text, output_note, rating, passed):
    _validate_case(case)
    if case["status"] != "candidate_ready":
        raise ValueError("case is not ready for verification")
    if not isinstance(input_text, str) or not isinstance(output_note, str):
        raise ValueError("verification fields must be strings")
    if rating is not None and (not isinstance(rating, int) or not 1 <= rating <= 5):
        raise ValueError("rating must be an integer from 1 to 5")
    if not isinstance(passed, bool):
        raise ValueError("passed must be a boolean")

    verification = {
        "status": "passed" if passed else "failed",
        "input": input_text,
        "output_note": output_note,
        "rating": rating,
        "verified_at": _now(),
    }
    case["verification"] = verification
    case["status"] = "validation_pending" if passed else "validation_failed"
    return verification


def record_pairwise_verification(
    case,
    runs,
    overall_conclusion,
    passed,
    summary_note="",
):
    _validate_case(case)
    if case["status"] != "candidate_ready":
        raise ValueError("case is not ready for verification")
    if not isinstance(runs, list) or not (1 <= len(runs) <= 5):
        raise ValueError("runs must be a list containing 1 to 5 items")
    if overall_conclusion not in _PAIRWISE_OVERALL_CONCLUSIONS:
        raise ValueError("invalid overall conclusion")
    if not isinstance(passed, bool):
        raise ValueError("passed must be a boolean")
    if not isinstance(summary_note, str):
        raise ValueError("summary_note must be a string")

    sanitized_runs = []
    for idx, run in enumerate(runs):
        if not isinstance(run, dict):
            raise ValueError("each run must be a dict")
        verdict = run.get("verdict", "undecided")
        if verdict not in _PAIRWISE_VERDICTS:
            raise ValueError("invalid pairwise verdict")
        source_type = run.get("source_type", "legacy_unknown")
        if source_type not in _PAIRWISE_SOURCE_TYPES:
            raise ValueError("invalid pairwise source type")
        user_confirmed = run.get("user_confirmed", False)
        if not isinstance(user_confirmed, bool):
            raise ValueError("invalid pairwise user confirmation")
        truncated = run.get("truncated", False)
        if not isinstance(truncated, bool):
            raise ValueError("invalid pairwise truncation status")
        source_chars = run.get("source_chars", len(str(run.get("context_text") or "")))
        context_chars = run.get("context_chars", len(str(run.get("context_text") or "")))
        if not isinstance(source_chars, int) or source_chars < 0:
            raise ValueError("invalid pairwise source chars")
        if not isinstance(context_chars, int) or context_chars < 0:
            raise ValueError("invalid pairwise context chars")
        sanitized_runs.append({
            "id": str(run.get("id") or f"run_{idx + 1}"),
            "source_type": source_type,
            "source_label": str(run.get("source_label") or ""),
            "context_scope": str(run.get("context_scope") or ""),
            "source_chars": source_chars,
            "context_chars": context_chars,
            "context_label": str(run.get("context_label") or ""),
            "context_text": str(run.get("context_text") or ""),
            "context_hash": str(run.get("context_hash") or ""),
            "truncated": truncated,
            "user_confirmed": user_confirmed,
            "captured_at": str(run.get("captured_at") or ""),
            "user_input": str(run.get("user_input") or ""),
            "baseline_output": str(run.get("baseline_output") or ""),
            "candidate_output": str(run.get("candidate_output") or ""),
            "baseline_latency_ms": int(run.get("baseline_latency_ms") or 0),
            "candidate_latency_ms": int(run.get("candidate_latency_ms") or 0),
            "verdict": verdict,
            "note": str(run.get("note") or ""),
        })

    if passed and not any(
        run["source_type"] in {"real_business_replay", "historical_failure", "designed_boundary"}
        and run["user_confirmed"]
        for run in sanitized_runs
    ):
        raise ValueError("AI 合成样本不能单独作为正式采纳证据")

    verification = {
        "status": "passed" if passed else "failed",
        "input": sanitized_runs[0]["user_input"] if sanitized_runs else "",
        "output_note": summary_note,
        "rating": 5 if passed and overall_conclusion == "candidate_better" else None,
        "pairwise_runs": sanitized_runs,
        "overall_conclusion": overall_conclusion,
        "summary_note": summary_note,
        "verified_at": _now(),
    }
    case["verification"] = verification
    case["status"] = "validation_pending" if passed else "validation_failed"
    return verification


def record_verdict(case, candidate_id, choice, note=""):
    _validate_case(case)
    if case["status"] not in _VERDICT_STATUSES:
        raise ValueError("case is not ready for a verdict")
    if case["verdict"] is not None:
        raise ValueError("verdict already recorded")
    if not isinstance(candidate_id, str):
        raise ValueError("candidate id must be a string")
    if choice not in _CHOICES:
        raise ValueError("invalid choice")
    if not isinstance(note, str):
        raise ValueError("verdict note must be a string")
    if not any(
        isinstance(candidate, dict) and candidate.get("id") == candidate_id
        for candidate in case["candidates"]
    ):
        raise ValueError("candidate not found")

    verdict = {
        "candidate_id": candidate_id,
        "choice": choice,
        "note": note,
        "created_at": _now(),
    }
    case["verdict"] = verdict
    case["status"] = {
        "adopt": "validation_pending",
        "edit": "editing",
        "reject": "discarded",
    }[choice]
    return verdict
