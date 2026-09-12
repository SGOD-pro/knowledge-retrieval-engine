import json
from pathlib import Path


class SystemService:
    """Service handling health checks and live benchmark metrics."""

    def health_check(self) -> dict:
        return {"status": "ok", "version": "v0.1"}

    def get_benchmarks(self) -> dict:
        tmp_dir = Path(__file__).resolve().parent.parent.parent.parent / "tmp"

        # Priority 1: canonical_77_benchmark_live_okf.json
        # Priority 2: verification_benchmark_60.json
        target_files = [
            tmp_dir / "canonical_77_benchmark_live_okf.json",
            tmp_dir / "verification_benchmark_60.json",
        ]

        data = None
        for tf in target_files:
            if tf.exists():
                try:
                    with open(tf, "r", encoding="utf-8") as f:
                        content = json.load(f)
                        summary = content.get("summary") or content.get("metrics")
                        if summary and (summary.get("total_queries", 0) > 0 or summary.get("scored_queries", 0) > 0):
                            data = summary
                            break
                except Exception:
                    continue

        if not data:
            return {
                "status": "UNVERIFIED",
                "version": "v2.4.1",
                "kpis": {
                    "p95_latency": {
                        "value": 0.0,
                        "unit": "s",
                        "target": 4.0,
                        "delta": "0.0s vs SLA target",
                        "status": "unverified",
                    },
                    "recall_5": {
                        "value": 0.0,
                        "unit": "%",
                        "target": 75.0,
                        "delta": "0.0% vs baseline",
                        "status": "unverified",
                    },
                    "faithfulness": {
                        "value": 0.0,
                        "unit": "%",
                        "target": 80.0,
                        "delta": "0.0% vs baseline",
                        "status": "unverified",
                    },
                    "llm_activation": {
                        "value": 0.0,
                        "unit": "%",
                        "target": 60.0,
                        "delta": "0.0% under cap",
                        "status": "unverified",
                    },
                },
                "latency_chart": {
                    "target_line": 4.0,
                    "data_points": [],
                },
            }

        recall_5_val = round((data.get("recall_at_5", 0.0) or 0.0) * 100, 2)
        faith_val = round((data.get("real_answer_faithfulness", 0.0) or 0.0) * 100, 2)
        p95_val = round((data.get("p95_latency_ms", 0.0) or 0.0) / 1000.0, 2)
        llm_act_val = round((data.get("llm_activation_rate", 0.0) or 0.0) * 100, 2)

        return {
            "status": "PASSING ALL" if (recall_5_val >= 75.0 and faith_val >= 80.0 and p95_val <= 4.0) else "FAILING",
            "version": "v2.4.1",
            "kpis": {
                "p95_latency": {
                    "value": p95_val,
                    "unit": "s",
                    "target": 4.0,
                    "delta": f"{round(p95_val - 4.0, 2)}s vs SLA target",
                    "status": "passing" if p95_val <= 4.0 else "failing",
                },
                "recall_5": {
                    "value": recall_5_val,
                    "unit": "%",
                    "target": 75.0,
                    "delta": f"{round(recall_5_val - 75.0, 2)}% vs baseline",
                    "status": "passing" if recall_5_val >= 75.0 else "failing",
                },
                "faithfulness": {
                    "value": faith_val,
                    "unit": "%",
                    "target": 80.0,
                    "delta": f"{round(faith_val - 80.0, 2)}% vs baseline",
                    "status": "passing" if faith_val >= 80.0 else "failing",
                },
                "llm_activation": {
                    "value": llm_act_val,
                    "unit": "%",
                    "target": 60.0,
                    "delta": f"{round(llm_act_val - 60.0, 2)}% vs baseline",
                    "status": "passing" if llm_act_val <= 60.0 else "failing",
                },
            },
            "latency_chart": {
                "target_line": 4.0,
                "data_points": [],
            },
        }


system_service = SystemService()
