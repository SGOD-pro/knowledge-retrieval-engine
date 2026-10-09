import pytest
from fastapi.testclient import TestClient

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from main import app
from src.schemas.contracts.envelope import TrustedAuthContext
from src.security.auth import require_auth_context

client = TestClient(app)

def test_workspace_routes_enforce_auth_context_match():
    """Verify that route handlers actively enforce the workspace boundary against the auth context."""
    # Bob is authenticated but authorized for ws_beta
    bob_beta_context = TrustedAuthContext(
        authorized_workspace_id="ws_beta",
        principal_id="usr_bob",
        principal_roles=("owner",),
        access_policy_version="v1"
    )
    
    app.dependency_overrides[require_auth_context] = lambda: bob_beta_context
    
    try:
        # Attempt to access Alice's workspace ws_alpha
        
        # 1. get_workspace (already manual checks owner, so it might 404)
        resp1 = client.get("/api/v1/workspaces/ws_alpha")
        assert resp1.status_code == 404, f"Expected 404 for GET workspace, got {resp1.status_code}"
        
        # 2. list_sources
        resp2 = client.get("/api/v1/workspaces/ws_alpha/sources")
        assert resp2.status_code == 404, f"Expected 404 for list sources, got {resp2.status_code}"
        
        # 3. create_source
        resp3 = client.post("/api/v1/workspaces/ws_alpha/sources", json={"title": "Hack"})
        assert resp3.status_code == 404, f"Expected 404 for create source, got {resp3.status_code}"
        
        # 4. get_source (assuming src_1 exists in ws_alpha)
        resp4 = client.get("/api/v1/workspaces/ws_alpha/sources/src_1")
        assert resp4.status_code == 404, f"Expected 404 for get source, got {resp4.status_code}"
        
        # 5. delete_source
        resp5 = client.delete("/api/v1/workspaces/ws_alpha/sources/src_1")
        assert resp5.status_code == 404, f"Expected 404 for delete source, got {resp5.status_code}"
        
        # 6. query
        resp6 = client.post("/api/v1/workspaces/ws_alpha/query", json={"query": "test"})
        assert resp6.status_code == 404, f"Expected 404 for query, got {resp6.status_code}"
        
    finally:
        app.dependency_overrides.clear()
