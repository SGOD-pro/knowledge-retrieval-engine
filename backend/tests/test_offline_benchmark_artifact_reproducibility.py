"""Tests for offline canonical benchmark artifact reproducibility and EvidenceItem round-trip.

Proves:
  1. EvidenceItem serialization via to_dict and restoration via from_dict and restore_evidence_item.
  2. Offline rescoring produces identical grounded grades to online scoring for:
     - Supported text retrieval answers
     - Supported structured aggregate answers
     - Supported refusals
  3. Negative controls (unsupported claims, fabricated structured results, refusal with citations)
     fail identically online and offline after JSON round-trip.
  4. Scorer version is distinct ("2.2.0") and strict comparator gates reject comparisons against
     historical 2.1.0 manifests while accepting matching 2.2.0 manifests.
"""

import json
import pytest

from services.evaluation.benchmark_scorer import (
    SCORER_VERSION,
    evaluate_grounded_answer,
    rescore_query_record_offline,
)
from services.evaluation.benchmark_comparator import (
    compare_manifests,
    NOT_COMPARABLE_MESSAGE,
)
from services.retrieval.evidence_contract import (
    EvidenceItem,
    restore_evidence_item,
)


# --- 1. EvidenceItem Serialization and Restoration Round-trip ---

def test_evidence_item_to_dict_and_from_dict_json_roundtrip():
    """Verify EvidenceItem serializes to dict and restores identically through JSON."""
    item = EvidenceItem(
        evidence_id="ev_chunk_101",
        workspace_id="ws_canonical_comparison_0004",
        document_id="492ccb2d-0d0d-5022-8a7b-e3f87df8befd",
        document_version="3518726963892d32ec11a876a5dfa448",
        source_type="chunk",
        locator={
            "chunk_id": "492ccb2d-0d0d-5022-8a7b-e3f87df8befd:page:1:element:0",
            "page_number": 1,
            "location_reference": "Page: 1",
            "section_path": ["Item 1", "Financial Statements"],
        },
        text="Commission File Number: 001-36743. Apple Inc.",
        structured_payload=None,
        score=0.985,
        strategy="vector_rerank",
        provenance={"source_format": "pdf", "metadata": {"page": 1}},
        citation_payload={
            "chunk_id": "492ccb2d-0d0d-5022-8a7b-e3f87df8befd:page:1:element:0",
            "document_id": "492ccb2d-0d0d-5022-8a7b-e3f87df8befd",
            "document_filename": "SEC-Form-10Q.pdf",
            "location_reference": "Page: 1",
            "page_number": 1,
            "strategy": "vector_rerank",
        },
    )

    item_dict = item.to_dict()
    assert isinstance(item_dict, dict)

    json_str = json.dumps(item_dict)
    loaded_dict = json.loads(json_str)

    restored = restore_evidence_item(loaded_dict)
    assert isinstance(restored, EvidenceItem)
    assert restored.evidence_id == item.evidence_id
    assert restored.workspace_id == item.workspace_id
    assert restored.document_id == item.document_id
    assert restored.document_version == item.document_version
    assert restored.source_type == item.source_type
    assert restored.locator == item.locator
    assert restored.text == item.text
    assert restored.structured_payload == item.structured_payload
    assert restored.score == pytest.approx(item.score)
    assert restored.strategy == item.strategy
    assert restored.provenance == item.provenance
    assert restored.citation_payload == item.citation_payload
    assert restored == item


def test_restore_evidence_item_is_idempotent():
    """Verify restore_evidence_item returns the object unmodified if already EvidenceItem."""
    item = EvidenceItem(
        evidence_id="ev_001",
        workspace_id="ws_001",
        document_id="doc_001",
        document_version="v1",
        source_type="chunk",
        locator={},
        text="sample text",
        structured_payload=None,
        score=1.0,
        strategy="bm25",
        provenance={},
        citation_payload={},
    )
    restored = restore_evidence_item(item)
    assert restored is item


# --- 2. Positive Controls: Supported Text Answer ---

