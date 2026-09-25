#!/usr/bin/env python3
"""Independent Evidence Retrieval Strategy Evaluation Harness.

Runs each evidence retrieval strategy independently on canonical benchmark questions,
measures retrieval quality without calling answer generation, isolates no-evidence
questions from Recall@k denominators, and outputs structured per-question metrics.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

# Ensure backend/src is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = BASE_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from db.database import CloudRepository
from services.retrieval.evidence_contract import (
    EvidenceItem,
    RetrievalLimits,
    RetrievalStrategyResult,
)
from services.retrieval.strategies.base import RetrievalStrategy
from services.retrieval.strategies.bm25 import LexicalBM25Strategy
from services.retrieval.strategies.knowledge_graph import KnowledgeGraphStrategy
from services.retrieval.strategies.okf import OKFStrategy
from services.retrieval.strategies.page_index import PageIndexStrategy
from services.retrieval.strategies.structured_table import StructuredTableStrategy
from services.retrieval.strategies.vector_rerank import VectorRerankStrategy
from services.telemetry import init_request_telemetry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("evaluate_retrieval_strategies")


def compute_retrieval_metrics(
    retrieved_ids: list[str],
    gt_ids: set[str],
) -> dict[str, float]:
    """Compute standard IR metrics: Recall@5, Precision@3, MRR@5, NDCG@5."""
    if not gt_ids:
        return {"recall_at_5": 0.0, "precision_at_3": 0.0, "mrr_at_5": 0.0, "ndcg_at_5": 0.0}

    # Recall@5
    top5_set = set(retrieved_ids[:5])
    recall_at_5 = len(top5_set & gt_ids) / len(gt_ids)

    # Precision@3
    top3 = retrieved_ids[:3]
    precision_at_3 = len(set(top3) & gt_ids) / min(3, len(top3)) if top3 else 0.0

    # MRR@5
    mrr_at_5 = 0.0
    for rank, rid in enumerate(retrieved_ids[:5], start=1):
        if rid in gt_ids:
            mrr_at_5 = 1.0 / rank
            break

    # NDCG@5
    dcg = 0.0
    for rank, rid in enumerate(retrieved_ids[:5], start=1):
        rel = 1.0 if rid in gt_ids else 0.0
        dcg += rel / math.log2(rank + 1)

    idcg = 0.0
    ideal_count = min(len(gt_ids), 5)
    for rank in range(1, ideal_count + 1):
        idcg += 1.0 / math.log2(rank + 1)

    ndcg_at_5 = (dcg / idcg) if idcg > 0.0 else 0.0

    return {
        "recall_at_5": round(recall_at_5, 4),
        "precision_at_3": round(precision_at_3, 4),
        "mrr_at_5": round(mrr_at_5, 4),
        "ndcg_at_5": round(ndcg_at_5, 4),
    }


async def evaluate_question_on_strategy(
    question_id: str,
    question_text: str,
    strategy: RetrievalStrategy,
    workspace_id: str,
    gt_evidence_ids: set[str],
    is_no_evidence_question: bool,
    limits: RetrievalLimits | None = None,
) -> dict[str, Any]:
    """Execute strategy independently on one question and score without answer generation."""
    telemetry = init_request_telemetry()
    limits = limits or RetrievalLimits(top_k=10)

    try:
        result: RetrievalStrategyResult = await strategy.retrieve(
            query=question_text,
            workspace_id=workspace_id,
            limits=limits,
            telemetry=telemetry,
        )
    except Exception as exc:
        logger.warning(
            "Strategy %s failed on question %s: %s",
            strategy.name,
            question_id,
            exc,
        )
        return {
            "question_id": question_id,
            "strategy": strategy.name,
            "retrieved_evidence_ids": [],
            "ground_truth_evidence_ids": list(gt_evidence_ids),
            "recall_at_5": None if is_no_evidence_question else 0.0,
            "precision_at_3": None if is_no_evidence_question else 0.0,
            "mrr_at_5": None if is_no_evidence_question else 0.0,
            "ndcg_at_5": None if is_no_evidence_question else 0.0,
            "latency_ms": 0.0,
            "remote_calls": {},
            "failure_reason": f"exception: {exc}",
            "refusal_candidate_detected": True if is_no_evidence_question else False,
            "unsupported_evidence_returned": False,
            "false_premise_correction_support": False,
        }

    # Extract retrieved IDs
    retrieved_evidence_ids: list[str] = []
    for item in result.items:
        retrieved_evidence_ids.append(item.evidence_id)
        # Also check chunk_id in locator
        cid = item.locator.get("chunk_id")
        if cid and cid not in retrieved_evidence_ids:
            retrieved_evidence_ids.append(str(cid))
        # If structured table
        tid = item.locator.get("table_id")
        if tid and tid not in retrieved_evidence_ids:
            retrieved_evidence_ids.append(str(tid))

    if is_no_evidence_question:
        # Evaluate refusal and false premise separately
        refusal_candidate = (len(result.items) == 0 or result.confidence < 0.3 or result.failure_reason is not None)
        unsupported_returned = len(result.items) > 0 and not refusal_candidate
        # For false premise questions: does retrieved evidence mention relevant entities?
        false_premise_support = len(result.items) > 0

        return {
            "question_id": question_id,
            "strategy": strategy.name,
            "retrieved_evidence_ids": retrieved_evidence_ids,
            "ground_truth_evidence_ids": [],
            "recall_at_5": None,
            "precision_at_3": None,
            "mrr_at_5": None,
            "ndcg_at_5": None,
            "latency_ms": result.latency_ms,
            "remote_calls": result.remote_call_counts,
            "failure_reason": result.failure_reason,
            "refusal_candidate_detected": refusal_candidate,
            "unsupported_evidence_returned": unsupported_returned,
            "false_premise_correction_support": false_premise_support,
        }

    metrics = compute_retrieval_metrics(retrieved_evidence_ids, gt_evidence_ids)

    return {
        "question_id": question_id,
        "strategy": strategy.name,
        "retrieved_evidence_ids": retrieved_evidence_ids,
        "ground_truth_evidence_ids": list(gt_evidence_ids),
        "recall_at_5": metrics["recall_at_5"],
        "precision_at_3": metrics["precision_at_3"],
        "mrr_at_5": metrics["mrr_at_5"],
        "ndcg_at_5": metrics["ndcg_at_5"],
        "latency_ms": result.latency_ms,
        "remote_calls": result.remote_call_counts,
        "failure_reason": result.failure_reason,
    }


def resolve_ground_truth(
    gt_entries: dict[str, Any],
    all_chunks: list[Any],
    repo: CloudRepository,
    workspace_id: str,
) -> tuple[dict[str, set[str]], set[str]]:
    """Resolve ground truth items into sets of evidence IDs."""
    doc_fname_to_id = {}
    try:
        docs = repo.list_documents(workspace_id) if hasattr(repo, "list_documents") else []
        for d in docs:
            doc_fname_to_id[d.filename] = str(d.id)
    except Exception:
        pass

    resolved_gt: dict[str, set[str]] = {}
    no_evidence_qids: set[str] = set()

    for qid, entry in gt_entries.items():
        ev_list = entry.get("relevant_evidence", [])
        if not ev_list:
            resolved_gt[qid] = set()
            no_evidence_qids.add(qid)
            continue

        matched_ids: set[str] = set()
        for ev in ev_list:
            fname = ev.get("source_filename", "")
            loc_type = ev.get("locator_type")
            loc_val = str(ev.get("locator", ""))
            target_did = doc_fname_to_id.get(fname)

            if loc_type in ("csv_column_aggregate", "table_query") or fname.endswith(".csv"):
                # Structured table evidence
                table_id = fname.replace(".csv", "")
                matched_ids.add(table_id)
                matched_ids.add(f"table_{table_id}")

            for c in all_chunks:
                # Match by document if known, or match by metadata filename
                c_fname = (getattr(c, "metadata", {}) or {}).get("filename") or ""
                c_did = str(getattr(c, "document_id", ""))
                if target_did and c_did != target_did and c_fname != fname:
                    continue

                if loc_type == "pdf_page":
                    try:
                        if getattr(c, "page_number", None) == int(loc_val):
                            matched_ids.add(str(c.id))
                    except (ValueError, TypeError):
                        pass
                elif loc_type in ("csv_row", "spreadsheet_row"):
                    meta_row = (getattr(c, "metadata", {}) or {}).get("row")
                    if meta_row is not None and str(meta_row) == loc_val:
                        matched_ids.add(str(c.id))
                    elif c.location_reference and f"Row: {loc_val}" in c.location_reference:
                        matched_ids.add(str(c.id))

        if not matched_ids:
            # If no chunk matched specifically, but relevant_evidence exists, fall back to matching on filename or locators
            for ev in ev_list:
                fname = ev.get("source_filename", "")
                if fname:
                    matched_ids.add(fname)
                    matched_ids.add(fname.split(".")[0])

        resolved_gt[qid] = matched_ids

    return resolved_gt, no_evidence_qids


def get_git_commit() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(BASE_DIR)).decode("utf-8").strip()
        return out
    except Exception:
        return "unknown_commit"


async def main_async():
    parser = argparse.ArgumentParser(description="Evaluate retrieval strategies independently.")
    parser.add_argument("--workspace-id", default="ws_fresh_benchmark", help="Workspace ID")
    parser.add_argument("--benchmark-file", default="data/test.json", help="Path to benchmark test.json")
    parser.add_argument("--ground-truth", default="evaluation_assets/canonical_60_ground_truth.json", help="Path to ground truth JSON")
    parser.add_argument("--output-dir", default=None, help="Directory to save evaluation results")
    parser.add_argument("--strategies", nargs="+", default=None, help="Specific strategies to run")

    args = parser.parse_args()

    # Locate files relative to BASE_DIR if not absolute
    bench_path = Path(args.benchmark_file)
    if not bench_path.is_absolute():
        bench_path = BASE_DIR / bench_path
        if not bench_path.exists():
            bench_path = BASE_DIR.parent / args.benchmark_file

    gt_path = Path(args.ground_truth)
    if not gt_path.is_absolute():
        gt_path = BASE_DIR / gt_path

    if not bench_path.exists():
        logger.error("Benchmark file not found: %s", bench_path)
        sys.exit(1)
    if not gt_path.exists():
        logger.error("Ground truth file not found: %s", gt_path)
        sys.exit(1)

    with open(bench_path, "r", encoding="utf-8") as f:
        bench_data = json.load(f)

    with open(gt_path, "r", encoding="utf-8") as f:
        gt_data = json.load(f)

    questions = bench_data.get("questions", bench_data if isinstance(bench_data, list) else [])
    if isinstance(bench_data, dict) and "questions" not in bench_data:
        questions = list(bench_data.values())

    logger.info("Loaded %d benchmark questions.", len(questions))

    # Output directory
    commit_sha = get_git_commit()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        out_dir = BASE_DIR / "reports" / "strategy_evaluation" / commit_sha / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)

    repo = CloudRepository()
    all_chunks = repo.get_all_chunks(workspace_id=args.workspace_id)
    logger.info("Loaded %d workspace chunks for ground truth mapping.", len(all_chunks))

    resolved_gt, no_evidence_qids = resolve_ground_truth(
        gt_entries=gt_data,
        all_chunks=all_chunks,
        repo=repo,
        workspace_id=args.workspace_id,
    )

    available_strategies: dict[str, RetrievalStrategy] = {
        "vector_rerank": VectorRerankStrategy(repository=repo),
        "bm25": LexicalBM25Strategy(repository=repo),
        "structured_table": StructuredTableStrategy(),
        "page_index": PageIndexStrategy(repository=repo),
        "knowledge_graph": KnowledgeGraphStrategy(repository=repo),
        "okf": OKFStrategy(repository=repo),
    }

    selected_strategies = args.strategies or list(available_strategies.keys())

    all_results: list[dict[str, Any]] = []

    for strat_name in selected_strategies:
        if strat_name not in available_strategies:
            logger.warning("Unknown strategy %s, skipping.", strat_name)
            continue

        strategy = available_strategies[strat_name]
        logger.info("=== Running Strategy: %s ===", strat_name)

        strat_results = []
        for q in questions:
            qid = q.get("id") or q.get("question_id")
            qtext = q.get("question") or q.get("text")
            category = q.get("category", "UNKNOWN")

            gt_ids = resolved_gt.get(qid, set())
            is_no_ev = qid in no_evidence_qids

            eval_res = await evaluate_question_on_strategy(
                question_id=qid,
                question_text=qtext,
                strategy=strategy,
                workspace_id=args.workspace_id,
                gt_evidence_ids=gt_ids,
                is_no_evidence_question=is_no_ev,
            )
            eval_res["category"] = category
            strat_results.append(eval_res)
            all_results.append(eval_res)

        # Compute summary for strategy
        scored_qs = [r for r in strat_results if r["recall_at_5"] is not None]
        avg_recall = sum(r["recall_at_5"] for r in scored_qs) / len(scored_qs) if scored_qs else 0.0
        avg_prec = sum(r["precision_at_3"] for r in scored_qs) / len(scored_qs) if scored_qs else 0.0
        avg_mrr = sum(r["mrr_at_5"] for r in scored_qs) / len(scored_qs) if scored_qs else 0.0
        avg_lat = sum(r["latency_ms"] for r in strat_results) / len(strat_results) if strat_results else 0.0

        logger.info(
            "Strategy %s: Recall@5=%.4f, Precision@3=%.4f, MRR@5=%.4f, Latency=%.1fms (on %d positive-evidence qs)",
            strat_name,
            avg_recall,
            avg_prec,
            avg_mrr,
            avg_lat,
            len(scored_qs),
        )

    # Save complete evaluation JSON
    eval_json_path = out_dir / "evaluation_results.json"
    with open(eval_json_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)
    logger.info("Evaluation results saved to %s", eval_json_path)

    # Generate and save strategy_matrix.md report
    from services.evaluation.strategy_report_generator import generate_strategy_matrix_markdown
    report_md = generate_strategy_matrix_markdown(all_results, commit_sha=commit_sha, timestamp=timestamp)
    report_md_path = out_dir / "strategy_matrix.md"
    with open(report_md_path, "w", encoding="utf-8") as f:
        f.write(report_md)
    logger.info("Strategy matrix report saved to %s", report_md_path)

    return out_dir, all_results


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
