#!/usr/bin/env python3
"""Canonical 60-Question RAG Benchmark Runner (Q136-Q195).

Strictly evaluates the canonical 60-question evaluation suite against ws_fresh_benchmark.
Enforces:
  1. Clean worktree check (git status --porcelain).
  2. Corpus completeness and SHA-256 hash tracking.
  3. Live provider validation (fails if any provider is mocked).
  4. Bypasses exact and semantic cache on every query (benchmark_mode=True).
  5. Clears in-process BM25 cache once at start of cold benchmark.
  6. Evaluates raw candidate chunk IDs prior to generation for Grounded Recall@5,
     Precision@3, MRR@5, and nDCG@5 against structured relevance ground truth.
  7. Tightened grading for premise correction, arithmetic, and JSON compliance.
  8. Writes immutable reports to backend/reports/<commit_sha>/<timestamp>/manifest.json.
  9. Strict 9-variable equality comparison with baseline artifact.
"""

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
import unittest.mock

# Ensure backend/src is in sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
SRC_DIR = BACKEND_DIR / "src"
ROOT_DIR = BACKEND_DIR.parent

sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from modules.query.query_service import QueryService
from modules.query.query_repository import QueryRepository
from schemas.models import QueryRequest
from services.evaluation.benchmark_scorer import (
    compute_retrieval_metrics,
    grade_premise_correction,
    grade_arithmetic_answer,
    grade_json_schema,
    grade_structured_numeric,
    grade_structured_json,
    grade_structured_premise,
    validate_citations,
    content_match,
    compute_answer_relevancy,
    compute_faithfulness,
)
from services.evaluation.benchmark_comparator import (
    compare_manifests,
    NOT_COMPARABLE_MESSAGE,
)
from services.retrieval.bm25_retriever import invalidate_chunk_cache

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("canonical_60_benchmark")


def verify_clean_worktree():
    """Verify that git working tree has no uncommitted tracked or unstaged changes."""
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            check=True,
        )
        lines = [
            line for line in res.stdout.strip().split("\n")
            if line and not line.strip().startswith("??")  # ignore untracked if any
        ]
        if lines:
            raise RuntimeError(
                f"Worktree is not clean. Uncommitted changes detected:\n" + "\n".join(lines)
            )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to check git status: {e}")


