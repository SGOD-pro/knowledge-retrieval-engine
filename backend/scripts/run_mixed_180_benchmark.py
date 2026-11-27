#!/usr/bin/env python3
"""Mixed-corpus 180-question benchmark runner preserving gold labels evaluator-side."""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
SRC_DIR = BACKEND_DIR / "src"
ROOT_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(BACKEND_DIR))

from config import settings
from db.workspace_boundary_repo import get_workspace_boundary_repo
from modules.query.query_repository import QueryRepository
from modules.query.query_service import QueryService
from schemas.models import QueryRequest
from services.evaluation.mixed_180_evaluator import (
    grade_180_record,
    load_engine_requests,
    resolve_180_locators,
    validate_180_dataset,
)
from services.retrieval.bm25_retriever import invalidate_chunk_cache


def main() -> None:
    parser = argparse.ArgumentParser(description="Run mixed-corpus 180-question benchmark.")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--shuffle-seed", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--staged-dir", default=None)
    args = parser.parse_args()

    q_only_p = ROOT_DIR / "data" / "kre_questions_only_180.json"
    bench_p = ROOT_DIR / "data" / "kre_benchmark_180.json"
    docs_p = ROOT_DIR / "data" / "kre_shuffled_documents.json"

    val_summary = validate_180_dataset(
        q_only_p, bench_p, docs_p,
        staged_dir=Path(args.staged_dir) if args.staged_dir else None,
    )
    engine_reqs = load_engine_requests(q_only_p)
    bench_by_id = {q["id"]: q for q in json.loads(bench_p.read_text(encoding="utf-8"))["questions"]}

    if args.shuffle_seed is not None:
        random.Random(args.shuffle_seed).shuffle(engine_reqs)
    if args.limit:
        engine_reqs = engine_reqs[: args.limit]

    invalidate_chunk_cache()
    b_repo = get_workspace_boundary_repo()
    snaps = {s.source: s for s in b_repo.list_document_snapshots(args.workspace_id)}
    repo = QueryRepository()
    chunks = repo.get_all_chunks(workspace_id=args.workspace_id)
    svc = QueryService(repo=repo)

    results = []
    for idx, req_item in enumerate(engine_reqs, 1):
        qid = req_item["id"]
        t0 = time.perf_counter()
        resp = svc.execute_query(QueryRequest(query=req_item["question"], workspace_id=args.workspace_id, cache=False, benchmark_mode=True))
        ms = round((time.perf_counter() - t0) * 1000.0, 2)
        b_item = bench_by_id[qid]
        resolved = resolve_180_locators(b_item.get("expected_sources") or [], chunks, snaps)
        grade = grade_180_record(b_item, resp, resolved)
        grade["latency_ms"] = ms
        grade["executed_path"] = resp.get("executed_path")
        results.append(grade)

    scorable = [r for r in results if r["scorable"]]
    summary = {
        "workspace_id": args.workspace_id,
        "total": len(results),
        "scorable": len(scorable),
        "unscorable": len(results) - len(scorable),
        "answer_accuracy": round(sum(1 for r in scorable if r["answer_correct"]) / max(1, len(scorable)), 4),
        "grounded_accuracy": round(sum(1 for r in scorable if r["grounded_correct"]) / max(1, len(scorable)), 4),
        "validation": val_summary,
        "results": results,
    }
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))


if __name__ == "__main__":
    main()
