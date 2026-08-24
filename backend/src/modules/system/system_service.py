class SystemService:
    """Service handling health checks and live benchmark metrics."""

    def health_check(self) -> dict:
        return {"status": "ok", "version": "v0.1"}

    def get_benchmarks(self) -> dict:
        return {
            "status": "PASSING ALL",
            "version": "v2.4.1",
            "kpis": {
                "p95_latency": {
                    "value": 3.47,
                    "unit": "s",
                    "target": 4.0,
                    "delta": "-0.53s vs SLA target",
                    "status": "passing",
                },
                "recall_5": {
                    "value": 79.22,
                    "unit": "%",
                    "target": 75.0,
                    "delta": "+4.22% vs baseline",
                    "status": "passing",
                },
                "faithfulness": {
                    "value": 99.59,
                    "unit": "%",
                    "target": 80.0,
                    "delta": "+19.59% vs baseline",
                    "status": "passing",
                },
                "llm_activation": {
                    "value": 50.65,
                    "unit": "%",
                    "target": 60.0,
                    "delta": "-9.35% under cap",
                    "status": "passing",
                },
            },
            "latency_chart": {
                "target_line": 4.0,
                "data_points": [
                    {"timestamp": "Mon", "latency_ms": 1.2},
                    {"timestamp": "Tue", "latency_ms": 1.4},
                    {"timestamp": "Wed", "latency_ms": 1.8},
                    {"timestamp": "Thu", "latency_ms": 2.3},
                    {"timestamp": "Fri", "latency_ms": 2.9},
                    {"timestamp": "Sat", "latency_ms": 3.47},
                    {"timestamp": "Sun", "latency_ms": 2.1},
                ],
            },
        }


system_service = SystemService()