def get_current_commit_sha() -> str:
    """Get HEAD commit SHA."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return "unknown_commit"


def verify_live_providers():
    """Verify that no remote providers or generation functions are mocked."""
    from providers import llm_provider, embedding_provider, reranker_provider

    targets = [
        ("llm_provider.generate_completion", llm_provider.generate_completion),
        ("embedding_provider.embed_text", embedding_provider.embed_text),
        ("reranker_provider.rerank_documents", reranker_provider.rerank_documents),
    ]
    for name, target in targets:
        if isinstance(target, (unittest.mock.Mock, unittest.mock.MagicMock)):
            raise RuntimeError(f"Mocked provider detected for {name}. Benchmark runner requires live providers.")
        if hasattr(target, "assert_called") or hasattr(target, "return_value"):
            if isinstance(target.return_value, (unittest.mock.Mock, unittest.mock.MagicMock)):
                raise RuntimeError(f"Mocked return value detected for {name}. Benchmark runner requires live providers.")


def compute_file_sha256(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_corpus_manifest_hash(documents_meta: list[dict]) -> tuple[str, dict[str, str]]:
    """Compute combined SHA-256 hash across all corpus documents in data/."""
    file_hashes = {}
    combined_hash = hashlib.sha256()

    for doc in sorted(documents_meta, key=lambda x: x["filename"]):
        fname = doc["filename"]
        matches = list(ROOT_DIR.glob(f"data/**/{fname}"))
        if not matches:
            matches = list(ROOT_DIR.glob(f"**/{fname}"))
        if not matches:
            raise FileNotFoundError(f"Corpus document '{fname}' not found in workspace.")
        fpath = matches[0]
        fhash = compute_file_sha256(fpath)
        file_hashes[fname] = fhash
        combined_hash.update(f"{fname}:{fhash}".encode("utf-8"))

    return combined_hash.hexdigest(), file_hashes


GROUND_TRUTH_FILE = BACKEND_DIR / "evaluation_assets" / "canonical_60_ground_truth.json"


def resolve_ground_truth_chunk_ids(
    ground_truth_entries: dict[str, Any],
    all_chunks: list[Any],
    doc_id_map: dict[str, str],
    doc_hashes: dict[str, str] | None = None,
) -> tuple[dict[str, set[str]], list[str]]:
    """Resolves structured ground truth locators to chunk IDs post-ingestion.
    Returns (resolved_map, unscorable_qids).

    Rules:
    - Never uses expected_answer, token overlap, or fallback to all document chunks.
    - If doc_hashes is provided, the document_sha256 in each evidence entry must match
      the actual corpus hash; mismatch marks the query UNSCORABLE_GROUND_TRUTH.
    - ALL required evidence locators must resolve to at least one chunk; partial
      resolution (some locators unmatched) marks the query UNSCORABLE_GROUND_TRUTH.
    """
    doc_fname_to_id: dict[str, str] = {}
    for k, v in doc_id_map.items():
        if any(k.endswith(ext) for ext in (".pdf", ".csv", ".xls", ".xlsx")):
            doc_fname_to_id[k] = v
        else:
            doc_fname_to_id[v] = k

    resolved: dict[str, set[str]] = {}
    unscorable: list[str] = []

    for qid, entry in ground_truth_entries.items():
        if entry.get("question_type") == "structured_aggregate":
            resolved[qid] = set()
            continue

        ev_list = entry.get("relevant_evidence", [])
        if not ev_list:
            resolved[qid] = set()
            continue

        all_matched: set[str] = set()
        any_locator_unresolved = False

        for ev in ev_list:
            fname = ev["source_filename"]
            ev_hash = ev.get("document_sha256", "")
            target_did = doc_fname_to_id.get(fname)
            loc_type = ev["locator_type"]
            loc_val = ev["locator"]

            # Hash validation: if corpus hashes are available, verify match
            if doc_hashes and ev_hash:
                actual_hash = doc_hashes.get(fname, "")
                if actual_hash and actual_hash != ev_hash:
                    logger.warning(
                        "resolve.hash_mismatch qid=%s fname=%s expected=%s actual=%s",
                        qid, fname, ev_hash[:16], actual_hash[:16],
                    )
                    any_locator_unresolved = True
                    continue

            locator_matched: set[str] = set()
            for c in all_chunks:
                if str(c.document_id) != str(target_did):
                    continue

                if loc_type == "pdf_page":
                    try:
                        if c.page_number == int(loc_val):
                            locator_matched.add(str(c.id))
                    except (ValueError, TypeError):
                        pass
                elif loc_type in ("csv_row", "spreadsheet_row"):
                    try:
                        meta_row = (c.metadata or {}).get("row")
                        if meta_row is not None and meta_row == int(loc_val):
                            locator_matched.add(str(c.id))
                        elif c.location_reference and (
                            f"Row: {loc_val}" in c.location_reference
                            or f"Row {loc_val}" in c.location_reference
                        ):
                            locator_matched.add(str(c.id))
                    except (ValueError, TypeError):
                        pass

            if not locator_matched:
                any_locator_unresolved = True
            else:
                all_matched |= locator_matched

        # ALL required locators must resolve; any unresolved locator → UNSCORABLE
        if any_locator_unresolved:
            unscorable.append(qid)
            resolved[qid] = set()
        elif not all_matched:
            unscorable.append(qid)
            resolved[qid] = set()
        else:
            resolved[qid] = all_matched

    return resolved, unscorable


def run_benchmark(
    workspace_id: str = "ws_fresh_benchmark",
    benchmark_type: str = "cold",
    baseline_path: str | None = None,
    allow_dirty: bool = False,
    test_path: str = "data/test.json",
) -> dict[str, Any]:
    """Execute canonical benchmark run."""
    if not allow_dirty:
        verify_clean_worktree()

    verify_live_providers()

    test_file = ROOT_DIR / test_path
    if not test_file.exists():
        raise FileNotFoundError(f"Test suite file not found: {test_file}")

    suite_sha256 = compute_file_sha256(test_file)

    with open(test_file, "r", encoding="utf-8") as f:
        test_data = json.load(f)

    questions = test_data["questions"]
    documents_meta = test_data.get("documents", [])

    corpus_manifest_sha256, file_hashes = compute_corpus_manifest_hash(documents_meta)
    commit_sha = get_current_commit_sha()
    timestamp = time.strftime("%Y%m%d_%H%M%S")

    # Invalidate in-process cache for cold benchmark
    if benchmark_type == "cold":
        invalidate_chunk_cache()

    repo = QueryRepository()
    all_ws_chunks = repo.get_all_chunks(workspace_id=workspace_id)
    if not all_ws_chunks:
        raise RuntimeError(f"No chunks found in workspace '{workspace_id}'. Workspace must be ingested prior to running benchmark.")

    doc_id_map = {d["doc_id"]: d["filename"] for d in documents_meta}

    if not GROUND_TRUTH_FILE.exists():
        raise FileNotFoundError(f"Evaluator ground truth file not found: {GROUND_TRUTH_FILE}")

    with open(GROUND_TRUTH_FILE, "r", encoding="utf-8") as f:
        ground_truth_data = json.load(f)

    structured_ground_truth, unscorable_qids = resolve_ground_truth_chunk_ids(
        ground_truth_data, all_ws_chunks, doc_id_map
    )
    if unscorable_qids:
        logger.error("UNSCORABLE_GROUND_TRUTH detected on queries: %s", unscorable_qids)
        raise RuntimeError(f"Benchmark failed: {len(unscorable_qids)} UNSCORABLE_GROUND_TRUTH queries cannot resolve locators.")

    service = QueryService(repo=repo)

    logger.info(
        "Starting canonical 60-question benchmark (commit=%s, workspace=%s, type=%s, suite_hash=%s)",
        commit_sha[:8],
        workspace_id,
        benchmark_type,
        suite_sha256[:8],
    )

    query_results = []
    total_q = len(questions)

    refusal_total = 0
    refusal_correct = 0
    no_evidence_total = 0
    false_answer_count = 0
    unsupported_citation_count = 0
    structured_agg_total = 0
    structured_agg_correct = 0
    premise_correction_total = 0
    premise_correction_correct = 0
    infra_failure_count = 0

    all_recalls = []
    all_precisions = []
    all_mrrs = []
    all_ndcgs = []
    all_latencies = []
    llm_call_count = 0

    for idx, q in enumerate(questions, 1):
        qid = q["id"]
        category = q.get("category", "")
        q_text = q["question"]
        expected_ans = q.get("expected_answer")
        is_hallucination = q.get("hallucination_check", False)

        req = QueryRequest(
            query=q_text,
            workspace_id=workspace_id,
            cache=False,
            benchmark_mode=True,
        )

        t_start = time.perf_counter()
        resp = service.execute_query(req)
        query_latency_ms = (time.perf_counter() - t_start) * 1000.0
        all_latencies.append(query_latency_ms)

        # Assert response was NOT cached
        assert not resp.get("cached", False), f"Query {qid} returned cached response in benchmark_mode"

        if resp.get("status") == "error" or resp.get("error_code"):
            infra_failure_count += 1

        ans_raw = resp.get("answer", "")
        if isinstance(ans_raw, (dict, list)):
            ans_text = json.dumps(ans_raw)
        else:
            ans_text = str(ans_raw or "")
        candidates = resp.get("retrieval_candidates", {})
        reranked_ids = resp.get("reranked_chunk_ids") or candidates.get("reranked", [])
        compressed_ids = set(resp.get("compressed_context_chunk_ids") or candidates.get("compressed_context", []))
        llm_cited_ids = resp.get("llm_cited_chunk_ids") or candidates.get("llm_citations", [])
        top_citations = resp.get("citations", [])
        final_citation_ids = resp.get("final_citation_chunk_ids") or [
            str(c.get("chunk_id")) for c in top_citations if c.get("chunk_id")
        ]

        gt_entry = ground_truth_data.get(qid, {})
        contract = gt_entry.get("answer_contract", {})
        contract_type = contract.get("contract_type", "semantic")
        question_type = gt_entry.get("question_type") or ("structured_aggregate" if contract_type == "structured_aggregate" else "retrieval")

        # Evaluate retrieval metrics against resolved structured ground truth
        rel_ids = structured_ground_truth.get(qid, set())
        has_positive_evidence = len(rel_ids) > 0

        if question_type == "structured_aggregate":
            structured_agg_total += 1
            ret_metrics = {
                "recall_at_5": None,
                "precision_at_3": None,
                "mrr_at_5": None,
                "ndcg_at_5": None,
                "note": "excluded_structured_aggregate",
            }
        elif has_positive_evidence:
            ret_metrics = compute_retrieval_metrics(reranked_ids, rel_ids)
            all_recalls.append(ret_metrics["recall_at_5"])
            all_precisions.append(ret_metrics["precision_at_3"])
            all_mrrs.append(ret_metrics["mrr_at_5"])
            all_ndcgs.append(ret_metrics["ndcg_at_5"])
        else:
            # Exclude no-evidence queries from retrieval aggregate metrics
            ret_metrics = {
                "recall_at_5": None,
                "precision_at_3": None,
                "mrr_at_5": None,
                "ndcg_at_5": None,
                "note": "excluded_no_positive_evidence",
            }
            no_evidence_total += 1

        refusal_indicators = [
            "couldn't find any relevant",
            "not found",
            "does not contain",
            "not mentioned",
            "no information",
            "cannot find",
            "absent",
            "no positive source",
        ]
        ans_is_refusal = any(ind in ans_text.lower() for ind in refusal_indicators) or ans_text.strip() == "NOT_FOUND"

        is_infra_error = (resp.get("status") == "error" or bool(resp.get("error_code")))

        if contract_type == "refusal":
            refusal_total += 1
            if is_infra_error:
                # Requirement 15: Infrastructure failure is scored as incorrect or unmeasured, never as a correct refusal
                is_correct = False
                is_refusal = False
                false_answer_count += 1
            elif ans_is_refusal:
                refusal_correct += 1
                is_correct = True
                is_refusal = True
            else:
                false_answer_count += 1
            if len(top_citations) > 0 and not is_infra_error:
                unsupported_citation_count += 1
        elif contract_type == "numeric":
            is_correct = grade_structured_numeric(ans_text, contract)
            if question_type == "structured_aggregate" and is_correct:
                structured_agg_correct += 1
        elif contract_type == "json_schema":
            is_correct = grade_structured_json(ans_text, contract)
        elif contract_type == "premise_correction":
            refusal_total += 1
            premise_correction_total += 1
            is_correct = grade_structured_premise(ans_text, contract)
            if is_correct:
                refusal_correct += 1
                premise_correction_correct += 1
        else:
            if expected_ans:
                relevancy = compute_answer_relevancy(ans_text, expected_ans)
                c_match = content_match(ans_text, expected_ans)
                is_correct = c_match and relevancy >= 0.40

        # Check citations validity against compressed context and ground truth
        cit_val = validate_citations(
            top_citations,
            context_chunk_ids=compressed_ids if compressed_ids else None,
            ground_truth_chunk_ids=rel_ids if rel_ids else None,
        )

        executed_path = resp.get("executed_path", "full")
        gen_calls = resp.get("generation_calls", 0)
        if gen_calls > 0:
            llm_call_count += 1

        res_entry = {
            "id": qid,
            "category": category,
            "question": q_text,
            "expected_answer": expected_ans,
            "answer": ans_text,
            "is_correct": is_correct,
            "is_refusal": is_refusal,
            "executed_path": executed_path,
            "planned_path": resp.get("planned_path", "full"),
            "reranker_mode": resp.get("reranker_mode", "remote_success"),
            "retrieval_metrics": ret_metrics,
            "citation_validity": cit_val,
            "latency_ms": round(query_latency_ms, 2),
            "reranked_chunk_ids": reranked_ids,
            "compressed_context_chunk_ids": list(compressed_ids),
            "llm_cited_chunk_ids": llm_cited_ids,
            "final_citation_chunk_ids": final_citation_ids,
            "bedrock_embedding_calls": resp.get("bedrock_embedding_calls", 0),
            "bge_lambda_calls": resp.get("bge_lambda_calls", 0),
            "reranker_remote_calls": resp.get("reranker_remote_calls", 0),
            "generation_calls": gen_calls,
            "citations": top_citations,
        }
        query_results.append(res_entry)

        logger.info(
            "[%02d/%02d] %s (%s): %s | Path=%s | Reranker=%s | Latency=%.1fms",
            idx,
            total_q,
            qid,
            category,
            "PASS" if is_correct else "FAIL",
            executed_path,
            resp.get("reranker_mode", "remote_success"),
            query_latency_ms,
        )

    # Compute aggregate metrics with strict denominator handling
    pos_count = len(all_recalls)
    overall_acc = round(sum(1 for r in query_results if r["is_correct"]) / total_q, 4)
    refusal_acc = round(refusal_correct / max(1, refusal_total), 4) if refusal_total > 0 else 1.0
    avg_recall_5 = round(sum(all_recalls) / pos_count, 4) if pos_count > 0 else 0.0
    avg_precision_3 = round(sum(all_precisions) / pos_count, 4) if pos_count > 0 else 0.0
    avg_mrr_5 = round(sum(all_mrrs) / pos_count, 4) if pos_count > 0 else 0.0
    avg_ndcg_5 = round(sum(all_ndcgs) / pos_count, 4) if pos_count > 0 else 0.0
    llm_act_rate = round(llm_call_count / total_q, 4)

    false_answer_rate = round(false_answer_count / max(1, no_evidence_total), 4) if no_evidence_total > 0 else 0.0
    unsupported_cit_rate = round(unsupported_citation_count / max(1, no_evidence_total), 4) if no_evidence_total > 0 else 0.0

    from providers.bedrock_models import get_embedding_model, get_llm_model

    manifest = {
        "commit_sha": commit_sha,
        "baseline_commit_sha": commit_sha,  # Set self as baseline commit if creating initial baseline
        "timestamp": timestamp,
        "workspace_id": workspace_id,
        "benchmark_type": benchmark_type,
        "runner_version": "2.1.0",
        "scorer_version": "2.1.0",
        "test_suite_name": test_data.get("test_suite_name", "canonical_60"),
        "test_suite_sha256": suite_sha256,
        "corpus_manifest_sha256": corpus_manifest_sha256,
        "corpus_file_hashes": file_hashes,
        "embedding_model": get_embedding_model(),
        "llm_model": get_llm_model(),
        "reranker_policy": "openrouter:cross-encoder/ms-marco-MiniLM-L-6-v2",
        "cache_mode": "bypass_exact_and_semantic",
        "total_questions": total_q,
        "denominators": {
            "total_questions": total_q,
            "answerable_retrieval_questions": pos_count,
            "answerable_structured_aggregate_questions": structured_agg_total,
            "refusal_questions": max(0, refusal_total - premise_correction_total),
            "premise_correction_questions": premise_correction_total,
            "no_evidence_questions": no_evidence_total,
            "infrastructure_failures": infra_failure_count,
            "positive_evidence_questions": pos_count,
            "refusal_evaluated_questions": refusal_total,
        },
        "metrics": {
            "overall_accuracy": overall_acc,
            "refusal_accuracy": refusal_acc,
            "grounded_recall_at_5": avg_recall_5,
            "precision_at_3": avg_precision_3,
            "mrr_at_5": avg_mrr_5,
            "ndcg_at_5": avg_ndcg_5,
            "llm_activation_rate": llm_act_rate,
            "no_evidence_metrics": {
                "false_answer_rate": false_answer_rate,
                "unsupported_citation_rate": unsupported_cit_rate,
            },
        },
        "query_latency_distribution_ms": {
            "min": round(min(all_latencies), 2),
            "max": round(max(all_latencies), 2),
            "mean": round(sum(all_latencies) / len(all_latencies), 2),
            "note": "Latency targets (<400ms fast / <4000ms full) are verified via dedicated backend/scripts/run_latency_harness.py (200 runs)",
        },
    }

    # Write report files to immutable directory
    report_dir = BACKEND_DIR / "reports" / commit_sha / timestamp
    report_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = report_dir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    details_path = report_dir / "query_details.json"
    with open(details_path, "w", encoding="utf-8") as f:
        json.dump(query_results, f, indent=2)

    summary_md_path = report_dir / "summary.md"
    summary_md = f"""# Canonical 60-Question Benchmark Report

