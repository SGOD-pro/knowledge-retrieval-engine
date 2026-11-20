#!/usr/bin/env python3
"""Standalone smoke script for manual verification of the LAYA decision endpoint.

Usage:
    cd backend && uv run python scripts/smoke_laya_endpoint.py
"""

from __future__ import annotations

import json
import os
import sys
import time

# Ensure src in path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import requests
from config import settings


def main() -> int:
    url = os.environ.get("LAYA_ENDPOINT_URL") or getattr(settings, "LAYA_ENDPOINT_URL", "")
    key = os.environ.get("LAYA_API_KEY") or getattr(settings, "LAYA_API_KEY", "")

    if not url:
        print("[ERROR] LAYA_ENDPOINT_URL is not configured in environment or .env")
        return 1
    if not key:
        print("[ERROR] LAYA_API_KEY is not configured in environment or .env")
        return 1

    print(f"Connecting to LAYA endpoint: {url}")
    payload = {
        "state": {
            "document": "Query: Where was Alan Turing born?\nGap: The exact birthplace city is missing from initial retrieval chunks."
        },
        "questions": {
            "route": {
                "type": "choice",
                "instructions": "Which targeted retrieval expansion route should be executed?",
                "criteria": {
                    "lexical_expansion": "Perform keyword BM25 search for specific missing terms",
                    "dense_expansion": "Perform vector similarity search for semantic concepts",
                    "structural_expansion": "Navigate document structure or specific sections",
                    "graph_expansion": "Traverse knowledge graph entity relationships",
                    "structured_expansion": "Query structured table columns or database operands",
                    "none": "No expansion needed or evidence is fundamentally missing",
                },
            },
            "complexity": {
                "type": "score",
                "instructions": "How complex is this retrieval gap?",
                "criteria": ["simple", "moderate", "complex"],
            },
        },
    }

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    t0 = time.perf_counter()
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=5.0)
    except Exception as e:
        print(f"[FAIL] Request failed: {e}")
        return 2

    latency_ms = (time.perf_counter() - t0) * 1000.0
    print(f"HTTP Status: {resp.status_code}")
    print(f"Roundtrip Latency: {latency_ms:.2f} ms")

    if resp.status_code != 200:
        print(f"[FAIL] Server returned non-200: {resp.text}")
        return 3

    try:
        data = resp.json()
        print("\nServer Response:")
        print(json.dumps(data, indent=2))
        route = data.get("answers", {}).get("route", {}).get("choice")
        conf = data.get("answers", {}).get("route", {}).get("answer_confidence")
        print(f"\n[SUCCESS] Chosen Route: '{route}', Confidence: {conf}")
        return 0
    except Exception as je:
        print(f"[FAIL] Invalid JSON response: {je}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
