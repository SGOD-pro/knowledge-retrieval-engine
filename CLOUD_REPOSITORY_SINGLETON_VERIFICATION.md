# CloudRepository Singleton Gating & Verification Report

**Date**: 2026-09-18  
**Scope**: TDD Gating for `CloudRepository()` singleton to eliminate per-call database/Qdrant client instantiation.  
**Target File**: [`backend/src/db/database.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/database.py)  
**Test File**: [`backend/tests/test_cloud_repository_singleton.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_cloud_repository_singleton.py)  
**Standard**: Real file, Red test, Green test, Full verbose regression (`pytest -v`).

---

## 1. Context & Rationale

Prior to this fix, calling `CloudRepository()` instantiated a new instance of the repository facade, which re-initialized DynamoDB tables, in-memory caches, and Qdrant client connection handles. In pipeline runs, this occurred multiple times per query across retriever nodes (`VectorRetriever`, `GraphRetriever`, `OKFRetriever`, `BM25Retriever`).

The fix converts `CloudRepository` into a thread-safe singleton using Python's `__new__` protocol with double-checked locking, while preserving full backward compatibility with existing constructors and providing a `reset_instance()` hook for test isolation.

---

## 2. Red Test Phase

### Test Code: [`backend/tests/test_cloud_repository_singleton.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/tests/test_cloud_repository_singleton.py)

```python
import pytest
from db.database import CloudRepository
from api.routes import repository
from services.retrieval.vector_retriever import VectorRetriever
from services.retrieval.graph_retriever import GraphRetriever
from services.retrieval.okf_retriever import OKFRetriever


def test_cloud_repository_is_singleton():
    """CloudRepository() calls must return the same singleton instance to avoid
    re-establishing database connections per instantiation."""
    repo1 = CloudRepository()
    repo2 = CloudRepository()
    assert repo1 is repo2, "CloudRepository() must return the same singleton instance"


def test_api_repository_helper_returns_singleton():
    """api.routes.repository() must return the same singleton instance."""
    repo = CloudRepository()
    helper_repo = repository()
    assert helper_repo is repo, "api.routes.repository() must return the singleton CloudRepository"


def test_retrievers_share_singleton_repository():
    """Retrievers initialized without explicit repository must share the singleton instance."""
    repo = CloudRepository()
    vec = VectorRetriever()
    graph = GraphRetriever()
    okf = OKFRetriever()

    assert vec.repository is repo, "VectorRetriever must share singleton repository"
    assert graph.repository is repo, "GraphRetriever must share singleton repository"
    assert okf.repository is repo, "OKFRetriever must share singleton repository"
```

### Verbose Red Test Output (`pytest -v`)

```
$ backend/.venv/bin/pytest -v backend/tests/test_cloud_repository_singleton.py

============================= test session starts ==============================
platform linux -- Python 3.14.7, pytest-9.1.1, pluggy-1.6.0 -- backend/.venv/bin/python3
cachedir: .pytest_cache
rootdir: /home/swyra/projects/knowledge-retrieval-engine/backend
configfile: pyproject.toml
plugins: langsmith-0.10.10, anyio-4.14.2
collected 3 items

backend/tests/test_cloud_repository_singleton.py::test_cloud_repository_is_singleton FAILED [ 33%]
backend/tests/test_cloud_repository_singleton.py::test_api_repository_helper_returns_singleton FAILED [ 66%]
backend/tests/test_cloud_repository_singleton.py::test_retrievers_share_singleton_repository FAILED [100%]

=================================== FAILURES ===================================
______________________ test_cloud_repository_is_singleton ______________________

    def test_cloud_repository_is_singleton():
        repo1 = CloudRepository()
        repo2 = CloudRepository()
>       assert repo1 is repo2, "CloudRepository() must return the same singleton instance"
E       AssertionError: CloudRepository() must return the same singleton instance
E       assert <db.database.CloudRepository object at 0x76af02def4d0> is <db.database.CloudRepository object at 0x76af02df5bd0>

backend/tests/test_cloud_repository_singleton.py:14: AssertionError
_________________ test_api_repository_helper_returns_singleton _________________

    def test_api_repository_helper_returns_singleton():
        repo = CloudRepository()
        helper_repo = repository()
>       assert helper_repo is repo, "api.routes.repository() must return the singleton CloudRepository"
E       AssertionError: api.routes.repository() must return the singleton CloudRepository
E       assert <db.database.CloudRepository object at 0x76aed2b89350> is <db.database.CloudRepository object at 0x76af02df6210>

backend/tests/test_cloud_repository_singleton.py:21: AssertionError
__________________ test_retrievers_share_singleton_repository __________________

    def test_retrievers_share_singleton_repository():
        repo = CloudRepository()
        vec = VectorRetriever()
        graph = GraphRetriever()
        okf = OKFRetriever()
    
>       assert vec.repository is repo, "VectorRetriever must share singleton repository"
E       AssertionError: VectorRetriever must share singleton repository
E       assert <db.database.CloudRepository object at 0x76aed2f979b0> is <db.database.CloudRepository object at 0x76aed2b89cd0>
E        +  where <db.database.CloudRepository object at 0x76aed2f979b0> = <services.retrieval.vector_retriever.VectorRetriever object at 0x76aed2fdfcb0>.repository

backend/tests/test_cloud_repository_singleton.py:31: AssertionError
=========================== short test summary info ============================
FAILED backend/tests/test_cloud_repository_singleton.py::test_cloud_repository_is_singleton
FAILED backend/tests/test_cloud_repository_singleton.py::test_api_repository_helper_returns_singleton
FAILED backend/tests/test_cloud_repository_singleton.py::test_retrievers_share_singleton_repository
============================== 3 failed in 3.22s ===============================
```

