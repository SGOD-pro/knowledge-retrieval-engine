import pytest
from fastapi.testclient import TestClient
import sys
from pathlib import Path

# Add src directory to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from main import app

client = TestClient(app)


def test_no_unprefixed_root_routes():
    """Item 7 guard: verify router is NOT mounted at root.
    All application endpoints must be mounted exclusively under /api/v1.
    Unprefixed requests to /workspaces, /query, /ingest, etc. must return 404.
    """
    # 1. Unprefixed HTTP paths must return 404 Not Found
    unprefixed_paths = [
        "/workspaces",
        "/system/benchmarks",
        "/auth/login",
    ]
    for path in unprefixed_paths:
        resp = client.get(path)
        assert resp.status_code == 404, f"Expected 404 for un-prefixed path {path}, got {resp.status_code}"

    # 2. Inspect registered OpenAPI paths:
    # All registered API endpoints must start with /api/v1.
    openapi_paths = list(app.openapi()["paths"].keys())
    assert len(openapi_paths) > 0, "Expected registered API paths"
    for path in openapi_paths:
        assert path.startswith("/api/v1/"), (
            f"Path '{path}' is registered outside /api/v1 prefix! Duplicate mount detected."
        )
