"""Reproducible benchmark runner for the core engine walking skeleton.

Measures and evaluates:
1. Correctness (factual accuracy, aggregate calculation precision, page hits).
2. Evidence support (citation presence, valid provenance, refusal on empty evidence).
3. Coverage (blocking aggregate claims on incomplete coverage without hallucination).
4. Token usage (prompt tokens, completion tokens, total accounting).
5. Query latency (average, p95, min, max wall clock duration).
6. Ingestion time tracking.

Persists compact metadata in DynamoDB and detailed reports in S3 using
the recoverable write sequence defined in AC-10.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import math
import time
from typing import Any, Callable
import uuid

from src.schemas.contracts.boundary import BenchmarkRunMetadata
from services.benchmark.persistence_service import BenchmarkPersistenceService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BenchmarkTestCase:
    """Individual benchmark test query specification with ground truth expectations."""
    query_id: str
    query: str
    expected_answer: str | None = None
    expected_doc_title: str | None = None
    expected_pages: tuple[int, ...] = ()
    query_type: str = "text"  # "tabular_aggregate", "text", "partial_coverage", "insufficient_evidence"
    requires_complete_coverage: bool = False


@dataclass
class QueryBenchmarkResult:
    """Detailed evaluation result for a single benchmark query execution."""
    query_id: str
    query: str
    query_type: str
    status: str
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    answer: str
    citations: list[dict[str, Any]]
    correctness: bool
    evidence_supported: bool
    coverage_valid: bool
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "query": self.query,
            "query_type": self.query_type,
            "status": self.status,
            "latency_ms": round(self.latency_ms, 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "answer": self.answer,
            "citations": self.citations,
            "correctness": self.correctness,
            "evidence_supported": self.evidence_supported,
            "coverage_valid": self.coverage_valid,
            "notes": self.notes,
        }


@dataclass
class BenchmarkRunReport:
    """Combined report containing compact metadata and full detailed artifact."""
    metadata: BenchmarkRunMetadata
    detailed_artifact: dict[str, Any]


class BenchmarkRunner:
    """Executes reproducible benchmark suites and records performance and correctness metrics."""

    def __init__(
        self,
        query_executor: Callable[[str, str], Any] | None = None,
        persistence_service: BenchmarkPersistenceService | None = None,
    ) -> None:
        self.query_executor = query_executor
        self.persistence_service = persistence_service or BenchmarkPersistenceService()

    def _default_executor(self, query_text: str, workspace_id: str) -> Any:
        from src.api.boundary_adapters import ProductionExecutionEngine
        engine = ProductionExecutionEngine()
        return engine.execute_query(
            workspace_id=workspace_id,
            query_text=query_text,
            is_aggregate=any(k in query_text.lower() for k in ("earliest", "latest", "sum", "average", "total", "count")),
        )

    def run_suite(
        self,
        workspace_id: str,
        test_cases: list[BenchmarkTestCase],
        ingestion_time_ms: float = 0.0,
        benchmark_run_id: str | None = None,
    ) -> BenchmarkRunReport:
        """Execute all benchmark test cases and persist results across S3 and DynamoDB."""
        executor = self.query_executor or self._default_executor
        run_id = benchmark_run_id or f"bm_{uuid.uuid4().hex[:8]}_{int(time.time())}"
        now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        query_results: list[QueryBenchmarkResult] = []
        latencies: list[float] = []

        total_prompt_tokens = 0
        total_completion_tokens = 0

        correct_count = 0
        evidence_supported_count = 0
        coverage_valid_count = 0

        for case in test_cases:
            t0 = time.perf_counter()
            try:
                outcome = executor(case.query, workspace_id)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                logger.warning("Query execution failed during benchmark for %s: %s", case.query_id, exc)
                query_results.append(
                    QueryBenchmarkResult(
                        query_id=case.query_id,
                        query=case.query,
                        query_type=case.query_type,
                        status="ERROR",
                        latency_ms=elapsed_ms,
                        prompt_tokens=0,
                        completion_tokens=0,
                        answer=f"Execution error: {exc}",
                        citations=[],
                        correctness=False,
                        evidence_supported=False,
                        coverage_valid=False,
                        notes=str(exc),
                    )
                )
                latencies.append(elapsed_ms)
                continue

            latencies.append(elapsed_ms)

            # Extract fields from QueryExecutionOutcome or dict
            answer = getattr(outcome, "answer", "") if hasattr(outcome, "answer") else str(outcome.get("answer", ""))
            status_val = getattr(outcome, "status", "SUCCESS") if hasattr(outcome, "status") else str(outcome.get("status", "SUCCESS"))
            raw_citations = getattr(outcome, "citations", ()) if hasattr(outcome, "citations") else outcome.get("citations", [])

            serialized_citations = []
            for cit in raw_citations:
                if hasattr(cit, "model_dump"):
                    serialized_citations.append(cit.model_dump())
                elif isinstance(cit, dict):
                    serialized_citations.append(cit)
                else:
                    serialized_citations.append({"text": str(cit)})

            # Estimate or extract token usage
            prompt_tok = max(10, int(len(case.query) / 4))
            comp_tok = max(1, int(len(answer) / 4)) if answer else 0
            total_prompt_tokens += prompt_tok
            total_completion_tokens += comp_tok

            # 1. Correctness Evaluation
            is_correct = False
            notes = ""
            if case.query_type == "insufficient_evidence":
                # Expect clean refusal without fabricated evidence
                is_correct = (
                    "couldn't find" in answer.lower()
                    or "insufficient" in answer.lower()
                    or status_val == "INSUFFICIENT_COVERAGE"
                    or len(serialized_citations) == 0
                )
            elif case.query_type == "partial_coverage":
                # Expect explicit aggregate claim refusal due to incomplete coverage
                is_correct = (
                    status_val == "INSUFFICIENT_COVERAGE"
                    or "incomplete" in answer.lower()
                    or "tombstoned" in answer.lower()
                    or "aborted" in answer.lower()
                )
            elif case.expected_answer is not None:
                # Value or keyword match
                is_correct = case.expected_answer.lower() in answer.lower()
            elif case.expected_pages:
                # Check citation page numbers
                cited_pages = set()
                for c in serialized_citations:
                    loc = c.get("location", {})
                    if isinstance(loc, dict) and "page" in loc:
                        cited_pages.add(int(loc["page"]))
                    elif "page_number" in c:
                        cited_pages.add(int(c["page_number"]))
                is_correct = any(p in cited_pages for p in case.expected_pages)
            else:
                is_correct = len(serialized_citations) > 0 and len(answer.strip()) > 0

            # 2. Evidence Support Evaluation
            if case.query_type in ("insufficient_evidence", "partial_coverage"):
                # On insufficient evidence or partial coverage abort, having zero ungrounded citations is supported
                evidence_supported = True
            else:
                evidence_supported = len(serialized_citations) > 0 and answer not in ("", "NOT_FOUND")

            # 3. Coverage Verification Evaluation
            if case.requires_complete_coverage:
                coverage_valid = (status_val != "SUCCESS") or ("incomplete" in answer.lower())
            else:
                coverage_valid = True

            if is_correct:
                correct_count += 1
            if evidence_supported:
                evidence_supported_count += 1
            if coverage_valid:
                coverage_valid_count += 1

            query_results.append(
                QueryBenchmarkResult(
                    query_id=case.query_id,
                    query=case.query,
                    query_type=case.query_type,
                    status=status_val,
                    latency_ms=elapsed_ms,
                    prompt_tokens=prompt_tok,
                    completion_tokens=comp_tok,
                    answer=answer,
                    citations=serialized_citations,
                    correctness=is_correct,
                    evidence_supported=evidence_supported,
                    coverage_valid=coverage_valid,
                    notes=notes,
                )
            )

        scored_count = len(test_cases)
        correctness_score = (correct_count / scored_count) if scored_count else 0.0
        evidence_support_score = (evidence_supported_count / scored_count) if scored_count else 0.0
        coverage_score = (coverage_valid_count / scored_count) if scored_count else 0.0

        avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
        sorted_lat = sorted(latencies)
        p95_index = min(len(sorted_lat) - 1, int(math.ceil(0.95 * len(sorted_lat)) - 1)) if sorted_lat else 0
        p95_lat = sorted_lat[p95_index] if sorted_lat else 0.0

        # Construct compact metadata
        metadata = BenchmarkRunMetadata(
            benchmark_run_id=run_id,
            workspace_id=workspace_id,
            status="completed",
            created_at=now_utc,
            total_queries=len(test_cases),
            scored_queries=scored_count,
            correctness_score=round(correctness_score, 4),
            evidence_support_score=round(evidence_support_score, 4),
            coverage_score=round(coverage_score, 4),
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            total_tokens=total_prompt_tokens + total_completion_tokens,
            avg_latency_ms=round(avg_lat, 2),
            p95_latency_ms=round(p95_lat, 2),
            ingestion_time_ms=round(ingestion_time_ms, 2),
            s3_artifact_key="",  # To be bound by persistence coordinator
        )

        detailed_artifact = {
            "benchmark_run_id": run_id,
            "workspace_id": workspace_id,
            "created_at": now_utc,
            "summary": {
                "total_queries": len(test_cases),
                "scored_queries": scored_count,
                "correctness_score": round(correctness_score, 4),
                "evidence_support_score": round(evidence_support_score, 4),
                "coverage_score": round(coverage_score, 4),
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_completion_tokens,
                "total_tokens": total_prompt_tokens + total_completion_tokens,
                "avg_latency_ms": round(avg_lat, 2),
                "p95_latency_ms": round(p95_lat, 2),
                "ingestion_time_ms": round(ingestion_time_ms, 2),
            },
            "queries": [q.to_dict() for q in query_results],
        }

        # Persist S3 first, then DynamoDB per AC-10 recoverable ordering
        persisted_metadata = self.persistence_service.persist_benchmark_run(
            workspace_id=workspace_id,
            metadata=metadata,
            detailed_artifact=detailed_artifact,
        )

        return BenchmarkRunReport(
            metadata=persisted_metadata,
            detailed_artifact=detailed_artifact,
        )
