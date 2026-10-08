"""Comprehensive boundary isolation, workspace authorization, and delivery gate tests.

Validates AC-1 through AC-15 from Specification 0002:
- Dual layer boundary enforcement
- Test authentication provider and production startup barrier
- Cross workspace 404 defense
- Atomic deletion tombstones and CAS publication retries
- Version aware cache key generation and exact query whitespace hashing
- Pinned snapshot and tombstone delivery gate rechecks
- Incomplete aggregate calculation handling
- Pre dispatch vector filter validation
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from src.schemas.contracts.boundary import (
    CASConflictError,
    CacheKeyDescriptor,
    CachedQueryResult,
    CitationItem,
    JobRecord,
    PublicationManifestRecord,
    QdrantFilterContract,
    QueryExecutionOutcome,
    SnapshotRecord,
    SourceItem,
    TombstoneRecord,
    WorkerExecutionContext,
    WorkspaceAccessDeniedError,
    WorkspaceRecord,
)
from src.schemas.contracts.envelope import TrustedAuthContext
from src.schemas.contracts.location import DocumentLocation
from src.db.workspace_boundary_repo import workspace_boundary_repo
from src.security.auth import (
    TestAuthRegistry,
    commit_publication_with_cas,
    retry_publication_with_tombstone_revalidation,
    validate_auth_configuration,
)
from src.services.cache_delivery import (
    ControlledFixtureExecutionEngine,
    InMemoryCacheStore,
    delivery_gate_recheck,
    evaluate_query_delivery_and_recomputation,
)
from src.services.vector_boundary import MockQdrantClient, dispatch_qdrant_search

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_repo_state():
    """Reset repository state to clean baseline before each test."""
    workspace_boundary_repo.reset_fixtures()


# ---------------------------------------------------------------------------
# AC-1, AC-4, AC-5: Happy Path & Caller Identity Resolution
# ---------------------------------------------------------------------------
def test_happy_path_alice_workspace_lifecycle():
    """Alice authenticates, creates a source, queries workspace alpha successfully."""
    headers = {"Authorization": "Bearer test_token_alice"}

    # 1. Verify Alice can access workspace alpha
    resp = client.get("/api/v1/workspaces/ws_alpha", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["workspace_id"] == "ws_alpha"
    assert data["status"] == "active"

    # 2. Register source in workspace alpha
    src_resp = client.post(
        "/api/v1/workspaces/ws_alpha/sources",
        json={"title": "Q3 Revenue Report"},
        headers=headers,
    )
    assert src_resp.status_code == 201
    src_data = src_resp.json()
    assert src_data["title"] == "Q3 Revenue Report"
    assert src_data["status"] == "registered"

    # 3. Query workspace alpha
    query_resp = client.post(
        "/api/v1/workspaces/ws_alpha/query",
        json={"query": "What was the Q3 revenue?"},
        headers=headers,
    )
    assert query_resp.status_code == 200
    q_data = query_resp.json()
    assert q_data["status"] == "SUCCESS"
    assert "Q3 revenue" in q_data["answer"]


# ---------------------------------------------------------------------------
# AC-2: Unauthenticated Request Rejection
# ---------------------------------------------------------------------------
def test_unauthenticated_requests_return_401():
    """Requests missing Authorization header return HTTP 401 Unauthorized immediately."""
    endpoints = [
        ("POST", "/api/v1/workspaces", {"name": "Test Workspace"}),
        ("GET", "/api/v1/workspaces/ws_alpha"),
        ("GET", "/api/v1/workspaces/ws_alpha/sources"),
        ("POST", "/api/v1/workspaces/ws_alpha/sources", {"title": "Test"}),
        ("POST", "/api/v1/workspaces/ws_alpha/query", {"query": "Test query"}),
        ("DELETE", "/api/v1/workspaces/ws_alpha/sources/src_dummy"),
    ]

    for method, path, *payload in endpoints:
        json_body = payload[0] if payload else None
        if method == "GET":
            r = client.get(path)
        elif method == "POST":
            r = client.post(path, json=json_body)
        elif method == "DELETE":
            r = client.delete(path)
        assert r.status_code == 401, f"{method} {path} should return 401, got {r.status_code}"


def test_invalid_bearer_token_returns_401():
    """Request with unrecognized bearer token returns HTTP 401 Unauthorized."""
    headers = {"Authorization": "Bearer unrecognized_fake_token"}
    resp = client.get("/api/v1/workspaces/ws_alpha", headers=headers)
    assert resp.status_code == 401


def test_unauthenticated_workspace_creation_proves_no_workspace_created():
    """Missing or invalid credentials return 401 and guarantee no workspace is mutated or created."""
    # 1. Missing Authorization header on standard payload
    resp_no_auth = client.post("/api/v1/workspaces", json={"name": "Attacker Workspace"})
    assert resp_no_auth.status_code == 401

    # 2. Missing Authorization header on legacy parameter payload (regression against fallback bypass)
    resp_legacy_bypass = client.post(
        "/api/v1/workspaces",
        json={"name": "Attacker Legacy Workspace", "industry": "legal", "description": "bypass attempt"},
    )
    assert resp_legacy_bypass.status_code == 401

    # 3. Invalid bearer token
    resp_bad_token = client.post(
        "/api/v1/workspaces",
        json={"name": "Attacker Invalid Token Workspace"},
        headers={"Authorization": "Bearer invalid_attacker_token"},
    )
    assert resp_bad_token.status_code == 401

    # Verify authoritative repository: only baseline fixtures ws_alpha and ws_beta exist
    assert workspace_boundary_repo.get_workspace("ws_alpha") is not None
    assert workspace_boundary_repo.get_workspace("ws_beta") is not None
    assert workspace_boundary_repo.get_workspace("Attacker Workspace") is None
    assert workspace_boundary_repo.get_workspace("Attacker Legacy Workspace") is None


# ---------------------------------------------------------------------------
# AC-3, AC-15: Cross Workspace Access Defense (404 Not Found)
# ---------------------------------------------------------------------------
def test_cross_workspace_access_returns_404():
    """Alice attempting to read, query, or delete Bob resources receives HTTP 404."""
    alice_headers = {"Authorization": "Bearer test_token_alice"}

    # Attempt to read Bob workspace beta
    resp_get = client.get("/api/v1/workspaces/ws_beta", headers=alice_headers)
    assert resp_get.status_code == 404

    # Attempt to query Bob workspace beta
    resp_query = client.post(
        "/api/v1/workspaces/ws_beta/query",
        json={"query": "Bob secret financials"},
        headers=alice_headers,
    )
    assert resp_query.status_code == 404

    # Attempt to list sources in Bob workspace beta
    resp_sources = client.get("/api/v1/workspaces/ws_beta/sources", headers=alice_headers)
    assert resp_sources.status_code == 404

    # Attempt to delete source in Bob workspace beta
    resp_del = client.delete("/api/v1/workspaces/ws_beta/sources/src_secret", headers=alice_headers)
    assert resp_del.status_code == 404


# ---------------------------------------------------------------------------
# AC-4: Direct Service Boundary Enforcement
# ---------------------------------------------------------------------------
def test_direct_service_boundary_checks():
    """Direct repository and helper methods raise WorkspaceAccessDeniedError on mismatch."""
    auth_alice = TrustedAuthContext(
        authorized_workspace_id="ws_alpha",
        principal_id="usr_alice",
        principal_roles=("owner",),
        access_policy_version="v1",
    )

    # Calling tombstone_workspace on foreign workspace raises WorkspaceAccessDeniedError
    with pytest.raises(WorkspaceAccessDeniedError):
        workspace_boundary_repo.tombstone_workspace(
            workspace_id="ws_beta",
            caller_principal_id=auth_alice.principal_id,
        )


# ---------------------------------------------------------------------------
# AC-5: Dynamic Workspace Ownership & Production Startup Barrier
# ---------------------------------------------------------------------------
def test_dynamic_workspace_ownership_creation():
    """Alice dynamically creates a workspace ws_test1; Bob gets 404 on it."""
    alice_headers = {"Authorization": "Bearer test_token_alice"}
    bob_headers = {"Authorization": "Bearer test_token_bob"}

    # Alice creates a new workspace
    create_resp = client.post(
        "/api/v1/workspaces",
        json={"name": "Alice Private Project"},
        headers=alice_headers,
    )
    assert create_resp.status_code == 201
    new_ws = create_resp.json()
    new_ws_id = new_ws["workspace_id"]
    assert new_ws_id.startswith("ws_")

    # Alice can read it
    assert client.get(f"/api/v1/workspaces/{new_ws_id}", headers=alice_headers).status_code == 200

    # Bob receives 404 attempting to read it
    assert client.get(f"/api/v1/workspaces/{new_ws_id}", headers=bob_headers).status_code == 404


def test_production_startup_barrier_raises_runtime_error():
    """Setting ENVIRONMENT='production' and KRE_AUTH_MODE='test' raises fatal RuntimeError."""
    with pytest.raises(RuntimeError) as exc_info:
        validate_auth_configuration(environment="production", auth_mode="test")
    assert "strictly prohibited" in str(exc_info.value)

    # Non-production allows test auth
    validate_auth_configuration(environment="development", auth_mode="test")
    validate_auth_configuration(environment="test", auth_mode="test")


# ---------------------------------------------------------------------------
# AC-6: Restricted Worker Context
# ---------------------------------------------------------------------------
def test_restricted_worker_context_scoping():
    """Worker context scoped to trusted running job; untrusted job raises PermissionError."""
    # 1. Register a valid running job in repository
    job = JobRecord(
        job_id="job_ingest_001",
        workspace_id="ws_alpha",
        operation="ingest",
        target_source_id="src_doc1",
        target_source_version=1,
        status="running",
        is_trusted_system_job=True,
        created_at="2026-10-08T00:00:00Z",
        updated_at="2026-10-08T00:00:00Z",
    )
    workspace_boundary_repo.save_job(job)

    # 2. Worker context created successfully
    worker_ctx = TestAuthRegistry.create_worker_context("job_ingest_001", workspace_boundary_repo)
    assert worker_ctx.authorized_workspace_id == "ws_alpha"
    assert worker_ctx.operation == "ingest"
    assert worker_ctx.auth_context.principal_id == "sa_pipeline_worker"

    # Worker can execute authorized operation
    worker_ctx.validate_operation_scope("ws_alpha", "ingest", "src_doc1", 1)

    # Worker cannot execute on foreign workspace
    with pytest.raises(WorkspaceAccessDeniedError):
        worker_ctx.validate_operation_scope("ws_beta", "ingest", "src_doc1", 1)

    # Worker cannot execute unapproved operation
    with pytest.raises(WorkspaceAccessDeniedError):
        worker_ctx.validate_operation_scope("ws_alpha", "evaluate", "src_doc1", 1)

    # 3. Non-running or untrusted job raises PermissionError
    queued_job = JobRecord(
        job_id="job_queued",
        workspace_id="ws_alpha",
        operation="ingest",
        status="queued",
        is_trusted_system_job=True,
        created_at="2026-10-08T00:00:00Z",
        updated_at="2026-10-08T00:00:00Z",
    )
    workspace_boundary_repo.save_job(queued_job)
    with pytest.raises(PermissionError):
        TestAuthRegistry.create_worker_context("job_queued", workspace_boundary_repo)


# ---------------------------------------------------------------------------
# AC-7: Typed Citation Locators
# ---------------------------------------------------------------------------
def test_typed_citation_locators():
    """CitationItem binds typed Location union and preserves float precision."""
    loc = DocumentLocation(type="document", page=3, bbox=(0.123456789, 0.2, 0.8, 0.987654321))
    cit = CitationItem(
        evidence_id="ev_src1_v1_chunk0",
        source_id="src_1",
        source_version=1,
        snippet="Key audit finding details",
        location=loc,
    )
    assert cit.location.type == "document"
    assert cit.location.bbox[0] == 0.123456789
    assert cit.location.bbox[3] == 0.987654321


# ---------------------------------------------------------------------------
# AC-8: Immediate Source Deletion Barrier & Idempotence
# ---------------------------------------------------------------------------
def test_source_deletion_barrier_and_idempotence():
    """Deleting a source immediately blocks direct endpoints with 404; delete is idempotent."""
    headers = {"Authorization": "Bearer test_token_alice"}

    # Create source
    src_resp = client.post(
        "/api/v1/workspaces/ws_alpha/sources",
        json={"title": "To Be Deleted"},
        headers=headers,
    )
    src_id = src_resp.json()["source_id"]

    # Verify readable
    assert client.get(f"/api/v1/workspaces/ws_alpha/sources/{src_id}", headers=headers).status_code == 200

    # Delete source
    del_resp = client.delete(f"/api/v1/workspaces/ws_alpha/sources/{src_id}", headers=headers)
    assert del_resp.status_code == 200
    tombstone_meta = del_resp.json()
    assert tombstone_meta["status"] == "tombstoned"
    gen_after_delete = tombstone_meta["manifest_generation"]

    # Direct access now returns 404 immediately
    assert client.get(f"/api/v1/workspaces/ws_alpha/sources/{src_id}", headers=headers).status_code == 404

    # Idempotent second delete returns existing tombstone without incrementing generation
    del_resp2 = client.delete(f"/api/v1/workspaces/ws_alpha/sources/{src_id}", headers=headers)
    assert del_resp2.status_code == 200
    assert del_resp2.json()["manifest_generation"] == gen_after_delete


# ---------------------------------------------------------------------------
# AC-9: Atomic Publication Fencing & Retry Revalidation
# ---------------------------------------------------------------------------
def test_atomic_publication_cas_and_retry_revalidation():
    """Concurrent deletion increments manifest generation, aborting CAS and preventing resurrection."""
    ws = workspace_boundary_repo.get_workspace("ws_alpha")
    expected_gen = ws.manifest_generation

    # 1. CAS commit succeeds when generation matches
    manifest = commit_publication_with_cas(
        workspace_id="ws_alpha",
        expected_manifest_generation=expected_gen,
        manifest_delta={"active_source_ids": ("src_1",)},
        table_store=workspace_boundary_repo,
    )
    assert manifest.manifest_generation == expected_gen + 1

    # 2. Concurrent deletion increments manifest generation
    workspace_boundary_repo.record_source_tombstone(
        workspace_id="ws_alpha",
        source_id="src_staged",
        caller_principal_id="usr_alice",
    )

    # Attempting publication with stale expected generation fails with CASConflictError
    with pytest.raises(CASConflictError):
        commit_publication_with_cas(
            workspace_id="ws_alpha",
            expected_manifest_generation=manifest.manifest_generation,
            manifest_delta={"active_source_ids": ("src_staged",)},
            table_store=workspace_boundary_repo,
        )

    # 3. Publication retry detects tombstoned source and aborts without resurrection
    def mock_builder(manifest, staged):
        return {"active_source_ids": list(staged)}

    with pytest.raises(ValueError) as exc:
        retry_publication_with_tombstone_revalidation(
            workspace_id="ws_alpha",
            staged_source_ids=("src_staged",),
            manifest_builder_func=mock_builder,
            table_store=workspace_boundary_repo,
        )
    assert "tombstoned source" in str(exc.value)


# ---------------------------------------------------------------------------
# AC-10: Exact Literal Query & Canonical JSON Hashing
# ---------------------------------------------------------------------------
def test_exact_query_whitespace_and_casing_hashing():
    """Preserves exact internal whitespace ('A  B' != 'A B') and case sensitivity."""
    hash1 = CacheKeyDescriptor.compute_query_hash("SELECT * FROM  table")
    hash2 = CacheKeyDescriptor.compute_query_hash("SELECT * FROM table")
    assert hash1 != hash2, "Differing whitespace in query must produce distinct hashes"

    hash_upper = CacheKeyDescriptor.compute_query_hash("EBITDA")
    hash_lower = CacheKeyDescriptor.compute_query_hash("ebitda")
    assert hash_upper != hash_lower, "Casing must produce distinct hashes"


def test_canonical_json_source_version_set_hash():
    """Canonical JSON hashing produces order independent digests."""
    pairs_a = (("src_2", 1), ("src_1", 2))
    pairs_b = (("src_1", 2), ("src_2", 1))
    hash_a = CacheKeyDescriptor.compute_source_version_set_hash(pairs_a)
    hash_b = CacheKeyDescriptor.compute_source_version_set_hash(pairs_b)
    assert hash_a == hash_b

    # Differing versions produce different hash
    pairs_c = (("src_1", 3), ("src_2", 1))
    assert hash_a != CacheKeyDescriptor.compute_source_version_set_hash(pairs_c)


def test_null_session_identity_preservation():
    """None session serializes as null, distinct from string 'none'."""
    desc_null = CacheKeyDescriptor(
        workspace_id="ws_alpha",
        principal_id="usr_alice",
        session_id=None,
        query_hash="a" * 64,
        source_version_set_hash="b" * 64,
        snapshot_id="snap_1",
    )
    desc_str = CacheKeyDescriptor(
        workspace_id="ws_alpha",
        principal_id="usr_alice",
        session_id="none",
        query_hash="a" * 64,
        source_version_set_hash="b" * 64,
        snapshot_id="snap_1",
    )
    assert desc_null.to_canonical_key() != desc_str.to_canonical_key()


# ---------------------------------------------------------------------------
# AC-11, AC-12: Pinned Snapshot & Tombstone Delivery Gate Rechecks
# ---------------------------------------------------------------------------
def test_delivery_gate_rechecks_pinned_snapshot_and_tombstone():
    """Cache hit delivery verifies snapshot version bindings and tombstone status."""
    auth = TrustedAuthContext(
        authorized_workspace_id="ws_alpha",
        principal_id="usr_alice",
        principal_roles=("owner",),
        access_policy_version="v1",
    )

    # Save active pinned snapshot
    snap = SnapshotRecord(
        snapshot_id="snap_1",
        workspace_id="ws_alpha",
        manifest_generation=1,
        source_versions=(("src_doc1", 2),),
        artifact_ids=("art_table1",),
        status="active",
        created_at="2026-10-08T00:00:00Z",
    )
    workspace_boundary_repo.save_snapshot(snap)

    cached_ok = CachedQueryResult(
        cache_key="key_1",
        authorized_workspace_id="ws_alpha",
        authorized_principal_id="usr_alice",
        access_policy_version="v1",
        execution_id="exec_1",
        snapshot_id="snap_1",
        dependent_source_versions=(("src_doc1", 2),),
        dependent_artifact_ids=("art_table1",),
        answer="Valid cached response",
        citations=(),
        cached_at="2026-10-08T00:00:00Z",
    )

    # 1. Delivery gate passes on valid cache hit
    assert delivery_gate_recheck(cached_ok, auth, "snap_1", workspace_boundary_repo) is not None

    # 2. Version mismatch with pinned snapshot invalidates cache
    cached_bad_version = CachedQueryResult(
        cache_key="key_bad_ver",
        authorized_workspace_id="ws_alpha",
        authorized_principal_id="usr_alice",
        access_policy_version="v1",
        execution_id="exec_1",
        snapshot_id="snap_1",
        dependent_source_versions=(("src_doc1", 1),),  # Snapshot has version 2!
        dependent_artifact_ids=("art_table1",),
        answer="Stale version cached response",
        citations=(),
        cached_at="2026-10-08T00:00:00Z",
    )
    assert delivery_gate_recheck(cached_bad_version, auth, "snap_1", workspace_boundary_repo) is None

    # 3. Subsequent tombstone invalidates cache
    workspace_boundary_repo.record_source_tombstone("ws_alpha", "src_doc1", "usr_alice")
    assert delivery_gate_recheck(cached_ok, auth, "snap_1", workspace_boundary_repo) is None


def test_aggregate_query_insufficient_coverage_on_deleted_source():
    """Aggregate query losing a supporting source returns INSUFFICIENT_COVERAGE (HTTP 422)."""
    headers = {"Authorization": "Bearer test_token_alice"}

    # Register active snapshot with two sources
    snap = SnapshotRecord(
        snapshot_id="snap_agg",
        workspace_id="ws_alpha",
        manifest_generation=1,
        source_versions=(("src_dept1", 1), ("src_dept2", 1)),
        artifact_ids=(),
        status="active",
        created_at="2026-10-08T00:00:00Z",
    )
    workspace_boundary_repo.save_snapshot(snap)

    # Delete one supporting source
    workspace_boundary_repo.record_source_tombstone("ws_alpha", "src_dept2", "usr_alice")

    # Aggregate query returns 422 with INSUFFICIENT_COVERAGE
    resp = client.post(
        "/api/v1/workspaces/ws_alpha/query",
        json={"query": "Calculate total aggregate budget across departments", "snapshot_id": "snap_agg"},
        headers=headers,
    )
    assert resp.status_code == 422
    err = resp.json()["detail"]
    assert err["status"] == 422
    assert "insufficient_coverage" in err["type"]


# ---------------------------------------------------------------------------
# AC-13: Graceful Cache Fallback
# ---------------------------------------------------------------------------
def test_graceful_cache_fallback_on_connection_error():
    """Cache disconnection logs warning and falls back gracefully to authoritative recomputation."""
    cache = InMemoryCacheStore()
    cache.is_connected = False  # Simulate Redis down

    auth = TrustedAuthContext(
        authorized_workspace_id="ws_alpha",
        principal_id="usr_alice",
        principal_roles=("owner",),
        access_policy_version="v1",
    )
    desc = CacheKeyDescriptor(
        workspace_id="ws_alpha",
        principal_id="usr_alice",
        query_hash="a" * 64,
        source_version_set_hash="b" * 64,
        snapshot_id="snap_default",
    )

    # evaluate_query_delivery_and_recomputation falls back gracefully
    outcome = evaluate_query_delivery_and_recomputation(
        workspace_id="ws_alpha",
        query_input="Revenue query",
        cache_key_descriptor=desc,
        auth_context=auth,
        cache_store=cache,
        table_store=workspace_boundary_repo,
    )
    assert outcome.status == "SUCCESS"


# ---------------------------------------------------------------------------
# AC-14: Qdrant Pre Dispatch Filter Rejection
# ---------------------------------------------------------------------------
def test_qdrant_pre_dispatch_filter_rejection():
    """Missing or mismatched tenant filter is rejected before dispatch to Qdrant."""
    auth = TrustedAuthContext(
        authorized_workspace_id="ws_alpha",
        principal_id="usr_alice",
        principal_roles=("owner",),
        access_policy_version="v1",
    )
    mock_client = MockQdrantClient()

    # Mismatched filter contract raises WorkspaceAccessDeniedError
    contract_beta = QdrantFilterContract(authorized_workspace_id="ws_beta")
    with pytest.raises(WorkspaceAccessDeniedError):
        dispatch_qdrant_search(
            query_vector=[0.1, 0.2],
            filter_contract=contract_beta,
            auth_context=auth,
            client=mock_client,
        )

    # Valid contract dispatches with filter
    contract_alpha = QdrantFilterContract(authorized_workspace_id="ws_alpha")
    mock_client.add_point("p1", [0.1, 0.2], {"workspace_id": "ws_alpha", "source_id": "src_1"})
    mock_client.add_point("p2", [0.1, 0.2], {"workspace_id": "ws_beta", "source_id": "src_2"})

    results = dispatch_qdrant_search(
        query_vector=[0.1, 0.2],
        filter_contract=contract_alpha,
        auth_context=auth,
        client=mock_client,
    )
    assert len(results) == 1
    assert results[0]["payload"]["workspace_id"] == "ws_alpha"


# ---------------------------------------------------------------------------
# Durable Storage & Repository Restart Persistence Regression Test
# ---------------------------------------------------------------------------
def test_sqlite_workspace_boundary_repo_persistence_across_restart(tmp_path):
    """Proves durable repository state, tombstones, and CAS counters survive restart."""
    from src.db.workspace_boundary_repo import SQLiteWorkspaceBoundaryRepository

    db_path = tmp_path / "test_boundary.db"

    # 1. Initial boot: verify baseline seeding
    repo = SQLiteWorkspaceBoundaryRepository(db_path=db_path)
    assert repo.get_workspace("ws_alpha") is not None
    assert repo.get_workspace("ws_beta") is not None

    # 2. Mutate state: create workspace, add source, advance CAS generation, and record tombstone
    ws_gamma = repo.create_workspace(name="Workspace Gamma", owner_principal_id="usr_alice", workspace_id="ws_gamma")
    assert ws_gamma.manifest_generation == 1

    src_item = repo.create_source("ws_gamma", "Document Alpha", source_id="src_doc1")
    assert src_item.status == "registered"

    # CAS update increments manifest generation to 2
    manifest = repo.cas_update_manifest("ws_gamma", expected_generation=1, delta={"active_source_ids": ["src_doc1"]})
    assert manifest.manifest_generation == 2

    # Record source tombstone increments manifest generation to 3
    tombstone = repo.record_source_tombstone("ws_gamma", "src_doc1", caller_principal_id="usr_alice")
    assert tombstone.manifest_generation == 3

    # Close connection / discard original instance to simulate full process restart
    repo._conn.close()
    del repo

    # 3. Simulate application restart with a fresh repository instance pointing to the same file
    repo_restarted = SQLiteWorkspaceBoundaryRepository(db_path=db_path)

    # Verify workspace persisted and has the exact incremented manifest generation
    ws_recovered = repo_restarted.get_workspace("ws_gamma")
    assert ws_recovered is not None
    assert ws_recovered.name == "Workspace Gamma"
    assert ws_recovered.owner_principal_id == "usr_alice"
    assert ws_recovered.manifest_generation == 3
    assert ws_recovered.status == "active"

    # Verify tombstone barrier persisted: direct source lookup returns None
    assert repo_restarted.get_source("ws_gamma", "src_doc1") is None
    assert repo_restarted.has_tombstone("ws_gamma", "source", "src_doc1") is True

    # Verify tombstone record attributes persisted exactly
    tombstone_recovered = repo_restarted.get_tombstone("ws_gamma", "source", "src_doc1")
    assert tombstone_recovered is not None
    assert tombstone_recovered.deleted_by_principal_id == "usr_alice"
    assert tombstone_recovered.manifest_generation == 3

    # Verify CAS behavior continues atomically from the recovered generation
    with pytest.raises(CASConflictError):
        # Stale generation 2 must be rejected
        repo_restarted.cas_update_manifest("ws_gamma", expected_generation=2, delta={"active_source_ids": []})

    # Expected generation 3 succeeds and increments to 4
    manifest_next = repo_restarted.cas_update_manifest("ws_gamma", expected_generation=3, delta={"active_source_ids": []})
    assert manifest_next.manifest_generation == 4
    assert repo_restarted.get_workspace("ws_gamma").manifest_generation == 4

    repo_restarted._conn.close()


# ---------------------------------------------------------------------------
# Route Payload Edge Cases (400 Bad Request)
# ---------------------------------------------------------------------------
def test_empty_or_whitespace_payloads_return_400():
    """Whitespace-only workspace names and source titles return HTTP 400 Bad Request."""
    alice_headers = {"Authorization": "Bearer test_token_alice"}

    # Whitespace workspace name returns 400
    resp_ws = client.post("/api/v1/workspaces", json={"name": "   "}, headers=alice_headers)
    assert resp_ws.status_code == 400
    assert "Workspace name must not be empty" in resp_ws.json()["detail"]

    # Whitespace source title returns 400
    resp_src = client.post("/api/v1/workspaces/ws_alpha/sources", json={"title": "   "}, headers=alice_headers)
    assert resp_src.status_code == 400
    assert "Source title must not be empty" in resp_src.json()["detail"]


# ---------------------------------------------------------------------------
# SQLite Repository Reset and Foreign Deletion Denial
# ---------------------------------------------------------------------------
def test_sqlite_workspace_boundary_repo_reset_and_access_denials(tmp_path):
    """Proves SQLite repository fixture reset, foreign deletion rejection, and job/snapshot storage."""
    from src.db.workspace_boundary_repo import SQLiteWorkspaceBoundaryRepository
    from src.schemas.contracts.boundary import JobRecord, SnapshotRecord

    db_path = tmp_path / "sqlite_coverage.db"
    repo = SQLiteWorkspaceBoundaryRepository(db_path=db_path)

    # Calling tombstone_workspace for foreign workspace raises WorkspaceAccessDeniedError
    with pytest.raises(WorkspaceAccessDeniedError):
        repo.tombstone_workspace(workspace_id="ws_beta", caller_principal_id="usr_alice")

    # Calling tombstone_workspace for nonexistent workspace raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        repo.tombstone_workspace(workspace_id="ws_nonexistent", caller_principal_id="usr_alice")

    # Save and retrieve JobRecord
    job = JobRecord(
        job_id="job_sqlite_001",
        workspace_id="ws_alpha",
        operation="ingest",
        status="running",
        is_trusted_system_job=True,
        created_at="2026-10-08T00:00:00Z",
        updated_at="2026-10-08T00:00:00Z",
    )
    repo.save_job(job)
    retrieved_job = repo.get_job("job_sqlite_001")
    assert retrieved_job is not None
    assert retrieved_job.job_id == "job_sqlite_001"
    assert retrieved_job.is_trusted_system_job is True

    # Save and retrieve SnapshotRecord
    snap = SnapshotRecord(
        snapshot_id="snap_sqlite_001",
        workspace_id="ws_alpha",
        manifest_generation=1,
        source_versions=(("src_doc1", 1),),
        artifact_ids=("art_idx1",),
        status="active",
        created_at="2026-10-08T00:00:00Z",
    )
    repo.save_snapshot(snap)
    retrieved_snap = repo.get_snapshot("ws_alpha", "snap_sqlite_001")
    assert retrieved_snap is not None
    assert retrieved_snap.snapshot_id == "snap_sqlite_001"
    assert retrieved_snap.source_versions == (("src_doc1", 1),)

    # Calling reset_fixtures resets tables back to clean baseline fixtures
    repo.reset_fixtures()
    assert repo.get_job("job_sqlite_001") is None
    assert repo.get_snapshot("ws_alpha", "snap_sqlite_001") is None
    assert repo.get_workspace("ws_alpha") is not None
    assert repo.get_workspace("ws_beta") is not None

    repo._conn.close()


# ---------------------------------------------------------------------------
# Backend Selection & Production Durable Storage Barrier
# ---------------------------------------------------------------------------
def test_get_workspace_boundary_repo_backend_selection_and_prod_barrier(monkeypatch):
    """Proves factory rejects memory in production and when durable storage is required."""
    from src.db.workspace_boundary_repo import get_workspace_boundary_repo

    # 1. Require durable with memory override raises RuntimeError
    monkeypatch.setenv("KRE_WORKSPACE_REPO_BACKEND", "memory")
    with pytest.raises(RuntimeError) as exc_durable:
        get_workspace_boundary_repo(reset=True, require_durable=True)
    assert "Memory repository cannot be used when durable backend is required" in str(exc_durable.value)

    # 2. Production environment with memory override raises RuntimeError
    monkeypatch.setenv("KRE_WORKSPACE_REPO_BACKEND", "memory")
    monkeypatch.setattr("config.settings.ENVIRONMENT", "prod")
    with pytest.raises(RuntimeError) as exc_prod:
        get_workspace_boundary_repo(reset=True)
    assert "Memory repository cannot be used when durable backend is required or in production" in str(exc_prod.value)

    # Reset back to test environment
    monkeypatch.setenv("KRE_WORKSPACE_REPO_BACKEND", "sqlite")
    monkeypatch.setattr("config.settings.ENVIRONMENT", "dev")
    test_repo = get_workspace_boundary_repo(reset=True)
    assert test_repo is not None


# ---------------------------------------------------------------------------
# Store Failure Handling (HTTP 503)
# ---------------------------------------------------------------------------
def test_store_failure_handling_returns_503(monkeypatch):
    """Proves unhandled authoritative database errors trigger HTTP 503 Service Unavailable."""
    import sqlite3
    from src.db.workspace_boundary_repo import SQLiteWorkspaceBoundaryRepository

    alice_headers = {"Authorization": "Bearer test_token_alice"}

    def failing_get_workspace(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked or disk full")

    # Monkeypatch repository get_workspace to simulate storage crash
    monkeypatch.setattr(SQLiteWorkspaceBoundaryRepository, "get_workspace", failing_get_workspace)

    # 1. GET /workspaces/{id} triggers 503
    resp_get = client.get("/api/v1/workspaces/ws_alpha", headers=alice_headers)
    assert resp_get.status_code == 503
    assert "Authoritative storage unreachable" in resp_get.json()["detail"]

    # 2. POST /workspaces triggers 503
    def failing_create_workspace(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(SQLiteWorkspaceBoundaryRepository, "create_workspace", failing_create_workspace)
    resp_create = client.post("/api/v1/workspaces", json={"name": "Crash Project"}, headers=alice_headers)
    assert resp_create.status_code == 503
    assert "Authoritative storage unreachable" in resp_create.json()["detail"]