- **Commit SHA**: `{commit_sha}`
- **Timestamp**: `{timestamp}`
- **Workspace**: `{workspace_id}`
- **Benchmark Type**: `{benchmark_type}`
- **Test Suite SHA-256**: `{suite_sha256}`
- **Corpus Manifest SHA-256**: `{corpus_manifest_sha256}`

## Question Breakdown & Truthful Denominators
- **Total Questions**: {total_q}
- **Answerable Retrieval Questions**: {pos_count}
- **Answerable Structured Aggregate Questions**: {structured_agg_total}
- **Refusal / No-Evidence Questions**: {no_evidence_total}
- **Premise Correction Questions**: {premise_correction_total}
- **Infrastructure Failures**: {infra_failure_count}

## Quality & Accuracy Metrics

| Metric | Measured | Target | Status |
| :--- | :--- | :--- | :--- |
| **Overall Accuracy** | {overall_acc * 100:.1f}% | > 80.0% | {'PASS' if overall_acc >= 0.80 else 'FAIL'} |
| **Refusal Accuracy** | {refusal_acc * 100:.1f}% | > 85.0% | {'PASS' if refusal_acc >= 0.85 else 'FAIL'} |
| **Grounded Recall@5** | {avg_recall_5 * 100:.1f}% | > 85.0% | {'PASS' if avg_recall_5 >= 0.85 else 'FAIL'} |
| **Precision@3** | {avg_precision_3 * 100:.1f}% | > 60.0% | {'PASS' if avg_precision_3 >= 0.60 else 'FAIL'} |
| **MRR@5** | {avg_mrr_5:.4f} | > 0.7500 | {'PASS' if avg_mrr_5 >= 0.75 else 'FAIL'} |
| **nDCG@5** | {avg_ndcg_5:.4f} | > 0.7500 | {'PASS' if avg_ndcg_5 >= 0.75 else 'FAIL'} |
| **LLM Activation Rate** | {llm_act_rate * 100:.1f}% | < 60.0% | {'PASS' if llm_act_rate <= 0.60 else 'FAIL'} |

