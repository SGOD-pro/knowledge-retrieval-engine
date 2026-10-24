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
import re
import time
from typing import Any, Callable
import uuid

from src.schemas.contracts.boundary import BenchmarkRunMetadata
from src.schemas.contracts.location import TableLocation
from services.benchmark.persistence_service import BenchmarkPersistenceService
from services.evaluation.benchmark_scorer import compute_faithfulness

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
    expected_source_id: str | None = None
    expected_source_version: int | None = None
    valid_source_versions: dict[str, int] | None = None
    dataset_is_incomplete: bool = False
    allow_token_estimation: bool = True


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
    token_usage_source: str = "unavailable"
    is_estimated_tokens: bool = False
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
            "token_usage_source": self.token_usage_source,
            "is_estimated_tokens": self.is_estimated_tokens,
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

    @staticmethod
    def _extract_numbers_normalized(text: str) -> set[str]:
        cleaned = re.sub(r"(?<=\d),(?=\d)", "", text)
        raw_nums = re.findall(r"\b\d+(?:\.\d+)?%?\b", cleaned)
        nums = set()
        for n in raw_nums:
            nums.add(n)
            clean = n.rstrip("%")
            nums.add(clean)
            try:
                f = float(clean)
                if f.is_integer():
                    nums.add(str(int(f)))
                else:
                    nums.add(str(f))
            except ValueError:
                pass
        return nums

    def _default_executor(self, query_text: str, workspace_id: str) -> Any:
        from src.api.boundary_adapters import ProductionExecutionEngine
        engine = ProductionExecutionEngine()
        return engine.execute_query(
            workspace_id=workspace_id,
            query_text=query_text,
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

            # Token accounting
            # Provider token usage must be used when available.
            # Deterministic execution must not invent model usage;
            # unavailable usage and estimates must be explicitly distinguished.
            # Prefer observed usage over query-type assumptions.
            prompt_tok = 0
            comp_tok = 0
            tok_source = "unavailable"
            is_estimated = False

            tok_dict = None
            if hasattr(outcome, "token_usage") and getattr(outcome, "token_usage") is not None:
                tok_dict = getattr(outcome, "token_usage")
            elif isinstance(outcome, dict) and outcome.get("token_usage") is not None:
                tok_dict = outcome.get("token_usage")

            obs_prompt = None
            obs_comp = None
            if isinstance(tok_dict, dict):
                obs_prompt = tok_dict.get("prompt_tokens") if "prompt_tokens" in tok_dict else tok_dict.get("input_tokens")
                obs_comp = tok_dict.get("completion_tokens") if "completion_tokens" in tok_dict else tok_dict.get("output_tokens")
            elif hasattr(outcome, "prompt_tokens") and getattr(outcome, "prompt_tokens") is not None:
                obs_prompt = getattr(outcome, "prompt_tokens")
                obs_comp = getattr(outcome, "completion_tokens", 0)

            # Check if execution telemetry establishes no model use.
            # Missing generation telemetry must remain unknown.
            # Do NOT infer zero from query-type, keywords, or aggregate flags without generation telemetry.
            telemetry_dict = (
                getattr(outcome, "telemetry", None)
                or (outcome.get("telemetry") if isinstance(outcome, dict) else None)
                or {}
            )
            generation_calls = (
                getattr(outcome, "generation_calls", None)
                if hasattr(outcome, "generation_calls")
                else None
            )
            if generation_calls is None and isinstance(outcome, dict) and "generation_calls" in outcome:
                generation_calls = outcome["generation_calls"]
            if generation_calls is None and isinstance(telemetry_dict, dict) and "generation_calls" in telemetry_dict:
                generation_calls = telemetry_dict["generation_calls"]

            is_exec_deterministic = (
                getattr(outcome, "execution_type", "") == "deterministic"
                or (isinstance(outcome, dict) and outcome.get("execution_type") == "deterministic")
                or getattr(outcome, "is_deterministic", False)
                or (isinstance(outcome, dict) and outcome.get("is_deterministic", False))
                or getattr(outcome, "structured_aggregate", False)
                or (isinstance(outcome, dict) and outcome.get("structured_aggregate", False))
            )

            telemetry_establishes_zero = (
                generation_calls is not None
                and generation_calls == 0
                and (
                    is_exec_deterministic
                    or (tok_dict is not None and obs_prompt == 0 and obs_comp == 0)
                )
            )

            # 1. Prefer observed token usage over query-type assumptions
            if obs_prompt is not None and obs_comp is not None and (int(obs_prompt) > 0 or int(obs_comp) > 0):
                prompt_tok = int(obs_prompt)
                comp_tok = int(obs_comp)
                tok_source = "provider_reported"
                is_estimated = False
            elif telemetry_establishes_zero:
                prompt_tok = 0
                comp_tok = 0
                tok_source = "deterministic_zero"
                is_estimated = False
            elif getattr(case, "allow_token_estimation", True):
                prompt_tok = max(1, int(len(case.query) / 4))
                comp_tok = max(1, int(len(answer) / 4)) if answer else 0
                tok_source = "estimated"
                is_estimated = True
            else:
                prompt_tok = 0
                comp_tok = 0
                tok_source = "unavailable"
                is_estimated = False

            total_prompt_tokens += prompt_tok
            total_completion_tokens += comp_tok

            # 1. Correctness Evaluation
            is_correct = False
            notes = ""
            is_infrastructure_error = (
                status_val.upper() in ("ERROR", "INTERNAL_ERROR", "STORAGE_FAILURE", "STORAGE_ERROR")
                or "execution error:" in answer.lower()
                or (isinstance(outcome, dict) and outcome.get("error_code") == "storage_failure")
            )

            refusal_indicators = (
                "couldn't find",
                "could not find",
                "insufficient",
                "not enough",
                "not found",
                "cannot answer",
                "can't answer",
                "no evidence",
                "do not have",
                "does not contain",
                "unsupported",
            )
            is_refusal_answer = (
                any(r in answer.lower() for r in refusal_indicators)
                or status_val in ("INSUFFICIENT_COVERAGE", "REFUSAL")
                or answer.strip() == ""
            )

            if is_infrastructure_error:
                # Infrastructure ERROR must fail partial-coverage correctness and all other checks
                is_correct = False
            elif case.query_type == "insufficient_evidence":
                # Expect clean refusal without fabricated assertions AND with zero citations.
                # A fabricated answer with empty citations must FAIL.
                is_correct = is_refusal_answer and (len(serialized_citations) == 0)
            elif case.query_type == "partial_coverage":
                # Expect explicit aggregate claim refusal due to incomplete coverage.
                # Credit only the agreed explicit coverage refusal.
                is_correct = (
                    status_val in ("INSUFFICIENT_COVERAGE", "REFUSAL")
                    or (
                        not is_infrastructure_error
                        and any(
                            r in answer.lower()
                            for r in (
                                "incomplete coverage",
                                "incomplete dataset",
                                "supporting source was tombstoned",
                                "aggregate calculation aborted",
                                "insufficient coverage",
                            )
                        )
                    )
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
            # A wrong-source/version or unsupported citation must not earn evidence-support credit.
            # Validate claim support using existing evidence verification; matching citation identifiers alone must not earn support credit.
            if is_infrastructure_error:
                evidence_supported = False
            elif case.query_type in ("insufficient_evidence", "partial_coverage"):
                if case.query_type == "insufficient_evidence":
                    evidence_supported = is_refusal_answer and (len(serialized_citations) == 0)
                else:
                    evidence_supported = (len(serialized_citations) == 0) if (status_val in ("INSUFFICIENT_COVERAGE", "REFUSAL") or "incomplete" in answer.lower()) else False
            else:
                if not serialized_citations or answer in ("", "NOT_FOUND") or is_refusal_answer:
                    evidence_supported = False
                else:
                    citations_valid = True
                    for c in serialized_citations:
                        c_source = c.get("source_id") or c.get("table_id") or c.get("document_filename")
                        c_version = c.get("source_version") or c.get("document_version")
                        c_evidence_id = c.get("evidence_id") or c.get("chunk_id") or c.get("selection_hash")

                        # Must have non-empty identifier and source
                        if not c_source or not c_evidence_id:
                            citations_valid = False
                            break

                        # Check expected source if specified
                        if case.expected_source_id and str(c_source) != str(case.expected_source_id):
                            citations_valid = False
                            break

                        # Check expected version if specified
                        if case.expected_source_version is not None:
                            try:
                                if int(c_version) != int(case.expected_source_version):
                                    citations_valid = False
                                    break
                            except (TypeError, ValueError):
                                citations_valid = False
                                break

                        # Check valid source versions dictionary if specified
                        if case.valid_source_versions is not None:
                            if c_source not in case.valid_source_versions:
                                citations_valid = False
                                break
                            expected_ver = case.valid_source_versions[c_source]
                            try:
                                if int(c_version) != int(expected_ver):
                                    citations_valid = False
                                    break
                            except (TypeError, ValueError):
                                citations_valid = False
                                break

                        # Check expected pages if specified
                        if case.expected_pages:
                            loc = c.get("location", {})
                            c_page = None
                            if isinstance(loc, dict) and "page" in loc:
                                c_page = int(loc["page"])
                            elif "page_number" in c:
                                c_page = int(c["page_number"])
                            if c_page is None or c_page not in case.expected_pages:
                                citations_valid = False
                                break

                    if citations_valid:
                        # Validate claim support using existing evidence verification;
                        # matching citation identifiers alone must not earn support credit.
                        context_snippets = [
                            str(c.get("snippet") or c.get("text") or c.get("text_snippet") or "").strip()
                            for c in serialized_citations
                        ]
                        joined_context = " ".join(s for s in context_snippets if s).strip()
                        has_table_citation = any(
                            (
                                isinstance(c.get("location"), TableLocation)
                                or (isinstance(c.get("location"), dict) and c.get("location").get("type") == "table")
                                or (isinstance(c.get("location"), dict) and "table_id" in c["location"])
                                or c.get("table_id") is not None
                                or c.get("source_format") in ("csv", "xlsx", "table")
                                or c.get("evidence_type") == "structured_aggregate"
                            )
                            for c in serialized_citations
                        )

                        is_tabular_aggregate = (
                            case.query_type == "tabular_aggregate"
                            or getattr(outcome, "is_deterministic", False)
                            or (isinstance(outcome, dict) and outcome.get("execution_type") == "deterministic")
                            or (isinstance(outcome, dict) and outcome.get("structured_aggregate") is True)
                            or getattr(outcome, "structured_aggregate", False)
                            or has_table_citation
                        )

                        # Extract operation-specific computed results and metadata from structured evidence contracts.
                        # Validate the claimed aggregate against its operation-specific computed result.
                        # Do not treat selection_count, total_rows, years, or unrelated snippet numbers
                        # as interchangeable with result_value.
                        # Preserve valid count and min/max results using their corresponding computed values.
                        operators = set()
                        computed_results = set()
                        metadata_counts = set()
                        context_nums = set()

                        def _inspect_dict(d: dict):
                            op = d.get("operator") or d.get("operation")
                            if op:
                                operators.add(str(op).lower())
                            rv = d.get("result_value")
                            if rv is not None:
                                computed_results.update(self._extract_numbers_normalized(str(rv)))
                            sv = d.get("secondary_value")
                            if sv is not None:
                                computed_results.update(self._extract_numbers_normalized(str(sv)))
                            sc = d.get("selection_count") or d.get("records_matched")
                            if sc is not None:
                                metadata_counts.update(self._extract_numbers_normalized(str(sc)))
                            tr = d.get("total_rows_in_table") or d.get("total_rows") or d.get("records_examined")
                            if tr is not None:
                                metadata_counts.update(self._extract_numbers_normalized(str(tr)))

                        # 1. Inspect outcome
                        if isinstance(outcome, dict):
                            _inspect_dict(outcome)
                            for k in ("structured_payload", "structured_execution_result", "execution_result"):
                                sp = outcome.get(k)
                                if isinstance(sp, dict):
                                    _inspect_dict(sp)
                                elif sp is not None and hasattr(sp, "__dict__"):
                                    _inspect_dict(vars(sp))
                        else:
                            op = getattr(outcome, "operator", None) or getattr(outcome, "operation", None)
                            if op:
                                operators.add(str(op).lower())
                            rv = getattr(outcome, "result_value", None)
                            if rv is not None:
                                computed_results.update(self._extract_numbers_normalized(str(rv)))
                            sv = getattr(outcome, "secondary_value", None)
                            if sv is not None:
                                computed_results.update(self._extract_numbers_normalized(str(sv)))
                            for k in ("structured_payload", "structured_execution_result", "execution_result"):
                                sp = getattr(outcome, k, None)
                                if isinstance(sp, dict):
                                    _inspect_dict(sp)
                                elif sp is not None and hasattr(sp, "__dict__"):
                                    _inspect_dict(vars(sp))

                        # 2. Inspect citations
                        for c in serialized_citations:
                            _inspect_dict(c)
                            sp = c.get("structured_payload") or c.get("structured_execution_result")
                            if isinstance(sp, dict):
                                _inspect_dict(sp)
                            elif sp is not None and hasattr(sp, "__dict__"):
                                _inspect_dict(vars(sp))

                            snip = str(c.get("snippet") or c.get("text") or c.get("text_snippet") or "").strip()
                            if snip:
                                s_nums = self._extract_numbers_normalized(snip)
                                context_nums.update(s_nums)

                                for m in re.finditer(r"\b(?:result_value)\s*[:=]\s*([0-9]+(?:\.[0-9]+)?%?)", snip, re.I):
                                    computed_results.update(self._extract_numbers_normalized(m.group(1)))
                                for m in re.finditer(r"\b(?:sum|avg|average|min|max|count)\s+result_value\s*[:=]\s*([0-9]+(?:\.[0-9]+)?%?)", snip, re.I):
                                    computed_results.update(self._extract_numbers_normalized(m.group(1)))
                                for m in re.finditer(r"\b(?:sum|avg|average|min|max|count|total\s+sum)\s*[:=]\s*([0-9]+(?:\.[0-9]+)?%?)", snip, re.I):
                                    computed_results.update(self._extract_numbers_normalized(m.group(1)))
                                for m in re.finditer(r"\bselection_count\s*[:=]\s*(\d+)", snip, re.I):
                                    metadata_counts.update(self._extract_numbers_normalized(m.group(1)))
                                for m in re.finditer(r"\btotal_rows(?:_in_table)?\s*[:=]\s*(\d+)", snip, re.I):
                                    metadata_counts.update(self._extract_numbers_normalized(m.group(1)))
                                for m in re.finditer(r"\b(?:aggregated|examined|matched)\s*(\d+)\s*rows?\b", snip, re.I):
                                    metadata_counts.update(self._extract_numbers_normalized(m.group(1)))
                                for op_name in ("sum", "count", "min", "max", "avg", "average", "range", "compare"):
                                    if re.search(rf"\b{op_name}\b", snip, re.I):
                                        operators.add(op_name)

                        is_count_op = (
                            "count" in operators
                            or (case.query and "how many" in case.query.lower())
                            or (case.query and bool(re.search(r"\bcount\b", case.query.lower())))
                        )

                        if is_count_op:
                            # For count operations, row count is the computed result value
                            if not computed_results and metadata_counts:
                                computed_results.update(metadata_counts)
                        else:
                            # For sum, avg, min, max, range, etc.
                            # selection_count and total_rows are metadata and NOT interchangeable with result_value
                            computed_results = computed_results - metadata_counts

                        if not computed_results:
                            computed_results = context_nums - metadata_counts

                        ans_nums = self._extract_numbers_normalized(answer)
                        ans_years = {n for n in ans_nums if len(n) == 4 and n.isdigit() and 1900 <= int(n) <= 2099}
                        ans_amounts = ans_nums - ans_years

                        if is_tabular_aggregate:
                            # Validate the answer against the operation-specific computed result
                            # using the structured evidence contract.
                            if not has_table_citation and not joined_context:
                                evidence_supported = False
                            elif not ans_nums:
                                if joined_context:
                                    faith_score = compute_faithfulness(answer, joined_context)
                                    evidence_supported = faith_score is not None and faith_score >= 0.25
                                else:
                                    evidence_supported = False
                            elif computed_results:
                                # The answer must assert the operation-specific computed result.
                                # Do not treat selection_count, total_rows, years, or unrelated snippet numbers
                                # as interchangeable with result_value.
                                if not (ans_nums & computed_results):
                                    evidence_supported = False
                                else:
                                    if ans_amounts:
                                        if not (ans_amounts & computed_results):
                                            evidence_supported = False
                                        elif not ans_amounts.issubset(computed_results | context_nums | metadata_counts):
                                            evidence_supported = False
                                        else:
                                            evidence_supported = True
                                    else:
                                        # Only year numbers claimed (e.g. min/max year range)
                                        if not (ans_years & (computed_results | context_nums)):
                                            evidence_supported = False
                                        else:
                                            evidence_supported = True
                            else:
                                evidence_supported = False
                        elif not joined_context:
                            evidence_supported = False
                        else:
                            # Non-aggregate text queries
                            if ans_nums:
                                if ans_amounts:
                                    if not ans_amounts.issubset(context_nums):
                                        evidence_supported = False
                                    elif ans_years and not (ans_years & context_nums):
                                        evidence_supported = False
                                    else:
                                        faith_score = compute_faithfulness(answer, joined_context)
                                        evidence_supported = faith_score is not None and faith_score >= 0.25
                                else:
                                    if not (ans_years & context_nums):
                                        evidence_supported = False
                                    else:
                                        faith_score = compute_faithfulness(answer, joined_context)
                                        evidence_supported = faith_score is not None and faith_score >= 0.25
                            else:
                                faith_score = compute_faithfulness(answer, joined_context)
                                evidence_supported = faith_score is not None and faith_score >= 0.25
                    else:
                        evidence_supported = False

            # 3. Coverage Verification Evaluation
            # - A valid complete-dataset aggregate MUST pass coverage scoring
            # - An incomplete-dataset complete claim MUST fail coverage scoring
            # - Infrastructure ERROR must fail coverage scoring
            if case.dataset_is_incomplete or case.query_type == "partial_coverage":
                if case.requires_complete_coverage or case.query_type == "partial_coverage":
                    if is_infrastructure_error:
                        coverage_valid = False
                    else:
                        coverage_valid = (
                            status_val in ("INSUFFICIENT_COVERAGE", "REFUSAL")
                            or any(
                                r in answer.lower()
                                for r in (
                                    "incomplete coverage",
                                    "incomplete dataset",
                                    "supporting source was tombstoned",
                                    "aggregate calculation aborted",
                                    "insufficient coverage",
                                )
                            )
                        )
                else:
                    coverage_valid = (status_val == "SUCCESS" and not is_infrastructure_error)
            else:
                # Complete dataset
                if case.requires_complete_coverage or case.query_type == "tabular_aggregate":
                    coverage_valid = (
                        status_val == "SUCCESS"
                        and not is_infrastructure_error
                        and "incomplete" not in answer.lower()
                    )
                else:
                    coverage_valid = not is_infrastructure_error


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
                    token_usage_source=tok_source,
                    is_estimated_tokens=is_estimated,
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
                "token_usage_breakdown": {
                    "provider_reported": sum(1 for q in query_results if q.token_usage_source == "provider_reported"),
                    "deterministic_zero": sum(1 for q in query_results if q.token_usage_source == "deterministic_zero"),
                    "estimated": sum(1 for q in query_results if q.token_usage_source == "estimated"),
                    "unavailable": sum(1 for q in query_results if q.token_usage_source == "unavailable"),
                },
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


def validate_benchmark_comparison_preconditions(
    baseline_run_id: str,
    candidate_run_id: str,
    comparison_workspace_id: str,
) -> bool:
    """Validate that benchmark accuracy comparisons execute only on controlled canonical workspaces.

    Per Spec 0005 AC-10:
    Formal benchmark accuracy claims remain explicitly blocked until post-repair canonical
    benchmark execution is run under controlled comparison conditions on ws_canonical_comparison_0004.
    Requiring ws_canonical_comparison_0004 is an additional guard, not proof of comparability
    or a replacement for manifest and configuration validation. Post-repair canonical evaluation
    remains pending, with zero measured accuracy improvement or live AWS readiness claimed.
    """
    CANONICAL_COMPARISON_WORKSPACE = "ws_canonical_comparison_0004"
    if comparison_workspace_id != CANONICAL_COMPARISON_WORKSPACE:
        raise ValueError(
            f"Benchmark comparison claims are blocked on workspace '{comparison_workspace_id}'. "
            f"Comparisons must execute on canonical controlled workspace '{CANONICAL_COMPARISON_WORKSPACE}'."
        )
    if not baseline_run_id or not candidate_run_id:
        raise ValueError("Both baseline_run_id and candidate_run_id are required for comparison.")
    return True
