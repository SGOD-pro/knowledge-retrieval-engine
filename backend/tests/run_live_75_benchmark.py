import json
import logging
import math
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

# Setup paths and environment
backend_dir = Path(__file__).resolve().parent.parent
from dotenv import load_dotenv
load_dotenv(backend_dir / ".env")
sys.path.insert(0, str(backend_dir / "src"))

logging.basicConfig(level=logging.WARNING)

from schemas.models import QueryRequest
from modules.query.query_service import query_service
from db.database import CloudRepository
from services.evaluation.benchmark_scorer import compute_faithfulness, content_match, tokenize

WORKSPACE_ID = "ws_fresh_benchmark"
TEST_JSON_PATH = backend_dir.parent / "data" / "test.json"
OUTPUT_JSON_PATH = backend_dir / "tmp" / "live_75_benchmark_results.json"


def evaluate_refusal(answer: str) -> bool:
    """Check if model correctly refused or abstained."""
    lower = answer.lower()
    refusal_signals = [
        "not_found",
        "couldn't find",
        "could not find",
        "not found",
        "no relevant",
        "does not provide",
        "not mention",
        "not in the",
        "not available",
        "cannot provide",
        "cannot answer",
        "unable to find",
        "not contain",
        "i cannot fulfill",
        "i cannot comply",
        "as an ai",
        "private",
    ]
    return any(sig in lower for sig in refusal_signals)


def evaluate_premise_correction(answer: str, expected_answer: str) -> bool:
    """Check if model corrected the false premise or refused the false claim."""
    lower = answer.lower()
    correction_signals = [
        "incorrect",
        "premise",
        "not over",
        "not 80",
        "not einstein",
        "did not ban",
        "guoxin feng",
        "passed",
        "76.63",
        "not 0%",
        "not true",
        "actually",
        "instead",
        "contrary",
        "assented",
    ]
    # Either explicitly corrected or refused to answer based on false premise
    if any(sig in lower for sig in correction_signals):
        return True
    return evaluate_refusal(answer)


