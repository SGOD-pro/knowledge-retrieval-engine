#!/usr/bin/env python3
"""Mixed-corpus 180-question benchmark runner exercising the normal query API and persisting per-question evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
SRC_DIR = BACKEND_DIR / "src"
ROOT_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from config import settings
from db.workspace_boundary_repo import get_workspace_boundary_repo
from modules.query.query_repository import QueryRepository
from providers.bedrock_models import get_embedding_model, get_llm_model
from services.evaluation.mixed_180_evaluator import (
    SCORER_VERSION,
    grade_180_record,
    load_engine_requests,
    resolve_180_locators,
    validate_180_dataset,
)
from services.retrieval.bm25_retriever import invalidate_chunk_cache


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _git_head_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT_DIR), text=True).strip()
    except Exception:
        return "unknown"


def _git_working_tree_status() -> dict[str, Any]:
    head_sha = _git_head_sha()
    try:
        status_out = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=str(ROOT_DIR), text=True
        ).strip()
        diff_summary = subprocess.check_output(
            ["git", "diff", "--stat"], cwd=str(ROOT_DIR), text=True
        ).strip()
        lines = [line.strip() for line in status_out.splitlines() if line.strip()]
        is_dirty = len(lines) > 0
        fingerprint_raw = f"{head_sha}\n{status_out}\n{diff_summary}"
        fingerprint = hashlib.sha256(fingerprint_raw.encode("utf-8")).hexdigest()
        return {
            "head_sha": head_sha,
            "is_dirty": is_dirty,
            "uncommitted_files_count": len(lines),
            "uncommitted_files": [l[3:] if len(l) > 3 else l for l in lines],
            "fingerprint": fingerprint,
        }
    except Exception:
        return {
            "head_sha": head_sha,
            "is_dirty": True,
            "uncommitted_files_count": 0,
            "uncommitted_files": [],
            "fingerprint": "unknown",
        }


def get_reranker_policy() -> str:
    """Return effective configured reranker provider, model, and fallback policy."""
    if os.environ.get("RERANKER_POLICY"):
        return os.environ["RERANKER_POLICY"]

    bedrock_enabled = (
        os.environ.get("BEDROCK_RERANKER_ENABLED", "").lower() in ("true", "1")
        or getattr(settings, "BEDROCK_RERANKER_ENABLED", False)
        or os.environ.get("RERANKER_PROVIDER") == "bedrock"
        or getattr(settings, "RERANKER_PROVIDER", "") == "bedrock"
    )
    if bedrock_enabled:
        model = getattr(settings, "BEDROCK_RERANKER_MODEL", "amazon.rerank-v1:0")
        region = getattr(settings, "BEDROCK_RERANKER_REGION", "us-west-2")
        return f"bedrock:{model}:{region} (fallback: lexical_bm25_plus)"

    if os.environ.get("RERANKER_PROVIDER") in ("lexical", "bm25", "bm25plus") or getattr(settings, "RERANKER_PROVIDER", "") in ("lexical", "bm25", "bm25plus"):
        return "lexical_bm25_plus"

    model = getattr(settings, "BEDROCK_RERANKER_MODEL", "amazon.rerank-v1:0")
    region = getattr(settings, "BEDROCK_RERANKER_REGION", "us-west-2")
    return f"bedrock:{model}:{region} (fallback: lexical_bm25_plus)"


def _classify_failure(grade: dict[str, Any], http_error: str | None) -> str | None:
    if http_error:
        return "infrastructure_failure"
    if not grade.get("scorable"):
        return "ingestion_failure"
    if grade.get("grounded_correct"):
        return None
    reason = str(grade.get("reason") or "")
    if reason in ("unexpected_refusal", "citation_locator_mismatch", "refusal_with_citations"):
        return "retrieval_miss"
    return "answer_failure"


def _default_query_caller(base_url: str | None, headers: dict[str, str]):
    if base_url:
        client = httpx.Client(base_url=base_url, timeout=90.0)

        def _call(payload: dict[str, Any]) -> dict[str, Any]:
            r = client.post("/api/v1/query", json=payload, headers=headers)
            r.raise_for_status()
            return r.json()

        return _call, client.close

    from fastapi.testclient import TestClient
    import main as app_main

    tc = TestClient(app_main.app)

    def _call_tc(payload: dict[str, Any]) -> dict[str, Any]:
        r = tc.post("/api/v1/query", json=payload, headers=headers)
        r.raise_for_status()
        return r.json()

    return _call_tc, tc.close


def run_mixed_180(
    *,
    workspace_id: str,
    shuffle_seed: int | None = None,
    limit: int | None = None,
    staged_dir: Path | None = None,
    base_url: str | None = None,
    auth_token: str = "test_token_alice",
    output_dir: Path | None = None,
    baseline_commit_sha: str | None = None,
    query_caller=None,
) -> dict[str, Any]:
    q_only_p = ROOT_DIR / "data" / "kre_questions_only_180.json"
    bench_p = ROOT_DIR / "data" / "kre_benchmark_180.json"
    docs_p = ROOT_DIR / "data" / "kre_shuffled_documents.json"

    val_summary = validate_180_dataset(q_only_p, bench_p, docs_p, staged_dir=staged_dir)
    engine_reqs = load_engine_requests(q_only_p)
    bench_by_id = {q["id"]: q for q in json.loads(bench_p.read_text(encoding="utf-8"))["questions"]}

    test_suite_sha256 = _file_sha256(bench_p)
    corpus_manifest_sha256 = _file_sha256(docs_p)
    git_status = _git_working_tree_status()
    effective_head = git_status["head_sha"]
    effective_baseline_commit = baseline_commit_sha or effective_head

    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(engine_reqs)
    if limit:
        engine_reqs = engine_reqs[:limit]

    invalidate_chunk_cache()
    b_repo = get_workspace_boundary_repo()
    snaps = {s.source: s for s in b_repo.list_document_snapshots(workspace_id)}
    chunks = QueryRepository().get_all_chunks(workspace_id=workspace_id)

    # Resolve required locators before paid query calls
    precheck_by_id: dict[str, dict[str, Any]] = {}
    for req_item in engine_reqs:
        qid = req_item["id"]
        b_item = bench_by_id[qid]
        precheck_by_id[qid] = resolve_180_locators(b_item.get("expected_sources") or [], chunks, snaps)

    closer = None
    if query_caller is None:
        headers = {"Authorization": f"Bearer {auth_token}"}
        query_caller, closer = _default_query_caller(base_url, headers)

    results: list[dict[str, Any]] = []
    try:
        for req_item in engine_reqs:
            qid = req_item["id"]
            b_item = bench_by_id[qid]
            resolved = precheck_by_id[qid]
            exp_state = b_item.get("expected_state", "COMPLETE")

            if resolved.get("unscorable_reasons") and exp_state in ("COMPLETE", "PARTIAL"):
                grade = grade_180_record(b_item, {"answer": "", "citations": []}, resolved)
                grade["latency_ms"] = 0.0
                grade["executed_path"] = None
                grade["skipped_paid_query"] = True
                grade["failure_category"] = "ingestion_failure"
                grade["citations"] = []
                results.append(grade)
                continue

            # Verify that gold fields never reach the runtime query API
            disallowed_gold_keys = {"expected_sources", "preferred_answer", "expected_state", "locator", "ground_truth", "gold"}
            leaked_in_req = set(req_item.keys()) & disallowed_gold_keys
            if leaked_in_req:
                raise ValueError(f"Gold fields leaked into request item: {leaked_in_req}")

            # Send ONLY question text and normal query parameters to the runtime
            payload = {
                "query": req_item["question"],
                "workspace_id": workspace_id,
                "cache": False,
                "benchmark_mode": True,
            }
            leaked_in_payload = set(payload.keys()) & disallowed_gold_keys
            if leaked_in_payload:
                raise ValueError(f"Gold fields leaked into query payload: {leaked_in_payload}")
            t0 = time.perf_counter()
            http_error: str | None = None
            resp: dict[str, Any] = {}
            try:
                resp = query_caller(payload)
            except Exception as exc:
                http_error = f"{type(exc).__name__}: {exc}"
            ms = round((time.perf_counter() - t0) * 1000.0, 2)

            if http_error:
                grade = {
                    "id": qid,
                    "expected_state": exp_state,
                    "scorable": True,
                    "unscorable_reasons": [],
                    "answer_correct": False,
                    "grounded_correct": False,
                    "reason": f"infrastructure_error:{http_error}",
                }
            else:
                grade = grade_180_record(b_item, resp, resolved)

            grade["latency_ms"] = ms
            grade["executed_path"] = resp.get("executed_path")
            grade["skipped_paid_query"] = False
            grade["failure_category"] = _classify_failure(grade, http_error)
            grade["answer"] = resp.get("answer")
            grade["citations"] = [
                {
                    "chunk_id": c.get("chunk_id"),
                    "document_filename": c.get("document_filename"),
                    "document_version": c.get("document_version"),
                }
                for c in (resp.get("citations") or [])
                if isinstance(c, dict)
            ]
            grade["retrieval_candidates"] = resp.get("retrieval_candidates") or {}
            results.append(grade)
    finally:
        if closer is not None:
            closer()

    scorable = [r for r in results if r["scorable"]]
    counts = {
        "infrastructure_failures": sum(1 for r in results if r.get("failure_category") == "infrastructure_failure"),
        "ingestion_failures": sum(1 for r in results if r.get("failure_category") == "ingestion_failure"),
        "retrieval_misses": sum(1 for r in results if r.get("failure_category") == "retrieval_miss"),
        "answer_failures": sum(1 for r in results if r.get("failure_category") == "answer_failure"),
    }

    overall_acc = round(sum(1 for r in scorable if r["grounded_correct"]) / max(1, len(scorable)), 4)
    ans_text_acc = round(sum(1 for r in scorable if r["answer_correct"]) / max(1, len(scorable)), 4)
    grounded_acc = overall_acc
    refusal_subset = [r for r in results if r.get("expected_state") == "NO_SUPPORT"]
    refusal_acc = (
        round(
            sum(1 for r in refusal_subset if r.get("answer_correct", False))
            / max(1, len(refusal_subset)),
            4,
        )
        if refusal_subset
        else 1.0
    )

    # Compute retrieval metrics from recorded evidence where available
    import math

    recalls: list[float] = []
    precisions: list[float] = []
    mrrs: list[float] = []
    ndcgs: list[float] = []

    for r in scorable:
        cands = r.get("retrieval_candidates")
        if isinstance(cands, list) and cands:
            cand_ids = [c if isinstance(c, str) else str(c.get("chunk_id", "")) for c in cands]
            resolved_for_q = resolved_by_qid.get(r["id"], {})
            target_ids = set(resolved_for_q.get("resolved_chunk_ids") or [])
            if target_ids:
                top5 = cand_ids[:5]
                top3 = cand_ids[:3]
                recalls.append(1.0 if any(cid in target_ids for cid in top5) else 0.0)
                precisions.append(sum(1 for cid in top3 if cid in target_ids) / 3.0)
                mrr_val = 0.0
                for rank, cid in enumerate(top5, 1):
                    if cid in target_ids:
                        mrr_val = 1.0 / rank
                        break
                mrrs.append(mrr_val)
                dcg = sum(1.0 / math.log2(rank + 1) for rank, cid in enumerate(top5, 1) if cid in target_ids)
                ndcgs.append(min(1.0, dcg / 1.0))

    grounded_recall_5 = round(sum(recalls) / len(recalls), 4) if recalls else "unmeasured"
    precision_3 = round(sum(precisions) / len(precisions), 4) if precisions else "unmeasured"
    mrr_5 = round(sum(mrrs) / len(mrrs), 4) if mrrs else "unmeasured"
    ndcg_5 = round(sum(ndcgs) / len(ndcgs), 4) if ndcgs else "unmeasured"

    summary = {
        # Baseline reference and comparator requirement fields
        "commit_sha": effective_head,
        "baseline_commit_sha": effective_baseline_commit,
        "test_suite_sha256": test_suite_sha256,
        "corpus_manifest_sha256": corpus_manifest_sha256,
        "workspace_id": workspace_id,
        "runner_version": "2.3.0",
        "scorer_version": SCORER_VERSION,
        "embedding_model": get_embedding_model(),
        "llm_model": get_llm_model(),
        "reranker_policy": get_reranker_policy(),
        "cache_mode": "bypass_exact_and_semantic",
        "benchmark_type": "mixed_180",
        # Test Suite metadata
        "test_suite_name": "kre_benchmark_180",
        "shuffle_seed": shuffle_seed,
        "total": len(results),
        "scorable": len(scorable),
        "unscorable": len(results) - len(scorable),
        # Accuracy & nested metrics for benchmark_comparator
        "answer_accuracy": ans_text_acc,
        "grounded_accuracy": grounded_acc,
        "metrics": {
            "overall_accuracy": overall_acc,
            "answer_text_accuracy": ans_text_acc,
            "grounded_accuracy": grounded_acc,
            "refusal_accuracy": refusal_acc,
            "grounded_recall_at_5": grounded_recall_5,
            "precision_at_3": precision_3,
            "mrr_at_5": mrr_5,
            "ndcg_at_5": ndcg_5,
            "llm_activation_rate": round(
                sum(
                    1
                    for r in results
                    if not r.get("skipped_paid_query", False)
                    and r.get("executed_path") in ("full", "synthesis")
                )
                / max(1, len(results)),
                4,
            ),
        },
        # Effective runtime settings
        "runtime_settings": {
            "workspace_repo_backend": os.getenv("KRE_WORKSPACE_REPO_BACKEND")
            or getattr(settings, "WORKSPACE_REPO_BACKEND", "dynamodb"),
            "table_store_backend": os.getenv("KRE_TABLE_STORE_BACKEND")
            or getattr(settings, "TABLE_STORE_BACKEND", "dynamodb"),
            "reranker_provider": os.getenv("RERANKER_PROVIDER")
            or getattr(settings, "RERANKER_PROVIDER", "bm25plus"),
            "okf_enabled": os.getenv("OKF_ENABLED", "").lower() in ("true", "1")
            or getattr(settings, "OKF_ENABLED", False),
            "graph_enabled": os.getenv("GRAPH_ENABLED", "").lower() in ("true", "1")
            or getattr(settings, "GRAPH_ENABLED", False),
            "laya_enabled": os.getenv("LAYA_ENABLED", "").lower() in ("true", "1")
            or getattr(settings, "LAYA_ENABLED", False),
        },
        # Working tree fingerprint
        "dirty_working_tree": {
            "is_dirty": git_status["is_dirty"],
            "uncommitted_files_count": git_status["uncommitted_files_count"],
            "uncommitted_files": git_status["uncommitted_files"],
            "fingerprint": git_status["fingerprint"],
        },
        "failure_breakdown": counts,
        "validation": val_summary,
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = output_dir or (BACKEND_DIR / "reports" / summary["commit_sha"] / ts)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out_dir / "mixed_180_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    summary["output_dir"] = str(out_dir)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run mixed-corpus 180-question benchmark.")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--shuffle-seed", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--staged-dir", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--auth-token", default="test_token_alice")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--baseline-commit-sha", default=None)
    args = parser.parse_args()

    summary = run_mixed_180(
        workspace_id=args.workspace_id,
        shuffle_seed=args.shuffle_seed,
        limit=args.limit,
        staged_dir=Path(args.staged_dir) if args.staged_dir else None,
        base_url=args.base_url,
        auth_token=args.auth_token,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        baseline_commit_sha=args.baseline_commit_sha,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