def test_supported_text_answer_json_roundtrip_identical_grades():
    """Verify supported factual text answer grades identically online and offline after JSON round-trip."""
    chunk_id = "492ccb2d-0d0d-5022-8a7b-e3f87df8befd:page:1:element:0"
    doc_id = "492ccb2d-0d0d-5022-8a7b-e3f87df8befd"
    doc_ver = "3518726963892d32ec11a876a5dfa448"
    fname = "SEC-Form-10Q.pdf"
    ans_text = "Apple's Commission File Number is 001-36743."

    ev_item = EvidenceItem(
        evidence_id=chunk_id,
        workspace_id="ws_canonical_comparison_0004",
        document_id=doc_id,
        document_version=doc_ver,
        source_type="chunk",
        locator={"chunk_id": chunk_id, "page_number": 1, "location_reference": "Page: 1"},
        text="UNITED STATES SECURITIES AND EXCHANGE COMMISSION Commission File Number: 001-36743 Apple Inc.",
        structured_payload=None,
        score=0.95,
        strategy="vector_rerank",
        provenance={"source_format": "pdf"},
        citation_payload={
            "chunk_id": chunk_id,
            "document_id": doc_id,
            "document_filename": fname,
            "page_number": 1,
            "location_reference": "Page: 1",
            "strategy": "vector_rerank",
        },
    )

    citations = [
        {
            "chunk_id": chunk_id,
            "document_id": doc_id,
            "document_filename": fname,
            "document_version": doc_ver,
            "page_number": 1,
            "location_reference": "Page: 1",
            "strategy": "vector_rerank",
            "text": "Commission File Number: 001-36743",
        }
    ]

    expected_sources = [fname]
    expected_version = doc_ver
    ground_truth_chunk_ids = [chunk_id]
    context_chunk_ids = [chunk_id]

    # Online evaluation
    online_eval = evaluate_grounded_answer(
        answer=ans_text,
        citations=citations,
        is_answer_text_correct=True,
        contract_type="semantic",
        contract={"contract_type": "semantic"},
        expected_sources=expected_sources,
        expected_source_version=expected_version,
        retained_evidence_items=[ev_item],
        context_chunk_ids=context_chunk_ids,
        ground_truth_chunk_ids=ground_truth_chunk_ids,
        is_infra_error=False,
        ans_is_refusal=False,
        question_type="retrieval",
    )
    assert online_eval["is_grounded_correct"] is True
    assert online_eval["reason"] == "grounded_and_verified"
    assert online_eval["citation_validation"]["valid"] is True

    # Build saved query record dictionary matching run_canonical_60_benchmark.py
    saved_record = {
        "id": "Q136",
        "category": "factual",
        "question": "What is Apple's Commission File Number?",
        "expected_answer": "001-36743",
        "answer": ans_text,
        "is_correct": True,
        "answer_text_correct": True,
        "is_grounded_correct": online_eval["is_grounded_correct"],
        "grounded_correct": online_eval["grounded_correct"],
        "grounded_evaluation_reason": online_eval["reason"],
        "is_refusal": False,
        "executed_path": "full",
        "planned_path": "full",
        "reranker_mode": "circuit_open_lexical_fallback",
        "latency_ms": 120.5,
        "reranked_chunk_ids": [chunk_id],
        "compressed_context_chunk_ids": context_chunk_ids,
        "llm_cited_chunk_ids": [chunk_id],
        "final_citation_chunk_ids": [chunk_id],
        "citations": citations,
        # Persisted request-retained evidence and evaluation inputs
        "retained_evidence_items": [ev_item.to_dict()],
        "contract_type": "semantic",
        "contract": {"contract_type": "semantic"},
        "question_type": "retrieval",
        "expected_sources": expected_sources,
        "expected_source_version": expected_version,
        "ground_truth_chunk_ids": ground_truth_chunk_ids,
        "is_infra_error": False,
        "ans_is_refusal": False,
        "structured_aggregate": False,
        "execution_type": "",
    }

    # Serialize to JSON and reload
    json_bytes = json.dumps(saved_record)
    reloaded_record = json.loads(json_bytes)

    # Offline evaluation from reloaded JSON record
    offline_eval = rescore_query_record_offline(reloaded_record)

    # Prove identical grades and validation outcomes
    assert offline_eval["is_grounded_correct"] == online_eval["is_grounded_correct"] is True
    assert offline_eval["grounded_correct"] == online_eval["grounded_correct"] is True
    assert offline_eval["reason"] == online_eval["reason"] == "grounded_and_verified"
    assert offline_eval["citation_validation"]["valid"] == online_eval["citation_validation"]["valid"] is True
    assert offline_eval["citation_validation"]["errors"] == online_eval["citation_validation"]["errors"] == []


