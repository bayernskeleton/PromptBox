from copy import deepcopy
from typing import Any


_SOURCE_TYPES = (
    "real_business_replay",
    "historical_failure",
    "designed_boundary",
    "ai_synthetic",
)


class VerificationEvidenceError(ValueError):
    """Raised when a version cannot provide a coherent evidence view."""


def _find_by_id(items: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
    return next((item for item in items if item.get("id") == item_id), None)


def get_version_evidence(
    data: dict[str, Any],
    snippet_id: str,
    version_id: str,
) -> dict[str, Any]:
    snippet = _find_by_id(data.get("snippets", []), snippet_id)
    if snippet is None:
        raise VerificationEvidenceError("snippet_not_found")
    version = _find_by_id(snippet.get("versions", []), version_id)
    if version is None:
        raise VerificationEvidenceError("version_not_found")
    repair_case_id = version.get("repair_case_id")
    if not repair_case_id:
        raise VerificationEvidenceError("该版本没有成对验证证据")
    repair_case = _find_by_id(data.get("repair_cases", []), repair_case_id)
    if repair_case is None:
        raise VerificationEvidenceError("验证记录缺失")
    verification = repair_case.get("verification")
    if not isinstance(verification, dict):
        raise VerificationEvidenceError("验证记录结构缺失")

    baseline_version = _find_by_id(
        snippet.get("versions", []), repair_case.get("base_version_id")
    )
    if baseline_version is None:
        raise VerificationEvidenceError("基线版本缺失")
    candidates = repair_case.get("candidates") or []
    if not candidates:
        raise VerificationEvidenceError("候选版本缺失")
    candidate = candidates[-1]
    runs = verification.get("pairwise_runs") or []
    source_counts = {source_type: 0 for source_type in _SOURCE_TYPES}
    missing_provenance_fields: set[str] = set()
    for run in runs:
        source_type = run.get("source_type")
        if source_type in source_counts:
            source_counts[source_type] += 1
        else:
            missing_provenance_fields.add("source_type")
        for field in (
            "source_label",
            "context_scope",
            "source_chars",
            "context_chars",
            "context_hash",
            "truncated",
            "user_confirmed",
            "captured_at",
        ):
            if field not in run:
                missing_provenance_fields.add(field)

    return {
        "snippet": deepcopy({key: snippet.get(key) for key in ("id", "title")}),
        "version": deepcopy(version),
        "baseline_version": deepcopy(baseline_version),
        "candidate": deepcopy(candidate),
        "repair_case": deepcopy({
            key: repair_case.get(key)
            for key in ("id", "base_version_id", "adopted_version_id", "task_goal")
        }),
        "analysis": deepcopy(repair_case.get("analysis")),
        "verification": deepcopy(verification),
        "source_counts": source_counts,
        "missing_provenance_fields": sorted(missing_provenance_fields),
    }


def list_verification_records(
    data: dict[str, Any],
    *,
    snippet_id: str | None = None,
    version_id: str | None = None,
    verdict: str | None = None,
    source_type: str | None = None,
    context_label: str | None = None,
) -> list[dict[str, Any]]:
    """Flatten saved pairwise runs into a read-only, filterable list."""
    records = []
    for snippet in data.get("snippets", []):
        if snippet_id and snippet.get("id") != snippet_id:
            continue
        versions_by_case = {
            version.get("repair_case_id"): version
            for version in snippet.get("versions", [])
            if version.get("repair_case_id")
        }
        for case in data.get("repair_cases", []):
            version = versions_by_case.get(case.get("id"))
            if version is None or (version_id and version.get("id") != version_id):
                continue
            verification = case.get("verification") or {}
            for run in verification.get("pairwise_runs") or []:
                if verdict and run.get("verdict") != verdict:
                    continue
                if source_type and run.get("source_type") != source_type:
                    continue
                if context_label and context_label not in (run.get("context_label") or ""):
                    continue
                record = deepcopy(run)
                record.update({
                    "repair_case_id": case.get("id"),
                    "snippet_id": snippet.get("id"),
                    "snippet_title": snippet.get("title", ""),
                    "version_id": version.get("id"),
                    "version_number": version.get("version_number"),
                    "overall_conclusion": verification.get("overall_conclusion"),
                    "verification_status": verification.get("status"),
                    "verified_at": verification.get("verified_at"),
                })
                records.append(record)
    records.sort(
        key=lambda item: item.get("captured_at") or item.get("verified_at") or "",
        reverse=True,
    )
    return records


__all__ = ["VerificationEvidenceError", "get_version_evidence", "list_verification_records"]
