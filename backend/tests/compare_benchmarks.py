import json
from pathlib import Path
from collections import defaultdict

backend_dir = Path(__file__).resolve().parent.parent
before_path = backend_dir / "tmp" / "live_65_benchmark_results_before_remediation.json"
after_path = backend_dir / "tmp" / "live_65_benchmark_results.json"

def analyze():
    if not after_path.exists():
        print(f"Waiting for {after_path} to exist...")
        return

    with open(before_path) as f:
        before_data = json.load(f)
    with open(after_path) as f:
        after_data = json.load(f)

    before_results = {r["id"]: r for r in before_data.get("results", [])}
    after_results = {r["id"]: r for r in after_data.get("results", [])}

    all_ids = list(after_results.keys())
    print(f"Loaded {len(before_results)} before results and {len(after_results)} after results.")

    # Overall metrics
    def compute_metrics(results_dict):
        total = len(results_dict)
        if total == 0:
            return {}
        correct = sum(1 for r in results_dict.values() if r.get("correct", False))
        recall5_num = sum(r.get("recall_at_5", 0) for r in results_dict.values())
        recall3_num = sum(r.get("recall_at_3", 0) for r in results_dict.values())
        mrr_sum = sum(r.get("mrr", 0.0) for r in results_dict.values())
        
        false_refusals = sum(1 for r in results_dict.values() if r.get("quality_status") == "FALSE_REFUSAL")
        hallucination_queries = [r for r in results_dict.values() if r.get("category") == "HALLUCINATION_ABSENT"]
        correct_refusals = sum(1 for r in hallucination_queries if r.get("correct", False))
        
        fast_path_runs = [r for r in results_dict.values() if r.get("fast_path", False)]
        fast_path_correct = sum(1 for r in fast_path_runs if r.get("correct", False))
        
        latencies = [r.get("latency_ms", 0.0) for r in results_dict.values()]
        avg_latency = sum(latencies) / total if latencies else 0.0

        return {
            "total": total,
            "correct": correct,
            "accuracy": (correct / total) * 100,
            "recall5_num": recall5_num,
            "recall5_den": total,
            "recall5_pct": (recall5_num / total) * 100,
            "recall3_num": recall3_num,
            "recall3_pct": (recall3_num / total) * 100,
            "mrr": mrr_sum / total,
            "false_refusals": false_refusals,
            "hallucination_total": len(hallucination_queries),
            "correct_refusals": correct_refusals,
            "refusal_accuracy": (correct_refusals / len(hallucination_queries) * 100) if hallucination_queries else 0.0,
            "fast_path_count": len(fast_path_runs),
            "fast_path_correct": fast_path_correct,
            "avg_latency_ms": avg_latency,
        }

    b_met = compute_metrics(before_results)
    a_met = compute_metrics(after_results)

    print("\n" + "="*80)
    print("OVERALL BENCHMARK COMPARISON (65 Queries, Identical IDs & Rubric)")
    print("="*80)
    print(f"{'Metric':<30} | {'Before Remediation':<20} | {'After Remediation':<20} | {'Delta':<10}")
    print("-" * 88)
    for k in ["accuracy", "recall5_pct", "recall3_pct", "mrr", "false_refusals", "refusal_accuracy", "fast_path_count", "avg_latency_ms"]:
        bv = b_met.get(k, 0)
        av = a_met.get(k, 0)
        delta = av - bv
        fmt_bv = f"{bv:.2f}" if isinstance(bv, float) else str(bv)
        fmt_av = f"{av:.2f}" if isinstance(av, float) else str(av)
        fmt_d = f"{delta:+.2f}" if isinstance(delta, float) else f"{delta:+d}"
        print(f"{k:<30} | {fmt_bv:<20} | {fmt_av:<20} | {fmt_d:<10}")

    # Category breakdown
    categories = sorted(list(set(r.get("category", "") for r in after_results.values())))
    print("\n" + "="*80)
    print("CATEGORY BREAKDOWN COMPARISON")
    print("="*80)
    print(f"{'Category':<22} | {'Count':<6} | {'Before Acc':<12} | {'After Acc':<12} | {'B.Rec@5':<10} | {'A.Rec@5':<10}")
    print("-" * 88)
    for cat in categories:
        b_cat = {qid: r for qid, r in before_results.items() if r.get("category") == cat}
        a_cat = {qid: r for qid, r in after_results.items() if r.get("category") == cat}
        b_c = sum(1 for r in b_cat.values() if r.get("correct", False))
        a_c = sum(1 for r in a_cat.values() if r.get("correct", False))
        b_r5 = sum(r.get("recall_at_5", 0) for r in b_cat.values())
        a_r5 = sum(r.get("recall_at_5", 0) for r in a_cat.values())
        tot = len(a_cat)
        b_acc_s = f"{b_c}/{tot} ({(b_c/tot)*100:.1f}%)" if tot else "N/A"
        a_acc_s = f"{a_c}/{tot} ({(a_c/tot)*100:.1f}%)" if tot else "N/A"
        b_r5_s = f"{b_r5}/{tot}"
        a_r5_s = f"{a_r5}/{tot}"
        print(f"{cat:<22} | {tot:<6} | {b_acc_s:<12} | {a_acc_s:<12} | {b_r5_s:<10} | {a_r5_s:<10}")

    # Changes per query
    print("\n" + "="*80)
    print("QUERY STATUS TRANSITIONS")
    print("="*80)
    transitions = defaultdict(list)
    for qid in all_ids:
        b_corr = before_results.get(qid, {}).get("correct", False)
        a_corr = after_results.get(qid, {}).get("correct", False)
        if not b_corr and a_corr:
            transitions["FIXED (False -> True)"].append(qid)
        elif b_corr and not a_corr:
            transitions["REGRESSED (True -> False)"].append(qid)
        elif not b_corr and not a_corr:
            transitions["REMAINED_FAIL (False -> False)"].append(qid)
        else:
            transitions["REMAINED_PASS (True -> True)"].append(qid)

    for trans, qids in transitions.items():
        print(f"\n{trans} (Total {len(qids)}):")
        for qid in qids:
            q_after = after_results[qid]
            print(f"  [{qid}][{q_after.get('category')}]: {q_after.get('question')[:65]}...")
            if trans in ("FIXED (False -> True)", "REGRESSED (True -> False)", "REMAINED_FAIL (False -> False)"):
                b_ans = before_results.get(qid, {}).get("generated_answer", "")[:60].replace("\n", " ")
                a_ans = q_after.get("generated_answer", "")[:60].replace("\n", " ")
                print(f"     Before: {b_ans}")
                print(f"     After:  {a_ans}")
                print(f"     Reason: {q_after.get('eval_reason')}")

if __name__ == "__main__":
    analyze()
