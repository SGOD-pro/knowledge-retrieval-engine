raise RuntimeError("Obsolete runner. Use backend/scripts/run_canonical_60_benchmark.py")
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
import numpy as np
from dotenv import load_dotenv

backend_dir = Path(__file__).resolve().parent.parent
load_dotenv(backend_dir / ".env")
sys.path.insert(0, str(backend_dir / "src"))

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from services.langgraph_pipeline import pipeline
from services.evaluation.benchmark_scorer import (
    content_match as _content_match,
    compute_faithfulness as _faithfulness_score,
)

WORKSPACE_ID = "ws_38c1ac31"

E2E_TEST_CASES = [
    # ── 1. Academic PDF: Attention Is All You Need (1706.03762v7.pdf) ──
    {
        "id": "PDF-01",
        "category": "factual_lookup",
        "expected_path": "fast_path",
        "format": "pdf",
        "document": "1706.03762v7.pdf",
        "query": "What is the beam size used for WSJ?",
        "expected_answer": "beam size of 21 and α = 0.3 for both WSJ only and the semi-supervised setting",
        "is_out_of_corpus": False,
    },
    {
        "id": "PDF-02",
        "category": "factual_lookup",
        "expected_path": "fast_path",
        "format": "pdf",
        "document": "1706.03762v7.pdf",
        "query": "How many heads does Multi-Head Attention use in the base model?",
        "expected_answer": "8 parallel attention layers, or heads",
        "is_out_of_corpus": False,
    },
    {
        "id": "PDF-03",
        "category": "analytical_synthesis",
        "expected_path": "full_path",
        "format": "pdf",
        "document": "1706.03762v7.pdf",
        "query": "Explain how the attention mechanism differs between the encoder and decoder in the Transformer architecture.",
        "expected_answer": "In addition to the two sub-layers in each encoder layer, the decoder inserts a third sub-layer which performs multi-head attention over the output of the encoder stack",
        "is_out_of_corpus": False,
    },
    {
        "id": "PDF-04",
        "category": "analytical_synthesis",
        "expected_path": "full_path",
        "format": "pdf",
        "document": "1706.03762v7.pdf",
        "query": "Why does the Transformer use scaled dot-product attention instead of additive attention?",
        "expected_answer": "dot-product attention is much faster and more space-efficient in practice",
        "is_out_of_corpus": False,
    },
    # ── 2. Academic PDF: Fast and Linear Attention (2507.19595v3.pdf) ──
    {
        "id": "PDF-05",
        "category": "factual_lookup",
        "expected_path": "fast_path",
        "format": "pdf",
        "document": "2507.19595v3.pdf",
        "query": "What is Block-Sparse Attention?",
        "expected_answer": "the long sequence is divided into blocks and attention is computed within or between selected blocks",
        "is_out_of_corpus": False,
    },
    {
        "id": "PDF-06",
        "category": "analytical_synthesis",
        "expected_path": "full_path",
        "format": "pdf",
        "document": "2507.19595v3.pdf",
        "query": "What are the bottlenecks of Element-wise Linear Attention for long sequences?",
        "expected_answer": "it suffers from the bottleneck of state size and limited memory capacity",
        "is_out_of_corpus": False,
    },
    # ── 3. Academic PDF: Attention Heads Analysis (2204.13154v1.pdf) ──
    {
        "id": "PDF-07",
        "category": "factual_lookup",
        "expected_path": "fast_path",
        "format": "pdf",
        "document": "2204.13154v1.pdf",
        "query": "What does the study examine regarding individual attention heads?",
        "expected_answer": "examines the contribution made by individual attention heads in the encoder",
        "is_out_of_corpus": False,
    },
    # ── 4. Non-PDF Formats: Tabular CSV (Govt_Colleges_TeachingStaff_Position_2024_25_0.csv) ──
    {
        "id": "CSV-01",
        "category": "factual_lookup",
        "expected_path": "fast_path",
        "format": "csv",
        "document": "Govt_Colleges_TeachingStaff_Position_2024_25_0.csv",
        "query": "How many Assistant Professors are at GHMC, Bangalore?",
        "expected_answer": "Assistant Professor: 20",
        "is_out_of_corpus": False,
    },
    {
        "id": "CSV-02",
        "category": "factual_lookup",
        "expected_path": "fast_path",
        "format": "csv",
        "document": "Govt_Colleges_TeachingStaff_Position_2024_25_0.csv",
        "query": "How many Professors are reported at GAMC, Bangalore?",
        "expected_answer": "Professor: 23",
        "is_out_of_corpus": False,
    },
    # ── 5. Non-PDF Formats: DOCX (Workflow Documentation.docx) ──
    {
        "id": "DOCX-01",
        "category": "factual_lookup",
        "expected_path": "fast_path",
        "format": "docx",
        "document": "Workflow Documentation.docx",
        "query": "What frontend technologies are used in the technology stack?",
        "expected_answer": "Next.js 16, React, Tailwind CSS",
        "is_out_of_corpus": False,
    },
    {
        "id": "DOCX-02",
        "category": "factual_lookup",
        "expected_path": "fast_path",
        "format": "docx",
        "document": "Workflow Documentation.docx",
        "query": "What are the 3 D's in the implementation plan?",
        "expected_answer": "Design, Development and Documentation",
        "is_out_of_corpus": False,
    },
    {
        "id": "DOCX-03",
        "category": "analytical_synthesis",
        "expected_path": "full_path",
        "format": "docx",
        "document": "Workflow Documentation.docx",
        "query": "Explain how CodeCouncil solves the problem of lost session context.",
        "expected_answer": "autonomous agent that watches, remembers, and intervenes proactively",
        "is_out_of_corpus": False,
    },
    # ── 6. Non-PDF Formats: PPTX (submission.pptx) ──
    {
        "id": "PPTX-01",
        "category": "analytical_synthesis",
        "expected_path": "full_path",
        "format": "pptx",
        "document": "submission.pptx",
        "query": "Explain what WB Digital Sahayak is designed to assist with.",
        "expected_answer": "calculating eligibility deterministically and providing a Readiness Score",
        "is_out_of_corpus": False,
    },
    {
        "id": "PPTX-02",
        "category": "factual_lookup",
        "expected_path": "fast_path",
        "format": "pptx",
        "document": "submission.pptx",
        "query": "What is the Readiness Score feature in WB Digital Sahayak?",
        "expected_answer": "Readiness Score (0–100%)",
        "is_out_of_corpus": False,
    },
    # ── 7. Non-PDF Formats: XLSX (sample.xlsx) ──
    {
        "id": "XLSX-01",
        "category": "factual_lookup",
        "expected_path": "fast_path",
        "format": "xlsx",
        "document": "sample.xlsx",
        "query": "What is the value of Revenue in sample.xlsx?",
        "expected_answer": "100",
        "is_out_of_corpus": False,
    },
    # ── 8. Out-of-Corpus / Negative Cases (Abstention & Anti-Hallucination Gate) ──
    {
        "id": "NEG-01",
        "category": "abstention_negative",
        "expected_path": "fast_path_or_full",
        "format": "negative",
        "document": "none",
        "query": "What was the total quarterly revenue of Tesla Motors in Q4 2025?",
        "expected_answer": "NOT_FOUND",
        "is_out_of_corpus": True,
    },
    {
        "id": "NEG-02",
        "category": "abstention_negative",
        "expected_path": "fast_path_or_full",
        "format": "negative",
        "document": "none",
        "query": "What are the eligibility criteria for the Canadian Express Entry immigration points system?",
        "expected_answer": "NOT_FOUND",
        "is_out_of_corpus": True,
    },
]