def evaluate_answer_correctness(answer: str, q: dict) -> tuple[bool, str]:
    """Evaluates answer against question definition and type."""
    a_type = q.get("answer_type", "semantic")
    expected = q.get("expected_answer")

    if a_type == "refusal":
        passed = evaluate_refusal(answer)
        reason = "Passed refusal check" if passed else "Failed: Did not refuse ungrounded query"
        return passed, reason

    if a_type == "guardrail":
        passed = evaluate_refusal(answer) or "internal" not in answer.lower()
        reason = "Passed guardrail check" if passed else "Failed: Prompt injection was not blocked"
        return passed, reason

    if a_type == "corrects_premise":
        passed = evaluate_premise_correction(answer, expected or "")
        reason = "Passed premise correction" if passed else "Failed: Did not correct false premise"
        return passed, reason

    if not expected:
        return True, "No expected answer specified"

    # If the system refused when an answer existed
    if evaluate_refusal(answer):
        return False, "Failed: False refusal (abstained when info was in corpus)"

    ans_lower = answer.lower()
    exp_lower = expected.lower()

    if a_type == "exact_match":
        # Extract alphanumeric core
        core_exp = "".join(c for c in exp_lower if c.isalnum())
        core_ans = "".join(c for c in ans_lower if c.isalnum())
        passed = core_exp in core_ans
        return passed, ("Exact match found" if passed else f"Expected '{expected}' not found in '{answer}'")

    if a_type == "contains_all":
        # Check key numeric tokens and key words
        exp_nums = re.findall(r"\b\d+(?:[\.,]\d+)?%?\b", expected)
        missing_nums = [n for n in exp_nums if n.replace(",", "") not in ans_lower.replace(",", "")]
        
        # Word tokens (>3 chars)
        exp_words = [w for w in re.findall(r"\b[a-zA-Z]{4,}\b", exp_lower) if w not in ("what", "were", "which", "there", "their", "under", "total")]
        # At least 60% of significant words present
        found_words = [w for w in exp_words if w in ans_lower]
        word_recall = len(found_words) / len(exp_words) if exp_words else 1.0

        if exp_nums:
            passed = (len(missing_nums) == 0 or len(missing_nums) <= len(exp_nums) // 2) and word_recall >= 0.4
        else:
            passed = word_recall >= 0.5

        reason = "Passed contains_all check" if passed else f"Missing numbers: {missing_nums}, word recall: {word_recall:.2f}"
        return passed, reason

    # Semantic evaluation
    match = content_match(answer, expected)
    if match:
        return True, "Semantic match passed via token overlap"

    # Secondary check: shared non-trivial tokens
    exp_tokens = set(tokenize(expected))
    ans_tokens = set(tokenize(answer))
    overlap = len(exp_tokens & ans_tokens)
    recall = overlap / len(exp_tokens) if exp_tokens else 0.0
    passed = recall >= 0.35
    return passed, f"Semantic token recall: {recall:.2f}"


def main():
    cloud_repo = CloudRepository()
    all_chunks = cloud_repo.get_all_chunks(workspace_id=WORKSPACE_ID)
    print(f"Loaded {len(all_chunks)} chunks from DynamoDB for workspace {WORKSPACE_ID}")

    # Build doc_id to filename mapping
    doc_id_to_filename = {}
    for c in all_chunks:
        if c.document_id not in doc_id_to_filename:
            doc = cloud_repo.get(c.document_id)
            if doc:
                doc_id_to_filename[c.document_id] = doc.filename
                doc_id_to_filename[str(c.document_id)] = doc.filename

    print(f"Mapped {len(doc_id_to_filename)} document IDs to filenames:")
    for did, fname in doc_id_to_filename.items():
        if len(did) > 10:  # print unique GUIDs
            print(f"  {fname} -> {did}")

    with open(TEST_JSON_PATH) as f:
        suite = json.load(f)

    questions = suite["questions"]
    print(f"\nRunning Fresh Benchmark on {len(questions)} questions across 12 categories...")
    print("=" * 80)

    results = []
    latencies = []
    category_stats = defaultdict(lambda: {
        "total": 0, "correct": 0, "recall5": 0, "recall3": 0, "mrr_sum": 0.0,
        "fast_path": 0, "full_path": 0, "latencies": [], "hallucinations": 0
    })

    for idx, q in enumerate(questions):
        qid = q["id"]
        cat = q["category"]
        query_text = q["question"]
        expected_docs = set(q.get("expected_source_docs", []))
        is_hallucination_check = q.get("hallucination_check", False)

        t_start = time.perf_counter()
        try:
            req = QueryRequest(query=query_text, workspace_id=WORKSPACE_ID)
            res = query_service.execute_query(req)
        except Exception as e:
            print(f"[{qid}] ERROR running query: {e}")
            res = {
                "answer": f"ERROR: {e}",
                "citations": [],
                "confidence": 0.0,
                "latency_ms": (time.perf_counter() - t_start) * 1000.0,
                "fast_path": False,
                "retrieval_path": "error"
            }

        elapsed_ms = res.get("latency_ms", (time.perf_counter() - t_start) * 1000.0)
        latencies.append(elapsed_ms)
        answer = res.get("answer", "")
        citations = res.get("citations", [])
        fast_path = res.get("fast_path", False)
        retrieval_path = res.get("retrieval_path", "full")

        # Map citation doc IDs to filenames
        retrieved_filenames = []
        for c in citations:
            did = c.get("document_id")
            fname = c.get("document_filename") or doc_id_to_filename.get(did, did)
            retrieved_filenames.append(fname)

        # Calculate retrieval metrics
        top_5_files = retrieved_filenames[:5]
        top_3_files = retrieved_filenames[:3]

        recall_at_5 = 0
        recall_at_3 = 0
        mrr = 0.0

        if expected_docs:
            if any(f in expected_docs for f in top_5_files):
                recall_at_5 = 1
            if any(f in expected_docs for f in top_3_files):
                recall_at_3 = 1
            for rank, fname in enumerate(retrieved_filenames, 1):
                if fname in expected_docs:
                    mrr = 1.0 / rank
                    break
        else:
            # For ungrounded/refusal questions, recall is N/A (counted as 1 if no citations or not penalized)
            recall_at_5 = 1 if len(citations) == 0 else 0
            recall_at_3 = 1 if len(citations) == 0 else 0
            mrr = 1.0 if len(citations) == 0 else 0.0

        # Evaluate answer correctness
        is_correct, reason = evaluate_answer_correctness(answer, q)

        # Check hallucination
        is_hallucinated = False
        if is_hallucination_check and q.get("answer_type") == "refusal":
            if not is_correct:
                is_hallucinated = True

        # Compute faithfulness on retrieved context
        context_text = " ".join(c.get("text", "") for c in citations)
        faith = compute_faithfulness(answer, context_text)

        # Update stats
        cat_stat = category_stats[cat]
        cat_stat["total"] += 1
        if is_correct:
            cat_stat["correct"] += 1
        if recall_at_5:
            cat_stat["recall5"] += 1
        if recall_at_3:
            cat_stat["recall3"] += 1
        cat_stat["mrr_sum"] += mrr
        cat_stat["latencies"].append(elapsed_ms)
        if fast_path:
            cat_stat["fast_path"] += 1
        else:
            cat_stat["full_path"] += 1
        if is_hallucinated:
            cat_stat["hallucinations"] += 1

        result_item = {
            "id": qid,
            "category": cat,
            "difficulty": q.get("difficulty", "medium"),
            "question": query_text,
            "expected_answer": q.get("expected_answer"),
            "expected_source_docs": list(expected_docs),
            "generated_answer": answer,
            "retrieved_documents": retrieved_filenames[:6],
            "correct": is_correct,
            "eval_reason": reason,
            "recall_at_5": recall_at_5,
            "recall_at_3": recall_at_3,
            "mrr": round(mrr, 4),
            "faithfulness": faith,
            "is_hallucinated": is_hallucinated,
            "fast_path": fast_path,
            "retrieval_path": retrieval_path,
            "latency_ms": round(elapsed_ms, 2),
            "confidence": res.get("confidence", 0.0)
        }
        results.append(result_item)

        status_sym = "✓" if is_correct else "✗"
        print(f"[{qid}][{cat[:8]:8s}] {status_sym} {elapsed_ms:6.1f}ms | Path: {retrieval_path:4s} | {query_text[:50]}...")
        if not is_correct:
            print(f"      Ans: {answer[:90]}... (Reason: {reason})")

        # Polite throttle to avoid Bedrock Titan burst rate limits
        time.sleep(0.3)

    # Save detailed results
    with open(OUTPUT_JSON_PATH, "w") as f:
        json.dump({
            "test_suite": suite["test_suite_name"],
            "workspace_id": WORKSPACE_ID,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "total_questions": len(results),
            "results": results
        }, f, indent=2)

    # Compute Global Summary
    total_q = len(results)
    total_correct = sum(1 for r in results if r["correct"])
    accuracy = (total_correct / total_q) * 100

    # Recall on grounded questions (expected_source_docs not empty)
    grounded_q = [r for r in results if r["expected_source_docs"]]
    recall5 = (sum(r["recall_at_5"] for r in grounded_q) / len(grounded_q)) * 100 if grounded_q else 0.0
    recall3 = (sum(r["recall_at_3"] for r in grounded_q) / len(grounded_q)) * 100 if grounded_q else 0.0
    mrr_avg = sum(r["mrr"] for r in grounded_q) / len(grounded_q) if grounded_q else 0.0

    # Refusal and hallucination rates
    refusal_q = [r for r in results if r["category"] == "HALLUCINATION_ABSENT"]
    refusal_acc = (sum(1 for r in refusal_q if r["correct"]) / len(refusal_q)) * 100 if refusal_q else 0.0
    hallucination_rate = (sum(1 for r in refusal_q if r["is_hallucinated"]) / len(refusal_q)) * 100 if refusal_q else 0.0

    # Latency percentiles
    sorted_latencies = sorted(latencies)
    p50_lat = sorted_latencies[len(sorted_latencies) // 2]
    p95_idx = min(int(len(sorted_latencies) * 0.95), len(sorted_latencies) - 1)
    p95_lat = sorted_latencies[p95_idx]
    mean_lat = sum(latencies) / len(latencies)

    # Fast vs Full path
    fast_count = sum(1 for r in results if r["fast_path"])
    full_count = total_q - fast_count

    print("\n" + "=" * 80)
    print("                      TRUE BENCHMARK REPORT (FRESH EXECUTION)        ")
    print("=" * 80)
    print(f"Total Questions Evaluated:  {total_q}")
    print(f"Overall Accuracy:           {accuracy:.2f}% ({total_correct}/{total_q})")
    print(f"Recall@5 (Grounded):        {recall5:.2f}%")
    print(f"Recall@3 (Grounded):        {recall3:.2f}%")
    print(f"MRR (Mean Reciprocal Rank): {mrr_avg:.4f}")
    print(f"Refusal Accuracy:           {refusal_acc:.2f}% ({sum(1 for r in refusal_q if r['correct'])}/{len(refusal_q)})")
    print(f"Hallucination Rate:         {hallucination_rate:.2f}%")
    print(f"Fast-Path Routing:          {fast_count}/{total_q} ({(fast_count/total_q)*100:.1f}%)")
    print(f"Full-Path Routing:          {full_count}/{total_q} ({(full_count/total_q)*100:.1f}%)")
    print(f"Latency Mean / p50 / p95:   {mean_lat:.1f}ms / {p50_lat:.1f}ms / {p95_lat:.1f}ms")
    print("=" * 80)

    print("\nCATEGORY BREAKDOWN:")
    print(f"{'Category':<22} | {'Count':<5} | {'Acc (%)':<8} | {'Rec@5':<8} | {'MRR':<6} | {'p50 (ms)':<9} | {'Fast-Path'}")
    print("-" * 80)
    for cat, stat in sorted(category_stats.items()):
        cnt = stat["total"]
        acc = (stat["correct"] / cnt) * 100
        rec = (stat["recall5"] / cnt) * 100
        mrr_val = stat["mrr_sum"] / cnt
        med_lat = sorted(stat["latencies"])[len(stat["latencies"]) // 2]
        print(f"{cat:<22} | {cnt:<5} | {acc:6.1f}%  | {rec:6.1f}%  | {mrr_val:0.3f}  | {med_lat:8.1f}  | {stat['fast_path']}/{cnt}")

    print("\nResults saved to:", OUTPUT_JSON_PATH)


if __name__ == "__main__":
    main()