# --- 3. Positive Controls: Structured Results ---

def test_supported_structured_result_json_roundtrip_identical_grades():
    """Verify supported structured aggregate answer grades identically online and offline after JSON round-trip."""
    doc_id = "34202080-60de-532e-a0c2-01f45b0af39a"
    doc_ver = "c029d234eefd3fee2563b9c0929946ca"
    sel_hash = "3985934bdd904a02"
    table_id = "survay"
    ans_text = "It fell by 4,191 Dollars (millions), from 980,268 to 976,077, a decline of 0.43%."

    ev_item = EvidenceItem(
        evidence_id=f"table_{table_id}_{sel_hash}",
        workspace_id="ws_canonical_comparison_0004",
        document_id=doc_id,
        document_version=doc_ver,
        source_type="table_row_set",
        locator={
            "table_id": table_id,
            "selection_hash": sel_hash,
            "selection_count": 2,
            "total_rows_in_table": 60255,
            "location_reference": "2 row(s) from survay",
        },
        text=ans_text,
        structured_payload={
            "table_id": table_id,
            "document_version": doc_ver,
            "operator": "compare",
            "target_column": "Value",
            "selection_hash": sel_hash,
            "selection_count": 2,
            "result_value": "4191",
            "secondary_value": "0.43",
        },
        score=1.0,
        strategy="structured_table",
        provenance={"table_id": table_id},
        citation_payload={
            "evidence_type": "structured_aggregate",
            "document_id": doc_id,
            "document_filename": "survay.csv",
            "document_version": doc_ver,
            "table_id": table_id,
            "operator": "compare",
            "target_column": "Value",
            "selection_count": 2,
            "total_rows_in_table": 60255,
            "selection_hash": sel_hash,
            "result_value": "4191",
            "secondary_value": "0.43",
            "location_reference": "2 row(s) from survay",
            "strategy": "structured_table",
        },
    )

    citations = [ev_item.citation_payload]
    contract = {
        "contract_type": "numeric",
        "target_value": "4191",
        "operator": "compare",
        "tolerance": 0.01,
    }

    # Online evaluation
    online_eval = evaluate_grounded_answer(
        answer=ans_text,
        citations=citations,
        is_answer_text_correct=True,
        contract_type="structured_aggregate",
        contract=contract,
        expected_sources=["survay.csv"],
        expected_source_version=doc_ver,
        retained_evidence_items=[ev_item],
        is_infra_error=False,
        ans_is_refusal=False,
        question_type="structured_aggregate",
        outcome={"structured_aggregate": True, "execution_type": "deterministic"},
    )
    assert online_eval["is_grounded_correct"] is True
    assert online_eval["reason"] == "grounded_structured_aggregate"
    assert online_eval["citation_validation"]["valid"] is True

    # Build saved query record
    saved_record = {
        "id": "Q153",
        "category": "aggregation",
        "question": "By how much did Total income change between 2024 and 2025 for All industries?",
        "expected_answer": "fell by 4,191",
        "answer": ans_text,
        "is_correct": True,
        "answer_text_correct": True,
        "is_grounded_correct": online_eval["is_grounded_correct"],
        "grounded_correct": online_eval["grounded_correct"],
        "grounded_evaluation_reason": online_eval["reason"],
        "is_refusal": False,
        "executed_path": "structured_aggregate",
        "planned_path": "structured_aggregate",
        "reranker_mode": "bypassed",
        "latency_ms": 45.2,
        "citations": citations,
        # Persisted request-retained evidence and evaluation inputs
        "retained_evidence_items": [ev_item.to_dict()],
        "contract_type": "structured_aggregate",
        "contract": contract,
        "question_type": "structured_aggregate",
        "expected_sources": ["survay.csv"],
        "expected_source_version": doc_ver,
        "ground_truth_chunk_ids": [],
        "is_infra_error": False,
        "ans_is_refusal": False,
        "structured_aggregate": True,
        "execution_type": "deterministic",
    }

    # JSON round-trip
    json_bytes = json.dumps(saved_record)
    reloaded_record = json.loads(json_bytes)

    # Offline evaluation
    offline_eval = rescore_query_record_offline(reloaded_record)

    # Prove identical grades
    assert offline_eval["is_grounded_correct"] == online_eval["is_grounded_correct"] is True
    assert offline_eval["grounded_correct"] == online_eval["grounded_correct"] is True
    assert offline_eval["reason"] == online_eval["reason"] == "grounded_structured_aggregate"
    assert offline_eval["citation_validation"]["valid"] == online_eval["citation_validation"]["valid"] is True


