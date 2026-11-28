#!/usr/bin/env python3
"""Bounded 3-arm LAYA smoke runner (supports offline stub mode and live endpoint smoke).

Enforces strict outbound call ceilings and disabled retries at both application
and SDK/transport layers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class OutboundCapExceededError(RuntimeError):
    """Raised when an outbound provider call cap is exceeded."""


class OutboundCallTracker:
    """Tracks and enforces strict outbound provider call ceilings."""

    def __init__(self, max_laya: int, max_synthesis: int, max_embedding: int) -> None:
        self.max_laya = max_laya
        self.max_synthesis = max_synthesis
        self.max_embedding = max_embedding
        self.laya_calls = 0
        self.synthesis_calls = 0
        self.embedding_calls = 0

    def record_laya(self) -> None:
        self.laya_calls += 1
        if self.laya_calls > self.max_laya:
            raise OutboundCapExceededError(f"LAYA cap exceeded: {self.laya_calls} > {self.max_laya}")

    def record_synthesis(self) -> None:
        self.synthesis_calls += 1
        if self.synthesis_calls > self.max_synthesis:
            raise OutboundCapExceededError(f"Synthesis cap exceeded: {self.synthesis_calls} > {self.max_synthesis}")

    def record_embedding(self) -> None:
        self.embedding_calls += 1
        if self.embedding_calls > self.max_embedding:
            raise OutboundCapExceededError(f"Embedding cap exceeded: {self.embedding_calls} > {self.max_embedding}")


def build_smoke_cases(doc_id: str, version: str) -> list[dict[str, Any]]:
    return [
        {
            "case_id": "LEX-1",
            "query": "How many medical officer posts are vacant in NATURE CURE & YOGA?",
            "expected_route": "lexical_expansion",
            "expected_answer_contains": "11",
            "expected_citation_id": f"{doc_id}:csv:row5",
            "source_version": version,
        },
        {
            "case_id": "NOSUP-1",
            "query": "What is the sanctioned medical officer count for SIDDHA?",
            "expected_route": "none",
            "expected_answer": "NOT_FOUND",
            "expected_citations": [],
            "source_version": version,
        },
    ]


def build_bedrock_no_retry_config():
    from botocore.config import Config
    return Config(retries={"max_attempts": 0})


def _old_format_endpoint_payload(self: Any, req: Any) -> dict[str, Any]:
    """Legacy static LAYA payload: all 6 routes + complexity score + no Requirement line."""
    all_criteria = {
        "lexical_expansion": "Perform keyword BM25 search for specific missing terms",
        "dense_expansion": "Perform vector similarity search for semantic concepts",
        "structural_expansion": "Navigate document structure or specific sections",
        "graph_expansion": "Traverse knowledge graph entity relationships",
        "structured_expansion": "Query structured table columns or database operands",
        "none": "No expansion needed or evidence is fundamentally missing",
    }
    snippets = " ".join(s.snippet for s in (getattr(req, "retrieved_summary", None) or [])[:3]).strip()
    doc_text = (
        f"Query: {req.query}\n"
        f"Gap detected: {req.missing_requirement.detected_gap}\n"
        f"Missing dimension: {req.missing_requirement.missing_dimension}"
    )
    if snippets:
        doc_text = f"{doc_text}\nContext: {snippets[:350]}"
    return {
        "state": {"document": doc_text},
        "questions": {
            "route": {
                "type": "choice",
                "instructions": "Which targeted retrieval expansion route should be executed?",
                "criteria": all_criteria,
            },
            "complexity": {
                "type": "score",
                "instructions": "How complex is this retrieval gap?",
                "criteria": ["simple", "moderate", "complex"],
            },
        },
    }


def validate_live_mode_config(settings_obj: Any) -> dict[str, str]:
    """Validate that live mode will not silently select a test double."""
    env_os = (os.environ.get("ENVIRONMENT") or "").strip().lower()
    env_cfg = (getattr(settings_obj, "ENVIRONMENT", "") or "").strip().lower()
    effective_env = env_os or env_cfg
    if effective_env == "test" or env_cfg == "test" or env_os == "test":
        raise RuntimeError(
            "Live smoke mode cannot run under ENVIRONMENT=test (llm_provider would select the test double). "
            "Use ENVIRONMENT=dev or ENVIRONMENT=prod, or pass --offline-stub."
        )
    llm_prov = (
        os.environ.get("LLM_PROVIDER")
        or getattr(settings_obj, "LLM_PROVIDER", None)
        or "bedrock"
    ).strip().lower()
    if llm_prov in ("test", "mock", "stub", "fake"):
        raise RuntimeError(
            f"Live smoke mode rejected resolved synthesis provider '{llm_prov}'."
        )
    endpoint_url = os.environ.get("LAYA_ENDPOINT_URL") or getattr(settings_obj, "LAYA_ENDPOINT_URL", "")
    api_key = os.environ.get("LAYA_API_KEY") or getattr(settings_obj, "LAYA_API_KEY", "")
    if not endpoint_url or not api_key:
        raise RuntimeError("Live smoke mode requires LAYA_ENDPOINT_URL and LAYA_API_KEY.")
    return {
        "effective_environment": effective_env or "unknown",
        "synthesis_provider": llm_prov,
    }


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    import time
    import requests
    from config import settings
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from schemas.contracts.boundary import DocumentSnapshot
    from schemas.contracts.verification import VerificationRecord
    from schemas.models import Chunk
    from services.langgraph_pipeline import Pipeline
    from services.retrieval.bm25_retriever import invalidate_chunk_cache
    from services.retrieval.fidelity_check import CoverageError
    from services.retrieval.laya_service import LayaDecisionService

    live_meta: dict[str, str] = {
        "effective_environment": (os.environ.get("ENVIRONMENT") or getattr(settings, "ENVIRONMENT", "") or "test").strip().lower(),
        "synthesis_provider": "stub" if args.offline_stub else "bedrock",
        "synthesis_model_id": "stub" if args.offline_stub else "",
    }
    if not args.offline_stub:
        live_meta.update(validate_live_mode_config(settings))
        from providers.bedrock_models import get_llm_model
        live_meta["synthesis_model_id"] = get_llm_model()

    doc_id = "ee9fa513-fee8-54be-ba11-fc661c9ef680"
    ws_id = args.workspace_id
    ver = args.source_version
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    cases = build_smoke_cases(doc_id, ver)

    tracker = OutboundCallTracker(
        max_laya=args.max_laya_calls,
        max_synthesis=args.max_synthesis_calls,
        max_embedding=args.max_embedding_calls,
    )
    bedrock_cfg = build_bedrock_no_retry_config()
    assert bedrock_cfg.retries == {"max_attempts": 0}

    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("MO Post", "usr_smoke", workspace_id=ws_id)
    b_repo.create_source(ws_id, "MO_Post_2024_25.csv", source_id=doc_id)
    rows = [
        (f"{doc_id}:csv:row2", "Sl. No.: 1. SYSTEM: AYURVEDA. SANCTION: 685. FILLED: 521. VACANT: 164."),
        (f"{doc_id}:csv:row3", "Sl. No.: 2. SYSTEM: UNANI. SANCTION: 105. FILLED: 59. VACANT: 46."),
        (f"{doc_id}:csv:row4", "Sl. No.: 3. SYSTEM: HOMEOPATHY. SANCTION: 81. FILLED: 47. VACANT: 34."),
        (f"{doc_id}:csv:row5", "Sl. No.: 4. SYSTEM: NATURE CURE & YOGA. SANCTION: 30. FILLED: 19. VACANT: 11."),
    ]
    all_chunks = [
        Chunk(
            id=cid, document_id=doc_id, source_format="csv", text=txt,
            element_type="table_row", workspace_id=ws_id, version=ver, document_version=ver,
            metadata={"document_version": ver, "source": "MO_Post_2024_25.csv"},
            embedding_fast=[0.1] * 384, embedding_full=[0.1] * 1024,
        )
        for cid, txt in rows
    ]
    # Omit row5 from baseline retrieval to inject a lexical gap for LEX-1
    baseline_chunks = all_chunks[:3]

    class _SmokeRepo:
        def __init__(self, initial: list[Chunk], full: list[Chunk]) -> None:
            self._initial = initial
            self._full = full
            self._expanded = False

        def get_all_chunks(self, document_ids: Any = None, workspace_id: str = "") -> list[Chunk]:
            src = self._full if self._expanded else self._initial
            return [c for c in src if not workspace_id or c.workspace_id == workspace_id]

        def search_vector(self, *a: Any, **kw: Any) -> list[tuple[Chunk, float]]:
            return [(c, 0.9) for c in self._initial]

        def search_vector_fast(self, *a: Any, **kw: Any) -> list[tuple[Chunk, float]]:
            return [(c, 0.9) for c in self._initial]

        def get(self, doc_id_arg: str) -> None:
            return None

    b_repo.save_document_snapshot(DocumentSnapshot(
        workspace_id=ws_id, document_id=doc_id, source="MO_Post_2024_25.csv",
        version=ver, format="csv", persisted_rows=4, total_rows=4,
        created_at="2026-11-27T18:16:50.802261Z",
    ))

    pipeline = Pipeline()
    results: list[dict[str, Any]] = []

    class _StubLayaClient:
        def __init__(self, max_retries: int, mode: str) -> None:
            assert max_retries == 0
            self.max_retries = max_retries
            self.mode = mode
            self.payloads: list[dict[str, Any]] = []

        def count_tokens(self, *args: Any, **kwargs: Any) -> dict[str, int]:
            return {"inputTokens": 50}

        def converse(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            messages = kwargs.get("messages") or []
            q_str = str(messages)
            dec = self.decide(q_str)
            return {
                "output": {"message": {"content": [{"text": json.dumps(dec)}]}},
                "usage": {"inputTokens": 50, "outputTokens": 20},
            }

        def decide(self, request_or_payload: Any, **kwargs: Any) -> dict[str, Any]:
            tracker.record_laya()
            q = getattr(request_or_payload, "query", "") or str(request_or_payload)
            self.payloads.append({"mode": self.mode, "query": q})
            if "NATURE CURE" in q.upper():
                return {
                    "decision_id": "dec_lex1",
                    "route": "lexical_expansion",
                    "parameters": {
                        "route_type": "lexical_expansion",
                        "query_terms": ["NATURE", "CURE", "YOGA", "VACANT"],
                        "document_ids": [doc_id],
                    },
                    "confidence": 0.95,
                    "rationale": "Recover missing NATURE CURE & YOGA row",
                }
            return {
                "decision_id": "dec_nosup1",
                "route": "none",
                "parameters": {"route_type": "none"},
                "confidence": 0.95,
                "rationale": "SIDDHA not present in workspace",
            }

    class _StubVerifier:
        def is_ready(self) -> bool:
            return True

        def verify(self, text: str = "", context: str = "", citations: Any = None, **kwargs: Any) -> VerificationRecord:
            return VerificationRecord(
                verification_id="vr_smoke_01",
                workspace_id=ws_id,
                query_id="q_smoke_01",
                snapshot_id="snap_1",
                requirement_id="req_1",
                evidence_id=f"{doc_id}:csv:row5",
                verifier_version="v1.0",
                verifier_type="generative",
                attempt_number=1,
                execution_time_ms=5,
                support_status="ASSERTED",
                confidence_score=0.95,
                verification_rationale="Grounded in recovered row5",
                verified_at="2026-11-28T00:00:00Z",
            )

    def _stub_call_llm(*a: Any, **kw: Any) -> dict[str, Any]:
        tracker.record_synthesis()
        prompt_str = str(a) + str(kw)
        if "NATURE CURE & YOGA" in prompt_str and "VACANT: 11" in prompt_str:
            return {
                "answer": "There are 11 vacant medical officer posts in NATURE CURE & YOGA.",
                "citations": [f"{doc_id}:csv:row5"],
                "usage": {"input_tokens": 80, "output_tokens": 18},
            }
        return {
            "answer": "NOT_FOUND",
            "citations": [],
            "usage": {"input_tokens": 80, "output_tokens": 5},
        }

    def _stub_embed(*a: Any, **kw: Any) -> list[float]:
        tracker.record_embedding()
        return [0.1] * 1024

    def _stub_embed_prov(*a: Any, **kw: Any) -> tuple[list[float], str]:
        tracker.record_embedding()
        return ([0.1] * 1024, "amazon.titan-embed-text-v2:0")

    def _mock_rerank(q: str, chs: list[Any], top_k: int = 6, return_mode: bool = False):
        top = list(chs)[:top_k]
        return (top, "mocked") if return_mode else top

    if not args.offline_stub:
        import aws.infra as aws_infra
        import providers.embedding_provider as emb_mod
        import providers.llm_provider as llm_mod
        import services.langgraph_pipeline as pipe_mod

        orig_call_llm = pipe_mod.call_llm
        orig_get_client = aws_infra.get_client
        orig_post = requests.post
        orig_format_payload = LayaDecisionService._format_endpoint_payload

        def _tracked_get_client(service_name: str, *g_args: Any, **g_kwargs: Any) -> Any:
            client_obj = orig_get_client(service_name, *g_args, **g_kwargs)
            if service_name == "bedrock-runtime" and not getattr(client_obj, "_smoke_wrapped", False):
                if hasattr(client_obj, "_client_config"):
                    client_obj._client_config.retries = {"max_attempts": 0}
                orig_converse = client_obj.converse
                orig_invoke = client_obj.invoke_model

                def _wrapped_converse(*c_args: Any, **c_kwargs: Any) -> Any:
                    tracker.record_synthesis()
                    resp_b = orig_converse(*c_args, **c_kwargs)
                    req_id = (resp_b.get("ResponseMetadata") or {}).get("RequestId")
                    if req_id is not None:
                        current_case_trace["synthesis_request_ids"].append(req_id)
                    return resp_b

                def _wrapped_invoke(*i_args: Any, **i_kwargs: Any) -> Any:
                    tracker.record_embedding()
                    return orig_invoke(*i_args, **i_kwargs)

                client_obj.converse = _wrapped_converse  # type: ignore[method-assign]
                client_obj.invoke_model = _wrapped_invoke  # type: ignore[method-assign]
                client_obj._smoke_wrapped = True
            return client_obj

        def _tracked_call_llm(*a: Any, **kw: Any) -> Any:
            res = orig_call_llm(*a, **kw)
            if isinstance(res, dict):
                current_case_trace["raw_answer"] = res.get("answer")
                current_case_trace["raw_citations"] = list(res.get("citations") or [])
            return res

    for arm in arms:
        for case in cases:
            invalidate_chunk_cache(ws_id)
            repo = _SmokeRepo(baseline_chunks, all_chunks)
            laya_enabled = arm != "deterministic"
            fid_calls = 0
            current_case_trace: dict[str, Any] = {
                "raw_answer": None,
                "raw_citations": [],
                "synthesis_request_ids": [],
            }

            def _injected_fidelity(q: str, ctx: list[str], **kw: Any) -> float:
                nonlocal fid_calls
                fid_calls += 1
                if fid_calls == 1:
                    repo._expanded = True
                    invalidate_chunk_cache(ws_id)
                    raise CoverageError(f"Injected baseline gap for {case['case_id']}")
                return 1.0

            t_case0 = time.perf_counter()
            if args.offline_stub:
                client = _StubLayaClient(max_retries=args.max_retries, mode=arm) if laya_enabled else None

                def _stub_call_llm_traced(*a: Any, **kw: Any) -> dict[str, Any]:
                    res = _stub_call_llm(*a, **kw)
                    current_case_trace["raw_answer"] = res.get("answer")
                    current_case_trace["raw_citations"] = list(res.get("citations") or [])
                    return res

                with (
                    patch("services.langgraph_pipeline._get_repo", return_value=repo),
                    patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank),
                    patch("services.langgraph_pipeline.call_llm", side_effect=_stub_call_llm_traced),
                    patch("services.langgraph_pipeline.check_fidelity", side_effect=_injected_fidelity),
                    patch("providers.embedding_provider.embed_text", side_effect=_stub_embed),
                    patch("providers.embedding_provider.embed_text_with_provenance", side_effect=_stub_embed_prov),
                ):
                    resp = pipeline.run(
                        query=case["query"],
                        workspace_id=ws_id,
                        boundary_repo=b_repo,
                        laya_enabled=laya_enabled,
                        deterministic_lexical_expansion_enabled=(arm == "deterministic"),
                        laya_client=client,
                        verifier=_StubVerifier(),
                        require_delivery_gate=True,
                    )
                laya_trace = None
            else:
                captured_laya: list[dict[str, Any]] = []

                def _tracked_post(url: str, *p_args: Any, **p_kwargs: Any) -> Any:
                    tracker.record_laya()
                    t_p0 = time.perf_counter()
                    payload = p_kwargs.get("json")
                    resp_http = orig_post(url, *p_args, **p_kwargs)
                    lat_ms = round((time.perf_counter() - t_p0) * 1000.0, 2)
                    laya_req_id = (
                        resp_http.headers.get("x-request-id")
                        or resp_http.headers.get("x-amzn-requestid")
                    )
                    rec: dict[str, Any] = {
                        "wire_bytes": len(json.dumps(payload).encode("utf-8")) if payload is not None else 0,
                        "status_code": resp_http.status_code,
                        "latency_ms": lat_ms,
                        "provider_request_id": laya_req_id,
                        "request_payload": payload,
                    }
                    try:
                        rec["response_json"] = resp_http.json()
                    except Exception:
                        rec["response_text"] = resp_http.text[:200]
                    captured_laya.append(rec)
                    return resp_http

                payload_fn = _old_format_endpoint_payload if arm == "laya_old" else orig_format_payload
                with (
                    patch("services.langgraph_pipeline._get_repo", return_value=repo),
                    patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank),
                    patch("services.langgraph_pipeline.call_llm", side_effect=_tracked_call_llm),
                    patch("services.langgraph_pipeline.check_fidelity", side_effect=_injected_fidelity),
                    patch("aws.infra.get_client", side_effect=_tracked_get_client),
                    patch("requests.post", side_effect=_tracked_post),
                    patch.object(LayaDecisionService, "_format_endpoint_payload", payload_fn),
                ):
                    resp = pipeline.run(
                        query=case["query"],
                        workspace_id=ws_id,
                        boundary_repo=b_repo,
                        laya_enabled=laya_enabled,
                        deterministic_lexical_expansion_enabled=(arm == "deterministic"),
                        verifier=_StubVerifier(),
                        require_delivery_gate=True,
                    )
                laya_trace = captured_laya

            case_latency_ms = round((time.perf_counter() - t_case0) * 1000.0, 2)
            results.append({
                "arm": arm,
                "case_id": case["case_id"],
                "initial_chunk_ids": [c.id for c in baseline_chunks],
                "row5_in_initial": any(c.id == f"{doc_id}:csv:row5" for c in baseline_chunks),
                "planned_path": getattr(resp, "planned_path", None),
                "executed_path": resp.executed_path,
                "laya_calls": resp.laya_calls,
                "expansion_steps": getattr(resp, "expansion_steps", None),
                "pipeline_synthesis_calls": resp.synthesis_calls,
                "synthesis_request_ids": current_case_trace["synthesis_request_ids"],
                "raw_answer": current_case_trace["raw_answer"],
                "raw_citations": current_case_trace["raw_citations"],
                "latency_ms": case_latency_ms,
                "delivered_answer": resp.answer,
                "delivered_citations": [
                    {
                        "chunk_id": c.get("chunk_id"),
                        "document_version": c.get("document_version"),
                    }
                    for c in (resp.citations or [])
                ],
                "laya_http_trace": laya_trace,
            })

    return {
        "workspace_id": ws_id,
        "source_version": ver,
        "offline_stub": args.offline_stub,
        "environment": live_meta["effective_environment"],
        "synthesis_provider": live_meta["synthesis_provider"],
        "synthesis_model_id": live_meta["synthesis_model_id"],
        "retries": {"laya_max_retries": args.max_retries, "bedrock_retries": bedrock_cfg.retries},
        "caps": {
            "max_laya_calls": args.max_laya_calls,
            "max_synthesis_calls": args.max_synthesis_calls,
            "max_embedding_calls": args.max_embedding_calls,
        },
        "outbound_attempts": {
            "laya_attempts": tracker.laya_calls,
            "synthesis_attempts": tracker.synthesis_calls,
            "embedding_attempts": tracker.embedding_calls,
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded 3-arm LAYA smoke runner")
    parser.add_argument("--workspace-id", default="ws_b540c5784fef")
    parser.add_argument("--source-version", default="2de945512ab7b3668c08374d4867c038")
    parser.add_argument("--arms", default="deterministic,laya_old,laya_dynamic")
    parser.add_argument("--max-laya-calls", type=int, default=4)
    parser.add_argument("--max-synthesis-calls", type=int, default=6)
    parser.add_argument("--max-embedding-calls", type=int, default=6)
    parser.add_argument("--max-retries", type=int, default=0)
    parser.add_argument("--offline-stub", action="store_true")
    args = parser.parse_args()
    summary = run_smoke(args)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