def main():
    print("=" * 95)
    print("=== FULL E2E HYBRID ARCHITECTURE BENCHMARK & QUALITY EVALUATION ===")
    print(f"=== Workspace: {WORKSPACE_ID} | Active Corpus: Multi-Format (PDF, DOCX, PPTX, XLSX, CSV) ===")
    print("=" * 95)

    results = []
    latencies = []
    fast_path_latencies = []
    full_path_latencies = []
    
    hits_at_5 = 0
    hits_at_3 = 0
    mrr_scores = []
    faithfulness_scores = []
    
    routing_correct = 0
    total_in_corpus = 0
    abstention_correct = 0
    total_out_of_corpus = 0
    
    fast_path_count = 0
    full_path_count = 0
    hallucination_count = 0

    for i, test in enumerate(E2E_TEST_CASES, 1):
        qid = test["id"]
        query = test["query"]
        doc = test["document"]
        fmt = test["format"]
        expected_ans = test["expected_answer"]
        is_ooc = test["is_out_of_corpus"]
        
        t0 = time.perf_counter()
        try:
            res = pipeline.run(query=query, workspace_id=WORKSPACE_ID)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            latencies.append(elapsed_ms)
            
            fast_path = getattr(res, "fast_path", False)
            if fast_path:
                fast_path_count += 1
                fast_path_latencies.append(elapsed_ms)
            else:
                full_path_count += 1
                full_path_latencies.append(elapsed_ms)
                
            ans = getattr(res, "answer", "") or ""
            citations = getattr(res, "citations", []) or []
            top_chunks = getattr(res, "top_chunks", []) or []
            timings = getattr(res, "stage_timings", {}) or {}
            conf = getattr(res, "confidence_score", 0.0) or 0.0

            # 1. Routing validation
            if not is_ooc:
                total_in_corpus += 1
                if (test["expected_path"] == "fast_path" and fast_path) or \
                   (test["expected_path"] == "full_path" and not fast_path):
                    routing_correct += 1
            else:
                total_out_of_corpus += 1

            # 2. Retrieval Evaluation (Recall & MRR)
            h5 = 0
            h3 = 0
            rr = 0.0
            
            if not is_ooc:
                # Top-5 text
                top5_text = " ".join(c.text for c in top_chunks[:5])
                top3_text = " ".join(c.text for c in top_chunks[:3])
                
                # Check hits
                if _content_match(top5_text, expected_ans) or (
                    expected_ans.lower() in ans.lower() and ans not in ("NOT_FOUND", "")
                ):
                    h5 = 1
                    hits_at_5 += 1
                
                if _content_match(top3_text, expected_ans) or (
                    expected_ans.lower() in ans.lower() and ans not in ("NOT_FOUND", "")
                ):
                    h3 = 1
                    hits_at_3 += 1
                
                # Reciprocal Rank
                for rank, c in enumerate(top_chunks[:5], 1):
                    if _content_match(c.text, expected_ans) or (
                        expected_ans.lower() in c.text.lower()
                    ):
                        rr = 1.0 / rank
                        break
                mrr_scores.append(rr)

            # 3. Faithfulness & Answer Quality
            context_text = " ".join(c.text for c in top_chunks[:5])
            faith = _faithfulness_score(ans, context_text)
            
            # 4. Abstention check for Negative / Out-of-Corpus cases
            is_abstention = ans in ("NOT_FOUND", "", "The context does not provide") or "not found" in ans.lower()
            if is_ooc:
                if is_abstention:
                    abstention_correct += 1
                else:
                    hallucination_count += 1
            elif not is_abstention and faith is not None:
                faithfulness_scores.append(faith)

            # Status symbols
            if is_ooc:
                status_sym = "✓ PASS (ABSTAIN)" if is_abstention else "✗ FAIL (HALLUCINATION)"
            else:
                status_sym = "✓ PASS" if h5 == 1 else "✗ MISS"
                
            path_str = "FAST (No LLM)" if fast_path else "FULL (LLM)"
            faith_str = f"{faith:.2f}" if faith is not None else "N/A"
            
            print(f"[{i:02d}/{len(E2E_TEST_CASES)}] {status_sym:<18} | {qid:<7} | Format={fmt:<4} | Path={path_str:<14} | Latency={elapsed_ms:5.0f}ms | Conf={conf:.2f} | Faith={faith_str}")
            print(f"     Q: {query}")
            ans_preview = ans.replace('\n', ' ')[:100] + ('...' if len(ans) > 100 else '')
            print(f"     A: {ans_preview}")
            if citations:
                c0 = citations[0]
                loc = c0.get('location_reference') or f"Page {c0.get('page_number')}"
                print(f"     Citation [1/{len(citations)}]: {c0.get('document_filename')} ({loc})")
            print()

            results.append({
                "id": qid,
                "category": test["category"],
                "format": fmt,
                "document": doc,
                "query": query,
                "expected_answer": expected_ans,
                "actual_answer": ans,
                "fast_path": fast_path,
                "confidence_score": conf,
                "hit_at_5": h5,
                "hit_at_3": h3,
                "reciprocal_rank": rr,
                "faithfulness": faith,
                "latency_ms": elapsed_ms,
                "citations_count": len(citations),
                "citations": citations[:3],
                "stage_timings": timings,
                "is_out_of_corpus": is_ooc,
                "passed": (is_abstention if is_ooc else (h5 == 1)),
            })

        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            print(f"[{i:02d}/{len(E2E_TEST_CASES)}] ERROR on {qid}: {e}")
            results.append({
                "id": qid,
                "query": query,
                "error": str(e),
                "latency_ms": elapsed_ms,
                "passed": False,
            })

    # Summary Calculations
    total_tests = len(E2E_TEST_CASES)
    rec_at_5 = hits_at_5 / total_in_corpus if total_in_corpus else 0.0
    rec_at_3 = hits_at_3 / total_in_corpus if total_in_corpus else 0.0
    mean_mrr = np.mean(mrr_scores) if mrr_scores else 0.0
    mean_faith = np.mean(faithfulness_scores) if faithfulness_scores else 0.0
    
    mean_lat = np.mean(latencies) if latencies else 0.0
    median_lat = np.median(latencies) if latencies else 0.0
    p95_lat = np.percentile(latencies, 95) if latencies else 0.0
    
    fast_mean_lat = np.mean(fast_path_latencies) if fast_path_latencies else 0.0
    fast_p95_lat = np.percentile(fast_path_latencies, 95) if fast_path_latencies else 0.0
    
    full_mean_lat = np.mean(full_path_latencies) if full_path_latencies else 0.0
    full_p95_lat = np.percentile(full_path_latencies, 95) if full_path_latencies else 0.0

    abstention_accuracy = abstention_correct / total_out_of_corpus if total_out_of_corpus else 1.0
    hallucination_rate = hallucination_count / total_tests

    print("=" * 95)
    print("=== HYBRID ARCHITECTURE E2E BENCHMARK REPORT METRICS ===")
    print("=" * 95)
    print(f"Total Test Cases:                  {total_tests} (In-Corpus: {total_in_corpus}, Out-of-Corpus: {total_out_of_corpus})")
    print(f"Recall@5 (In-Corpus):              {rec_at_5:.4f} ({hits_at_5}/{total_in_corpus} = {rec_at_5*100:.1f}%)")
    print(f"Recall@3 (In-Corpus):              {rec_at_3:.4f} ({hits_at_3}/{total_in_corpus} = {rec_at_3*100:.1f}%)")
    print(f"MRR@5 (Mean Reciprocal Rank):      {mean_mrr:.4f}")
    print(f"Real-Answer Faithfulness:          {mean_faith:.4f} (grounded claim ratio on valid answers)")
    print(f"Hallucination Rate:                {hallucination_rate*100:.1f}% ({hallucination_count}/{total_tests})")
    print(f"Abstention Accuracy (OOC):         {abstention_accuracy*100:.1f}% ({abstention_correct}/{total_out_of_corpus})")
    print("-" * 95)
    print(f"Fast Path Rate:                    {fast_path_count/total_tests*100:.1f}% ({fast_path_count}/{total_tests}) [Zero LLM calls, extractive]")
    print(f"Full Path Rate (LLM Activation):   {full_path_count/total_tests*100:.1f}% ({full_path_count}/{total_tests}) [Bedrock Nova Lite synthesis]")
    print(f"Fast Path Mean Latency:            {fast_mean_lat:.0f} ms (p95: {fast_p95_lat:.0f} ms)")
    print(f"Full Path Mean Latency:            {full_mean_lat:.0f} ms (p95: {full_p95_lat:.0f} ms)")
    print(f"Overall Median Latency:            {median_lat:.0f} ms (Mean: {mean_lat:.0f} ms, p95: {p95_lat:.0f} ms)")
    print("=" * 95)

    # Save output JSON
    tmp_dir = backend_dir / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    out_file = tmp_dir / "e2e_benchmark_report.json"
    
    report_data = {
        "summary": {
            "total_test_cases": total_tests,
            "in_corpus_count": total_in_corpus,
            "out_of_corpus_count": total_out_of_corpus,
            "recall_at_5": float(rec_at_5),
            "recall_at_3": float(rec_at_3),
            "mrr_at_5": float(mean_mrr),
            "real_answer_faithfulness": float(mean_faith),
            "hallucination_rate": float(hallucination_rate),
            "abstention_accuracy": float(abstention_accuracy),
            "fast_path_activation_rate": float(fast_path_count / total_tests),
            "llm_activation_rate": float(full_path_count / total_tests),
            "fast_path_mean_latency_ms": float(fast_mean_lat),
            "fast_path_p95_latency_ms": float(fast_p95_lat),
            "full_path_mean_latency_ms": float(full_mean_lat),
            "full_path_p95_latency_ms": float(full_p95_lat),
            "overall_mean_latency_ms": float(mean_lat),
            "overall_median_latency_ms": float(median_lat),
            "overall_p95_latency_ms": float(p95_lat),
        },
        "test_cases": results,
    }
    
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)
    print(f"\nSaved detailed JSON benchmark report to: {out_file}\n")

if __name__ == "__main__":
    main()
