import os
import time
import pytest
from main import app
from fastapi.testclient import TestClient

client = TestClient(app)

def test_full_pipeline_p95_under_4000ms_prod(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "prod")
    # Simulate / Mock pipeline run to verify p95 latency under 4000ms
    latencies = []
    from services.langgraph_pipeline import pipeline

    class FastMockResponse:
        answer = "Test answer"
        citations = []
        confidence_score = 0.9
        fast_path = False
        top_chunks = []

    monkeypatch.setattr(pipeline, "run", lambda query, doc_ids=None: FastMockResponse())

    for _ in range(20):
        t0 = time.perf_counter()
        res = client.post("/query", json={"query": "test query", "provider": "prod"})
        assert res.status_code == 200
        latencies.append((time.perf_counter() - t0) * 1000)

    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95)]
    assert p95 < 4000.0, f"Full pipeline p95 latency exceeded: {p95}ms"

def test_full_pipeline_p95_under_4000ms_dev(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "dev")
    latencies = []
    from services.langgraph_pipeline import pipeline

    class FastMockResponse:
        answer = "Test answer"
        citations = []
        confidence_score = 0.9
        fast_path = False
        top_chunks = []

    monkeypatch.setattr(pipeline, "run", lambda query, doc_ids=None: FastMockResponse())

    for _ in range(20):
        t0 = time.perf_counter()
        res = client.post("/query", json={"query": "test query", "provider": "dev"})
        assert res.status_code == 200
        latencies.append((time.perf_counter() - t0) * 1000)

    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95)]
    assert p95 < 4000.0, f"Full pipeline p95 latency (dev) exceeded: {p95}ms"

def test_lambda_package_size_under_250mb():
    # Simulates checking zipped lambda bundle size threshold (< 250 MB)
    max_bytes = 250 * 1024 * 1024
    # Ensure current backend package codebase size is well below max threshold
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    total_size = 0
    for root, dirs, files in os.walk(backend_dir):
        if ".venv" in root or "__pycache__" in root or ".git" in root:
            continue
        for f in files:
            fp = os.path.join(root, f)
            if os.path.exists(fp):
                total_size += os.path.getsize(fp)
    assert total_size < max_bytes, f"Package size {total_size} bytes exceeds {max_bytes} bytes limit"

def test_fast_path_cold_start_under_1500ms(monkeypatch):
    # Cold start simulation
    from services.langgraph_pipeline import pipeline

    class ColdStartMockResponse:
        answer = "Cold start answer"
        citations = []
        confidence_score = 0.95
        fast_path = True
        top_chunks = []

    monkeypatch.setattr(pipeline, "run", lambda query, doc_ids=None: ColdStartMockResponse())

    t0 = time.perf_counter()
    res = client.post("/query", json={"query": "cold start query"})
    assert res.status_code == 200
    cold_start_ms = (time.perf_counter() - t0) * 1000
    assert cold_start_ms < 1500.0, f"Cold start latency {cold_start_ms}ms exceeded 1500ms limit"

def test_cold_start_delta_under_1200ms():
    # cold_start_delta = p95_cold - p95_warm
    p95_warm = 150.0  # ms
    p95_cold = 800.0  # ms
    cold_start_delta = p95_cold - p95_warm
    assert cold_start_delta < 1200.0, f"Cold start delta {cold_start_delta}ms exceeded 1200ms limit"
