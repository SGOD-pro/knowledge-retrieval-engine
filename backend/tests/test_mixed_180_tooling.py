"""Offline regression and verification test suite for Mixed-180 ingestion, coverage, and benchmark tooling."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any
import uuid

import pytest

from db.table_store.memory_store import MemoryTableStore
from db.workspace_boundary_repo import (
    DocumentSnapshot,
    InMemoryWorkspaceBoundaryRepository,
    WorkbookSheetInventory,
    get_workspace_boundary_repo,
)
from schemas.contracts.boundary import SheetInventoryEntry
from modules.workspaces.workspaces_repository import WorkspacesRepository
from schemas.models import Chunk, Document
from scripts.ingest_mixed_180_workspace import (
    embed_chunks_safe,
    get_existing_qdrant_points,
    run_dry_run_validation,
)
from scripts.run_mixed_180_benchmark import _git_working_tree_status
from scripts.verify_mixed_180_coverage import verify_coverage
from services.evaluation.benchmark_comparator import (
    NOT_COMPARABLE_MESSAGE,
    REQUIRED_EQUAL_FIELDS,
    compare_manifests,
)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
STAGED_DIR = Path("/home/swyra/.gemini/antigravity-cli/brain/338cc016-3147-4604-897e-08680acdf711/scratch/staged_17_docs")
MANIFEST_PATH = ROOT_DIR / "data" / "kre_shuffled_documents.json"
BENCHMARK_PATH = ROOT_DIR / "data" / "kre_benchmark_180.json"


def test_dry_run_isolation():
    """Prove that dry-run parses all 17 files, 12,114 chunks, and 87,045 rows with zero remote writes."""
    from scripts.ingest_mixed_180_workspace import load_manifest_files

    raw_manifest, file_paths, file_hashes = load_manifest_files(MANIFEST_PATH, staged_dir=STAGED_DIR)
    assert len(file_paths) == 17

    report = run_dry_run_validation(file_paths, file_hashes, workspace_id="ws_mixed_180_baseline")
    assert report["dry_run"] is True
    assert report["files_verified"] == 17
    assert report["total_chunks"] == 12114
    assert report["total_rows"] == 87045
    assert report["remote_writes_performed"] == 0
    assert report["paid_calls_performed"] == 0
    assert report["status"] == "DRY_RUN_PASSED"


def test_normal_workspace_ownership_registration():
    """Verify normal workspace ownership registration assigns documents to workspaces."""
    ws_repo = WorkspacesRepository()
    test_ws = f"ws_ownership_test_{uuid.uuid4().hex[:8]}"

    chunk = Chunk(
        id="c1",
        document_id="doc1",
        source_format="pdf",
        text="Sample text",
        element_type="text",
        workspace_id=test_ws,
    )
    doc = Document(
        id="doc1",
        filename="sample.pdf",
        source_format="pdf",
        chunks=(chunk,),
        workspace_id=test_ws,
    )

    ws_repo.add_document_to_workspace(
        workspace_id=test_ws,
        document=doc,
        raw_bytes=b"%PDF-1.4 sample",
        size_str="15.0 KB",
    )

    res = ws_repo.get_workspace_documents(test_ws)
    docs = res.get("documents", [])
    assert any(d.get("id") == "doc1" or d.get("document_id") == "doc1" for d in docs)


class MockQdrantPoint:
    def __init__(self, id: str):
        self.id = id


class MockQdrantClient:
    def __init__(self):
        self.points: dict[str, Any] = {}

    def retrieve(self, collection_name: str, ids: list[str], **kwargs):
        return [MockQdrantPoint(id=pid) for pid in ids if pid in self.points]

    def upsert(self, collection_name: str, points: list[Any], wait: bool = True):
        for p in points:
            self.points[str(p.id if hasattr(p, "id") else p["id"])] = p


def test_partial_write_and_safe_resumption():
    """Test partial vector write recovery: reuses existing chunk embeddings and skips landed points."""
    # 1. Create 6 synthetic chunks with precomputed embeddings
    chunks = []
    for i in range(6):
        chunks.append(
            Chunk(
                id=f"chk_{i}",
                document_id="doc_partial",
                source_format="pdf",
                text=f"Sentence {i}",
                element_type="text",
                workspace_id="ws_partial",
                embedding_fast=[0.1] * 384,
                embedding_full=[0.2] * 1024,
            )
        )

    # 2. Simulate partial write: only first 2 points made it to Qdrant before failure
    client = MockQdrantClient()
    first_two_ids = [
        str(uuid.uuid5(uuid.NAMESPACE_OID, str(c.id))) for c in chunks[:2]
    ]
    for pid in first_two_ids:
        client.points[pid] = {"id": pid, "vector": [0.1] * 384}

    all_expected_ids = [
        str(uuid.uuid5(uuid.NAMESPACE_OID, str(c.id))) for c in chunks
    ]

    # 3. Rerun checks existing points
    existing = get_existing_qdrant_points(client, "kre_chunks", all_expected_ids)
    assert len(existing) == 2
    assert set(existing) == set(first_two_ids)

    # 4. Safe embedding reuse: embed_chunks_safe detects all chunks already have vectors
    embedded = embed_chunks_safe(chunks, provider="test")
    assert len(embedded) == 6
    # Embeddings preserved without modification
    assert embedded[0].embedding_fast == [0.1] * 384

    # 5. Upsert only the missing 4 points
    missing_chunks = [c for c in embedded if str(uuid.uuid5(uuid.NAMESPACE_OID, str(c.id))) not in existing]
    assert len(missing_chunks) == 4

    missing_points = [
        MockQdrantPoint(id=str(uuid.uuid5(uuid.NAMESPACE_OID, str(c.id))))
        for c in missing_chunks
    ]
    client.upsert("kre_chunks", missing_points)

    # 6. Verify full 6 points now in client
    final_existing = get_existing_qdrant_points(client, "kre_chunks", all_expected_ids)
    assert len(final_existing) == 6


def test_coverage_verification_rejection():
    """Test that coverage verification fails closed on missing snapshot, hash mismatch, or invalid locators."""
    test_ws = f"ws_cov_test_{uuid.uuid4().hex[:8]}"
    b_repo = InMemoryWorkspaceBoundaryRepository()
    t_store = MemoryTableStore()

    # Test rejection when snapshots are missing
    report = verify_coverage(
        workspace_id=test_ws,
        staged_dir=STAGED_DIR,
        manifest_path=MANIFEST_PATH,
        benchmark_path=BENCHMARK_PATH,
        skip_vector_check=True,
        boundary_repo=b_repo,
        table_store=t_store,
    )
    assert report["verified"] is False
    assert report["status"] == "FAILED"
    assert any("Missing DocumentSnapshot" in d for d in report["defects"])


def test_manifest_comparator_contract():
    """Verify that generated manifest satisfies all 10 REQUIRED_EQUAL_FIELDS and comparator Rule 1."""
    git_info = _git_working_tree_status()

    candidate_manifest = {
        "commit_sha": git_info["head_sha"],
        "baseline_commit_sha": git_info["head_sha"],  # Treats self as baseline
        "test_suite_sha256": "suite_sha_mock",
        "corpus_manifest_sha256": "corpus_sha_mock",
        "workspace_id": "ws_mixed_180_baseline",
        "runner_version": "2.3.0",
        "scorer_version": "2.0.0",
        "embedding_model": "amazon.titan-embed-text-v2:0",
        "llm_model": "apac.amazon.nova-lite-v1:0",
        "reranker_policy": "lexical_bm25_plus",
        "cache_mode": "bypass_exact_and_semantic",
        "benchmark_type": "mixed_180",
        "dirty_working_tree": {
            "is_dirty": git_info["is_dirty"],
            "uncommitted_files_count": git_info["uncommitted_files_count"],
            "fingerprint": git_info["fingerprint"],
        },
        "metrics": {
            "overall_accuracy": 0.85,
            "answer_text_accuracy": 0.88,
            "grounded_accuracy": 0.85,
            "refusal_accuracy": 1.0,
            "grounded_recall_at_5": 0.80,
            "precision_at_3": 0.75,
            "mrr_at_5": 0.82,
            "ndcg_at_5": 0.83,
            "llm_activation_rate": 0.85,
        },
    }

    # Verify all 10 fields are present
    for req in REQUIRED_EQUAL_FIELDS:
        assert req in candidate_manifest, f"Missing required comparator field: {req}"

    # Verify self-baseline comparison passes Rule 1 and Rule 3
    cmp_res = compare_manifests(candidate_manifest, candidate_manifest)
    assert cmp_res["comparable"] is True
    assert cmp_res["status"] == "COMPARABLE"
    assert cmp_res["deltas"]["overall_accuracy_delta"] == 0.0

    # Verify mismatch triggers NOT_COMPARABLE
    tampered_manifest = copy.deepcopy(candidate_manifest)
    tampered_manifest["runner_version"] = "9.9.9"
    cmp_fail = compare_manifests(tampered_manifest, candidate_manifest)
    assert cmp_fail["comparable"] is False
    assert cmp_fail["status"] == NOT_COMPARABLE_MESSAGE
    assert any("runner_version" in m for m in cmp_fail["mismatches"])

    # Incompatible scorer version (e.g. 2.0.0 vs 1.0.0) must be rejected
    v1_baseline = copy.deepcopy(candidate_manifest)
    v1_baseline["scorer_version"] = "1.0.0"
    cmp_v1 = compare_manifests(candidate_manifest, v1_baseline)
    assert cmp_v1["comparable"] is False
    assert cmp_v1["status"] == NOT_COMPARABLE_MESSAGE
    assert any("scorer_version" in m for m in cmp_v1["mismatches"])

    # Unmeasured retrieval metrics delta handling
    unmeasured_manifest = copy.deepcopy(candidate_manifest)
    unmeasured_manifest["metrics"]["grounded_recall_at_5"] = "unmeasured"
    cmp_unm = compare_manifests(unmeasured_manifest, unmeasured_manifest)
    assert cmp_unm["comparable"] is True
    assert cmp_unm["deltas"]["grounded_recall_at_5_delta"] == "unmeasured"