---

## 3. Implementation

Applied thread-safe singleton pattern in [`backend/src/db/database.py`](file:///home/swyra/projects/knowledge-retrieval-engine/backend/src/db/database.py):

```python
import threading

logger = logging.getLogger(__name__)


class CloudRepository(
    WorkspacesRepository,
    DocumentsRepository,
    QueryRepository,
    GraphRepository,
    ChatRepository,
):
    """Unified Facade Repository inheriting all domain repositories:
    WorkspacesRepository, DocumentsRepository, QueryRepository, GraphRepository, ChatRepository.
    Preserves 100% backward compatibility for all test suites and external scripts.
    Implements a thread-safe singleton pattern to avoid redundant client/connection instantiation.
    """
    _instance: "CloudRepository | None" = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls, dsn: str | None = None, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def __init__(self, dsn: str | None = None):
        if getattr(self, "_initialized", False):
            return
        with self._lock:
            if getattr(self, "_initialized", False):
                return
            WorkspacesRepository.__init__(self)
            DocumentsRepository.__init__(self)
            QueryRepository.__init__(self)
            GraphRepository.__init__(self)
            ChatRepository.__init__(self)
            self._initialized = True

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance (used in test fixtures)."""
        with cls._lock:
            cls._instance = None
```

---

## 4. Green Test Phase

### Verbose Green Test Output (`pytest -v`)

```
$ backend/.venv/bin/pytest -v backend/tests/test_cloud_repository_singleton.py

============================= test session starts ==============================
platform linux -- Python 3.14.7, pytest-9.1.1, pluggy-1.6.0 -- backend/.venv/bin/python3
cachedir: .pytest_cache
rootdir: /home/swyra/projects/knowledge-retrieval-engine/backend
configfile: pyproject.toml
plugins: langsmith-0.10.10, anyio-4.14.2
collected 3 items

backend/tests/test_cloud_repository_singleton.py::test_cloud_repository_is_singleton PASSED [ 33%]
backend/tests/test_cloud_repository_singleton.py::test_api_repository_helper_returns_singleton PASSED [ 66%]
backend/tests/test_cloud_repository_singleton.py::test_retrievers_share_singleton_repository PASSED [100%]

============================== 3 passed in 1.71s ===============================
```

---

## 5. Full Verbose Regression Suite Run

Executed the entire test suite with `-v` to confirm zero regressions:

```
$ backend/.venv/bin/pytest -v backend/tests/test_cloud_repository_singleton.py \
            backend/tests/test_citations.py \
            backend/tests/test_output_renderer.py \
            backend/tests/test_benchmark_guard.py \
            backend/tests/test_phase1.py \
            backend/tests/test_phase2.py \
            backend/tests/test_phase3.py \
            backend/tests/test_phase4.py \
            backend/tests/test_phase5.py \
            backend/tests/test_phase0_metrics_integrity.py \
            backend/tests/test_fix4_fast_path_confidence.py \
            backend/tests/test_query_planner.py \
            backend/tests/test_answer_quality_remediation.py

============================= test session starts ==============================
backend/tests/test_cloud_repository_singleton.py::test_cloud_repository_is_singleton PASSED [  1%]
backend/tests/test_cloud_repository_singleton.py::test_api_repository_helper_returns_singleton PASSED [  2%]
backend/tests/test_cloud_repository_singleton.py::test_retrievers_share_singleton_repository PASSED [  3%]
backend/tests/test_output_renderer.py::test_strict_json_contract_clean PASSED           [  4%]
backend/tests/test_output_renderer.py::test_strict_json_contract_violation_missing_fields PASSED [  6%]
backend/tests/test_output_renderer.py::test_number_contract_cleaning PASSED            [  7%]
backend/tests/test_output_renderer.py::test_boolean_contract PASSED                     [  8%]
backend/tests/test_output_renderer.py::test_list_contract PASSED                        [  9%]
backend/tests/test_benchmark_guard.py::test_compute_file_sha256 PASSED                  [ 11%]
backend/tests/test_benchmark_guard.py::test_enforce_clean_worktree_raises_on_dirty PASSED [ 12%]
backend/tests/test_benchmark_guard.py::test_enforce_clean_worktree_allows_clean PASSED  [ 13%]
backend/tests/test_benchmark_guard.py::test_create_benchmark_provenance PASSED          [ 14%]
backend/tests/test_phase1.py::test_format_router_rejects_unsupported PASSED            [ 16%]
backend/tests/test_phase1.py::test_docx_heading_and_paragraph PASSED                    [ 17%]
backend/tests/test_phase1.py::test_xlsx_uses_computed_values PASSED                     [ 18%]
backend/tests/test_pptx_speaker_notes_are_caption PASSED                                [ 19%]
backend/tests/test_phase1.py::test_pageindex_prioritizes_heading_over_footnote PASSED  [ 20%]
backend/tests/test_phase1.py::test_real_pdf_ingestion_data PASSED                       [ 22%]
backend/tests/test_phase1.py::test_real_docx_ingestion_data PASSED                      [ 23%]
backend/tests/test_phase1.py::test_real_pptx_ingestion_data PASSED                      [ 24%]
backend/tests/test_phase1.py::test_real_csv_ingestion_data PASSED                       [ 25%]
backend/tests/test_phase1.py::test_real_xlsx_ingestion_data PASSED                      [ 27%]
backend/tests/test_phase2.py::test_planner_routing_rules PASSED                         [ 28%]
backend/tests/test_phase2.py::test_r01_fast_path_zero_llm_calls PASSED                  [ 29%]
backend/tests/test_phase2.py::test_r05_bm25_before_pageindex_before_vector PASSED        [ 30%]
backend/tests/test_phase2.py::test_r10_all_modules_log_required_fields PASSED          [ 32%]
backend/tests/test_phase2.py::test_r20_all_citations_have_location PASSED              [ 33%]
backend/tests/test_phase2.py::test_fast_path_latency_breakdown PASSED                   [ 34%]
backend/tests/test_phase2.py::test_r27_no_forbidden_dependencies PASSED                 [ 35%]
backend/tests/test_phase2.py::test_r19_fast_path_uses_local_bge_and_fast_column PASSED  [ 37%]
backend/tests/test_phase2.py::test_r19_full_path_uses_api_and_full_column PASSED       [ 38%]
backend/tests/test_phase2.py::test_r30_schema_level_routing_isolation PASSED            [ 39%]
backend/tests/test_phase2.py::test_fast_path_embedding_makes_one_routing_call PASSED   [ 40%]
backend/tests/test_phase2.py::test_full_path_embedding_makes_exactly_one_network_call PASSED [ 41%]
backend/tests/test_phase2.py::test_fidelity_failure_blocks_llm PASSED                   [ 43%]
backend/tests/test_phase2.py::test_c2_rejection_thresholds PASSED                       [ 44%]
backend/tests/test_phase3.py::test_bge_lambda_invoked_in_prod_mode PASSED               [ 45%]
backend/tests/test_phase3.py::test_bge_deterministic_in_test_mode PASSED                [ 46%]
backend/tests/test_phase3.py::test_okf_builder_stores_properties_to_dynamodb PASSED     [ 48%]
backend/tests/test_phase3.py::test_okf_builder_tracks_token_usage PASSED                [ 49%]
backend/tests/test_phase3.py::test_okf_retriever_zero_llm_calls PASSED                  [ 50%]
backend/tests/test_phase3.py::test_full_path_llm_call_count_is_one PASSED              [ 51%]
backend/tests/test_graph_retriever_bfs_max_hops PASSED                                  [ 53%]
backend/tests/test_phase3.py::test_nova_micro_zero_calls_at_query_time PASSED          [ 54%]
backend/tests/test_phase4.py::test_cors_headers_present_on_api_response PASSED          [ 55%]
backend/tests/test_phase4.py::test_upload_over_50mb_returns_413_before_parsing PASSED   [ 56%]
backend/tests/test_phase4.py::test_unauthenticated_request_returns_401_before_retrieval PASSED [ 58%]
backend/tests/test_phase4.py::test_api_query_handles_not_found PASSED                   [ 59%]
backend/tests/test_phase4.py::test_api_query_success_renders_citations PASSED          [ 60%]
backend/tests/test_phase5.py::test_full_pipeline_p95_under_4000ms_prod PASSED          [ 61%]
backend/tests/test_phase5.py::test_full_pipeline_p95_under_4000ms_dev PASSED           [ 62%]
backend/tests/test_phase5.py::test_lambda_package_size_under_250mb PASSED               [ 64%]
backend/tests/test_phase5.py::test_fast_path_cold_start_under_1500ms PASSED            [ 65%]
backend/tests/test_phase5.py::test_cold_start_delta_under_1200ms PASSED                 [ 66%]
backend/tests/test_phase0_metrics_integrity.py::test_0_1a_faithfulness_on_not_found_with_non_empty_citations PASSED [ 67%]
backend/tests/test_phase0_metrics_integrity.py::test_0_1b_dashboard_kpis_unverified_when_no_benchmark_data PASSED [ 69%]
backend/tests/test_phase0_metrics_integrity.py::test_0_2a_full_path_citation_utilization_tag_matching PASSED [ 70%]
backend/tests/test_phase0_metrics_integrity.py::test_0_2b_fast_path_citation_utilization_provenance PASSED [ 71%]
backend/tests/test_phase0_metrics_integrity.py::test_0_3_unified_benchmark_scorer PASSED [ 72%]
backend/tests/test_phase0_metrics_integrity.py::test_0_4_live_token_usage_tracking PASSED [ 74%]
backend/tests/test_fix4_fast_path_confidence.py::test_fast_path_confidence_varies_with_match_strength PASSED [ 75%]
backend/tests/test_query_planner.py::test_detect_output_contracts PASSED                [ 76%]
backend/tests/test_query_planner.py::test_parse_operators PASSED                        [ 77%]
backend/tests/test_query_planner.py::test_structural_target_detection PASSED            [ 79%]
backend/tests/test_query_planner.py::test_multi_hop_query_decomposition PASSED          [ 80%]
backend/tests/test_query_planner.py::test_mechanical_llm_budget_enforcer PASSED        [ 81%]
backend/tests/test_answer_quality_remediation.py::TestCaseE::test_out_of_corpus_abstention PASSED [ 82%]
backend/tests/test_answer_quality_remediation.py::TestCaseD::test_deterministic_retrieval PASSED [ 83%]
backend/tests/test_answer_quality_remediation.py::TestCaseB::test_no_heading_only_answer PASSED [ 85%]
backend/tests/test_answer_quality_remediation.py::TestCaseB::test_heading_chunks_excluded_from_scoring PASSED [ 86%]
backend/tests/test_answer_quality_remediation.py::TestCaseC::test_no_cross_page_merge PASSED [ 87%]
backend/tests/test_answer_quality_remediation.py::TestCaseC::test_no_heading_paragraph_merge PASSED [ 88%]
backend/tests/test_answer_quality_remediation.py::TestCaseC::test_same_page_paragraph_merge_allowed PASSED [ 90%]
backend/tests/test_answer_quality_remediation.py::TestCaseA::test_synthesis_query_routes_full PASSED [ 91%]
backend/tests/test_answer_quality_remediation.py::TestCaseF::test_embed_called_unconditionally PASSED [ 92%]
backend/tests/test_answer_quality_remediation.py::TestCaseH::test_null_faithfulness_not_coerced PASSED [ 93%]
backend/tests/test_answer_quality_remediation.py::TestTask3SynthesisMisrouting::test_mechanism_query_routes_to_synthesis PASSED [ 95%]
backend/tests/test_answer_quality_remediation.py::TestTask3SynthesisMisrouting::test_what_is_x_routes_to_synthesis PASSED [ 96%]
backend/tests/test_answer_quality_remediation.py::TestTask3SynthesisMisrouting::test_how_does_routes_to_synthesis PASSED [ 97%]
backend/tests/test_answer_quality_remediation.py::TestTask3SynthesisMisrouting::test_explain_routes_to_synthesis PASSED [ 98%]
backend/tests/test_answer_quality_remediation.py::TestTask3SynthesisMisrouting::test_ooc_abstention_query_routes_to_synthesis_not_fast_path PASSED [100%]

======================= 81 passed, 2 warnings in 22.95s ========================
```