# --- 4. Positive Controls: Refusal Contract ---

def test_supported_refusal_json_roundtrip_identical_grades():
    """Verify clean refusal answer grades identically online and offline after JSON round-trip."""
    ans_text = "I couldn't find any relevant passages in the workspace documents matching your query."
    contract = {"contract_type": "refusal", "is_refusal_required": True}

    # Online evaluation
    online_eval = evaluate_grounded_answer(
        answer=ans_text,
        citations=[],
        is_answer_text_correct=True,
        contract_type="refusal",
        contract=contract,
        expected_sources=[],
        expected_source_version=None,
        retained_evidence_items=[],
        is_infra_error=False,
        ans_is_refusal=True,
        question_type="retrieval",
    )
    assert online_eval["is_grounded_correct"] is True
    assert online_eval["reason"] == "valid_unsupported_refusal"
    assert online_eval["citation_validation"]["valid"] is True

    # Build saved query record
    saved_record = {
        "id": "Q151",
        "category": "refusal",
        "question": "What is the correlation between clean cooking fuel and literacy in South Andaman?",
        "expected_answer": "NOT_FOUND",
        "answer": ans_text,
        "is_correct": True,
        "answer_text_correct": True,
        "is_grounded_correct": online_eval["is_grounded_correct"],
        "grounded_correct": online_eval["grounded_correct"],
        "grounded_evaluation_reason": online_eval["reason"],
        "is_refusal": True,
        "executed_path": "full",
        "planned_path": "full",
        "reranker_mode": "bypassed",
        "latency_ms": 25.0,
        "citations": [],
        # Persisted request-retained evidence and evaluation inputs
        "retained_evidence_items": [],
        "contract_type": "refusal",
        "contract": contract,
        "question_type": "retrieval",
        "expected_sources": [],
        "expected_source_version": None,
        "ground_truth_chunk_ids": [],
        "is_infra_error": False,
        "ans_is_refusal": True,
        "structured_aggregate": False,
        "execution_type": "",
    }

    # JSON round-trip
    json_bytes = json.dumps(saved_record)
    reloaded_record = json.loads(json_bytes)

    # Offline evaluation
    offline_eval = rescore_query_record_offline(reloaded_record)

    # Prove identical grades
    assert offline_eval["is_grounded_correct"] == online_eval["is_grounded_correct"] is True
    assert offline_eval["grounded_correct"] == online_eval["grounded_correct"] is True
    assert offline_eval["reason"] == online_eval["reason"] == "valid_unsupported_refusal"
    assert offline_eval["citation_validation"]["valid"] == online_eval["citation_validation"]["valid"] is True


# --- 5. Negative Controls: Deserialized Records Failing Correctly ---