> **Note on Latency**: Single 60-query pass provides directional latency only. Formal p95 warm/cold validation is performed via `backend/scripts/run_latency_harness.py`.
"""
    with open(summary_md_path, "w", encoding="utf-8") as f:
        f.write(summary_md)

    logger.info("Saved benchmark report to %s", report_dir)

    # Baseline comparison if baseline path provided
    if baseline_path:
        b_path = Path(baseline_path)
        if not b_path.exists():
            logger.warning("Baseline manifest not found at %s", baseline_path)
        else:
            with open(b_path, "r", encoding="utf-8") as f:
                baseline_manifest = json.load(f)
            manifest["baseline_commit_sha"] = baseline_manifest.get("commit_sha")
            cmp_res = compare_manifests(manifest, baseline_manifest)
            logger.info("=== Comparison Result against %s ===", baseline_path)
            logger.info("Status: %s", cmp_res["status"])
            if not cmp_res["comparable"]:
                for mismatch in cmp_res.get("mismatches", []):
                    logger.warning("  Mismatch: %s", mismatch)
            else:
                for k, v in cmp_res.get("deltas", {}).items():
                    logger.info("  %s: %+.4f", k, v)

    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Canonical 60-Question Evaluation Benchmark.")
    parser.add_argument("--workspace-id", default="ws_fresh_benchmark", help="Workspace ID to benchmark")
    parser.add_argument("--benchmark-type", choices=["cold", "warm"], default="cold", help="Cold or warm run")
    parser.add_argument("--baseline-path", default=None, help="Path to baseline manifest.json for comparison")
    parser.add_argument("--allow-dirty", action="store_true", help="Allow uncommitted worktree changes during development")
    parser.add_argument("--test-path", default="data/test.json", help="Path to test.json evaluation suite")

    args = parser.parse_args()
    res = run_benchmark(
        workspace_id=args.workspace_id,
        benchmark_type=args.benchmark_type,
        baseline_path=args.baseline_path,
        allow_dirty=args.allow_dirty,
        test_path=args.test_path,
    )
    print(json.dumps(res["metrics"], indent=2))
