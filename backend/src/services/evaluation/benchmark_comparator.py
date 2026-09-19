"""Benchmark manifest comparator.

Strictly enforces 9-variable equality and baseline commit reference.
Emits 'NOT COMPARABLE — no accuracy delta may be claimed' on any mismatch.
"""

from typing import Any

REQUIRED_EQUAL_FIELDS = [
    "test_suite_sha256",
    "corpus_manifest_sha256",
    "workspace_id",
    "runner_version",
    "scorer_version",
    "embedding_model",
    "llm_model",
    "reranker_policy",
    "cache_mode",
    "benchmark_type",
]

NOT_COMPARABLE_MESSAGE = "NOT COMPARABLE — no accuracy delta may be claimed"


def compare_manifests(
    candidate_manifest: dict[str, Any],
    baseline_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Compare candidate benchmark manifest against baseline artifact.

    Rules:
      1. candidate.baseline_commit_sha must equal baseline.commit_sha.
      2. candidate.commit_sha may differ from baseline.commit_sha.
      3. All REQUIRED_EQUAL_FIELDS must match exactly.
      4. Any violation returns NOT_COMPARABLE_MESSAGE.
    """
    mismatches = []

    # Rule 1: candidate.baseline_commit_sha must equal baseline.commit_sha
    cand_base_ref = candidate_manifest.get("baseline_commit_sha")
    base_commit = baseline_manifest.get("commit_sha")

    if not cand_base_ref or cand_base_ref != base_commit:
        mismatches.append(
            f"baseline_commit_sha mismatch: candidate reference '{cand_base_ref}' != baseline commit '{base_commit}'"
        )

    # Rule 3: Exact match on all required fields
    for field in REQUIRED_EQUAL_FIELDS:
        cand_val = candidate_manifest.get(field)
        base_val = baseline_manifest.get(field)
        if cand_val != base_val:
            mismatches.append(
                f"Field '{field}' mismatch: candidate='{cand_val}' != baseline='{base_val}'"
            )

    if mismatches:
        return {
            "comparable": False,
            "status": NOT_COMPARABLE_MESSAGE,
            "mismatches": mismatches,
            "deltas": {},
        }

    # If valid, calculate deltas
    cand_metrics = candidate_manifest.get("metrics", candidate_manifest)
    base_metrics = baseline_manifest.get("metrics", baseline_manifest)

    def _diff(key: str) -> float:
        return round(float(cand_metrics.get(key, 0.0)) - float(base_metrics.get(key, 0.0)), 4)

    deltas = {
        "overall_accuracy_delta": _diff("overall_accuracy"),
        "refusal_accuracy_delta": _diff("refusal_accuracy"),
        "grounded_recall_at_5_delta": _diff("grounded_recall_at_5"),
        "precision_at_3_delta": _diff("precision_at_3"),
        "mrr_at_5_delta": _diff("mrr_at_5"),
        "ndcg_at_5_delta": _diff("ndcg_at_5"),
        "llm_activation_rate_delta": _diff("llm_activation_rate"),
    }

    return {
        "comparable": True,
        "status": "COMPARABLE",
        "candidate_commit_sha": candidate_manifest.get("commit_sha"),
        "baseline_commit_sha": base_commit,
        "mismatches": [],
        "deltas": deltas,
    }