def test_offline_rescore_fails_on_fabricated_citation_snippet():
    """Verify offline rescoring fails when citation snippet is fabricated and absent from retained evidence."""
    chunk_id = "chunk_sec_001"
    doc_id = "doc_sec_01"
    fname = "SEC-Form-10Q.pdf"

    # Retained evidence does not contain 999-99999
    ev_item = EvidenceItem(
        evidence_id=chunk_id,
        workspace_id="ws_canonical_comparison_0004",
        document_id=doc_id,
        document_version="v1",
        source_type="chunk",
        locator={"chunk_id": chunk_id, "page_number": 1, "location_reference": "Page: 1"},
        text="The Commission regulates reporting under statutory rules.",
        structured_payload=None,
        score=0.9,
        strategy="vector_rerank",
        provenance={},
        citation_payload={"chunk_id": chunk_id, "document_id": doc_id, "document_filename": fname, "page_number": 1},
    )

    # Citation snippet fabricates Commission File Number 001-36743
    saved_record = {
        "id": "Q136",
        "answer": "Apple's Commission File Number is 001-36743.",
        "is_correct": True,
        "answer_text_correct": True,
        "citations": [
            {
                "chunk_id": chunk_id,
                "document_id": doc_id,
                "document_filename": fname,
                "document_version": "v1",
                "page_number": 1,
                "location_reference": "Page: 1",
                "text": "Commission File Number is 001-36743.",  # fabricated citation snippet
            }
        ],
        "retained_evidence_items": [ev_item.to_dict()],
        "contract_type": "semantic",
        "contract": {"contract_type": "semantic"},
        "question_type": "retrieval",
        "expected_sources": [fname],
        "expected_source_version": "v1",
        "ground_truth_chunk_ids": [chunk_id],
        "is_infra_error": False,
        "ans_is_refusal": False,
    }

    json_str = json.dumps(saved_record)
    reloaded = json.loads(json_str)

    res = rescore_query_record_offline(reloaded)
    assert res["is_grounded_correct"] is False
    assert "fabricates supporting content" in res["reason"] or "invalid_citations" in res["reason"]


def test_offline_rescore_fails_on_fabricated_structured_value():
    """Verify offline rescoring fails when structured citation fabricates secondary value 999."""
    doc_id = "34202080-60de-532e-a0c2-01f45b0af39a"
    sel_hash = "38f9121b44ab67f9"
    table_id = "survay"

    # Retained execution only has result_value = 100
    ev_item = EvidenceItem(
        evidence_id=f"table_{table_id}_{sel_hash}",
        workspace_id="ws_canonical_comparison_0004",
        document_id=doc_id,
        document_version="v1",
        source_type="table_row_set",
        locator={"table_id": table_id, "selection_hash": sel_hash, "selection_count": 10},
        text="Sum is 100",
        structured_payload={
            "table_id": table_id,
            "document_version": "v1",
            "operator": "sum",
            "target_column": "Value",
            "selection_hash": sel_hash,
            "selection_count": 10,
            "result_value": "100",
            # secondary_value is absent in retained execution
        },
        score=1.0,
        strategy="structured_table",
        provenance={},
        citation_payload={},
    )

    # Citation claims secondary_value = 999
    saved_record = {
        "id": "Q_agg_fab",
        "answer": "The secondary value is 999.",
        "is_correct": True,
        "citations": [
            {
                "evidence_type": "structured_aggregate",
                "document_id": doc_id,
                "document_filename": "survay.csv",
                "document_version": "v1",
                "table_id": table_id,
                "operator": "sum",
                "target_column": "Value",
                "selection_hash": sel_hash,
                "selection_count": 10,
                "result_value": "100",
                "secondary_value": "999",  # fabricated secondary value
                "location_reference": "10 rows",
            }
        ],
        "retained_evidence_items": [ev_item.to_dict()],
        "contract_type": "structured_aggregate",
        "contract": {"contract_type": "numeric", "target_value": "999", "operator": "sum"},
        "question_type": "structured_aggregate",
        "expected_sources": ["survay.csv"],
        "expected_source_version": "v1",
        "ground_truth_chunk_ids": [],
        "is_infra_error": False,
        "ans_is_refusal": False,
        "structured_aggregate": True,
        "execution_type": "deterministic",
    }

    json_str = json.dumps(saved_record)
    reloaded = json.loads(json_str)

    res = rescore_query_record_offline(reloaded)
    assert res["is_grounded_correct"] is False
    assert "fabricated" in res["reason"] or "invalid_citations" in res["reason"] or "claim_unsupported" in res["reason"]


def test_offline_rescore_fails_on_refusal_with_fabricated_citations():
    """Verify offline rescoring rejects refusals that attach citations."""
    saved_record = {
        "id": "Q_ref_fab",
        "answer": "I could not find any information.",
        "is_correct": True,
        "citations": [{"chunk_id": "c1", "document_id": "d1", "document_filename": "f.pdf", "page_number": 1}],
        "retained_evidence_items": [],
        "contract_type": "refusal",
        "contract": {"contract_type": "refusal", "is_refusal_required": True},
        "question_type": "retrieval",
        "is_infra_error": False,
        "ans_is_refusal": True,
    }
    json_str = json.dumps(saved_record)
    reloaded = json.loads(json_str)

    res = rescore_query_record_offline(reloaded)
    assert res["is_grounded_correct"] is False
    assert res["reason"] == "refusal_contract_with_fabricated_citations"


# --- 6. Scorer Version and Comparator Gates ---

def test_scorer_version_is_distinct():
    """Verify scorer version is distinctly set to 2.2.0."""
    assert SCORER_VERSION == "2.2.0"


def test_comparator_preserves_strict_gates_against_historical_manifest():
    """Verify compare_manifests strictly rejects comparisons between 2.2.0 candidate and 2.1.0 baseline."""
    base_manifest_2_1 = {
        "commit_sha": "a50c0a4f127c7418ffd79447430a9093d8b37f66",
        "baseline_commit_sha": "root_commit",
        "test_suite_sha256": "suite_sha_123",
        "corpus_manifest_sha256": "corpus_sha_456",
        "workspace_id": "ws_canonical_comparison_0004",
        "runner_version": "2.1.0",
        "scorer_version": "2.1.0",
        "embedding_model": "amazon.titan-embed-text-v2:0",
        "llm_model": "apac.amazon.nova-lite-v1:0",
        "reranker_policy": "openrouter:nvidia/llama-nemotron-rerank-vl-1b-v2:free (fallback: lexical_bm25_plus)",
        "cache_mode": "bypass_exact_and_semantic",
        "benchmark_type": "cold",
        "metrics": {"overall_accuracy": 0.5333},
    }

    cand_manifest_2_2 = dict(base_manifest_2_1)
    cand_manifest_2_2["commit_sha"] = "cand_commit_888"
    cand_manifest_2_2["baseline_commit_sha"] = "a50c0a4f127c7418ffd79447430a9093d8b37f66"
    cand_manifest_2_2["runner_version"] = "2.2.0"
    cand_manifest_2_2["scorer_version"] = "2.2.0"

    res = compare_manifests(cand_manifest_2_2, base_manifest_2_1)
    assert res["comparable"] is False
    assert res["status"] == NOT_COMPARABLE_MESSAGE
    assert any("scorer_version" in m for m in res["mismatches"])
    assert any("runner_version" in m for m in res["mismatches"])


def test_comparator_accepts_matching_2_2_manifests():
    """Verify compare_manifests accepts comparisons between matching 2.2.0 manifests."""
    base_manifest_2_2 = {
        "commit_sha": "base_commit_777",
        "baseline_commit_sha": "root_commit",
        "test_suite_sha256": "suite_sha_123",
        "corpus_manifest_sha256": "corpus_sha_456",
        "workspace_id": "ws_canonical_comparison_0004",
        "runner_version": "2.2.0",
        "scorer_version": "2.2.0",
        "embedding_model": "amazon.titan-embed-text-v2:0",
        "llm_model": "apac.amazon.nova-lite-v1:0",
        "reranker_policy": "openrouter:nvidia/llama-nemotron-rerank-vl-1b-v2:free (fallback: lexical_bm25_plus)",
        "cache_mode": "bypass_exact_and_semantic",
        "benchmark_type": "cold",
        "metrics": {"overall_accuracy": 0.5333, "grounded_accuracy": 0.5167},
    }

    cand_manifest_2_2 = dict(base_manifest_2_2)
    cand_manifest_2_2["commit_sha"] = "cand_commit_888"
    cand_manifest_2_2["baseline_commit_sha"] = "base_commit_777"
    cand_manifest_2_2["metrics"] = {"overall_accuracy": 0.5500, "grounded_accuracy": 0.5333}

    res = compare_manifests(cand_manifest_2_2, base_manifest_2_2)
    assert res["comparable"] is True
    assert res["status"] == "COMPARABLE"
    assert res["deltas"]["overall_accuracy_delta"] == pytest.approx(0.0167, abs=1e-4)
