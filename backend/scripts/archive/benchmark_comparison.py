raise RuntimeError("Obsolete runner. Use backend/scripts/run_canonical_60_benchmark.py")
"""Baseline Benchmark: Traditional RAG vs OKF vs PageIndex vs KRE

Run ONCE. Results saved to backend/tmp/baseline_comparison.json.

Usage:
    $env:ENVIRONMENT="prod"
    uv run python tests/benchmark_comparison.py

What this does:
  1. Ingests all docs from tests/data/advance/ (PDFs via ODL Lambda, CSVs row-level)
  2. Loads tests/data/advance/query.json (108 queries), randomly samples 60 (seed=42)
  3. Runs each query against 4 methods: Traditional RAG, PageIndex, OKF-only, KRE
  4. Tracks: latency, LLM input/output tokens, embed tokens, Qdrant queries, NOT_FOUND rate
  5. Scores: ROUGE-L vs expected_answer, LLM-as-judge faithfulness (Nova Lite T=0)
  6. Prints a full comparison table — NO SUGARCOATING
  7. Saves to tmp/baseline_comparison.json

Methods compared:
  A. Traditional RAG  — naive chunk-level cosine similarity, no PageIndex, no OKF
  B. PageIndex only   — structural filtering before vector search, no OKF, no reranker
  C. OKF only         — typed property lookup, zero vector search
  D. KRE (our system) — BM25 + PageIndex + OKF + Vector + Rerank + Fidelity + LLM
"""

import json
import logging
import os
import random
import sys
import time
from pathlib import Path

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("benchmark")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).parent / "data" / "advance"
OUT_DIR = Path(__file__).parent.parent / "tmp"
OUT_FILE = OUT_DIR / "baseline_comparison.json"
QUERY_FILE = DATA_DIR / "query.json"

# DOCS: all documents in the advance/ directory
DOCS = [
    DATA_DIR / "hdfc-mutual-fund-handbook.pdf",
    DATA_DIR / "Workflow Documentation.docx",
    DATA_DIR / "submission.pptx",
    DATA_DIR / "1706.03762v7.pdf",
    DATA_DIR / "2204.13154v1.pdf",
    DATA_DIR / "2507.19595v3.pdf",
    DATA_DIR / "National-Strategy-for-Artificial-Intelligence.pdf",
    DATA_DIR / "Govt_Colleges_TeachingStaff_Position_2024_25_0.csv",
    DATA_DIR / "rs_Bills_Passed_Returned_from_session_217-241.csv",
]

# Cap OKF Tier-3 extraction to first N chunks per document (LLM budget control)
OKF_CHUNK_CAP = 200
# Hard cap on total chunks per document for embedding (handles large CSVs)
DOC_CHUNK_CAP = 500
# Random seed for reproducible 60-query sampling
QUERY_SAMPLE_SEED = 42
QUERY_SAMPLE_SIZE = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rouge_l(hypothesis: str, reference: str) -> float:
    """Approximate ROUGE-L: LCS length / reference length."""
    if not reference or not hypothesis:
        return 0.0
    hyp_words = hypothesis.lower().split()
    ref_words = reference.lower().split()
    m, n = len(ref_words), len(hyp_words)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if ref_words[i - 1] == hyp_words[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    lcs = dp[m][n]
    return lcs / max(1, m)


def _faithfulness_score(query: str, answer: str, context: str) -> tuple[float, dict]:
    """LLM-as-judge faithfulness scoring via Nova Lite (T=0). Returns 0.0-1.0."""
    if not answer or answer == "NOT_FOUND" or not context or not context.strip():
        return 0.0, {"input_tokens": 0, "output_tokens": 0}
    try:
        from aws.infra import get_client

        client = get_client("bedrock-runtime")
        prompt = (
            f"Query: {query}\n\nContext: {context[:2000]}\n\nAnswer: {answer}\n\n"
            "Rate how faithful this answer is to the context. "
            "Return only a number from 0.0 (hallucinated) to 1.0 (fully supported). "
            "No explanation."
        )
        from providers.bedrock_models import get_llm_model

        resp = client.converse(
            modelId=get_llm_model(),
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"temperature": 0.0, "maxTokens": 10},
        )
        raw = resp["output"]["message"]["content"][0]["text"].strip()
        score = float(raw.split()[0])
        raw_usage = resp.get("usage", {})
        usage = {
            "input_tokens": raw_usage.get("inputTokens", 0),
            "output_tokens": raw_usage.get("outputTokens", 0),
        }
        return min(1.0, max(0.0, score)), usage
    except Exception as e:
        logger.warning("faithfulness_score_failed error=%s", e)
        return 0.0, {"input_tokens": 0, "output_tokens": 0}


def _qdrant_tracker():
    """Returns a context-managed tracker that monkey-patches Qdrant to count queries."""

    class QdrantTracker:
        def __init__(self):
            self.query_count = 0
            self.total_payload_bytes = 0

        def record(self, result_points: list):
            self.query_count += 1
            for pt in result_points:
                self.total_payload_bytes += len(
                    json.dumps(
                        pt.payload if hasattr(pt, "payload") else {}, default=str
                    )
                )

    return QdrantTracker()


# ---------------------------------------------------------------------------
# Method A: Traditional RAG (naive cosine only, no guardrails)
# ---------------------------------------------------------------------------


def run_traditional_rag(query: str, all_chunks) -> dict:
    """Embed query via Titan → cosine similarity → top-5 → LLM."""
    import numpy as np

    from providers.embedding_provider import (
        embed_text,
        get_token_counter,
        reset_token_counter,
    )

    t0 = time.perf_counter()
    result = {"method": "traditional_rag", "query": query}

    reset_token_counter()

    try:
        q_emb = embed_text(query)
        embed_usage_q = get_token_counter().copy()

        # Cosine similarity against all chunks with full embedding
        scored = []
        for chunk in all_chunks:
            if chunk.embedding_full:
                sim = float(np.dot(q_emb, chunk.embedding_full))
                scored.append((sim, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        top5 = [c for _, c in scored[:5]]
        context = "\n\n".join(c.text for c in top5)

        from services.llm.llm_service import call as llm_call

        t_llm = time.perf_counter()
        llm_resp = llm_call(query, context)
        llm_latency = (time.perf_counter() - t_llm) * 1000.0
        llm_usage = llm_resp.get("usage", {"input_tokens": 0, "output_tokens": 0})

        answer = llm_resp.get("answer", "NOT_FOUND")
        result.update(
            {
                "answer": answer,
                "context_snippet": context[:500],
                "context_chunks": len(top5),
                "latency_ms": (time.perf_counter() - t0) * 1000.0,
                "llm_latency_ms": llm_latency,
                "llm_input_tokens": llm_usage.get("input_tokens", 0),
                "llm_output_tokens": llm_usage.get("output_tokens", 0),
                "llm_calls": 0 if answer == "NOT_FOUND" and not context.strip() else 1,
                "embed_input_tokens": embed_usage_q.get("embed_input_tokens", 0),
                "embed_calls": embed_usage_q.get("embed_calls", 0),
                "qdrant_queries": 0,  # Traditional RAG uses in-memory cosine, not Qdrant
                "top_sim_score": scored[0][0] if scored else 0.0,
            }
        )
    except Exception as e:
        result["error"] = str(e)
        result["latency_ms"] = (time.perf_counter() - t0) * 1000.0
        logger.error("traditional_rag.failed query=%s error=%s", query[:50], e)

    return result


# ---------------------------------------------------------------------------
# Method B: PageIndex only
# ---------------------------------------------------------------------------


def run_page_index(query: str, all_chunks) -> dict:
    """BM25 → PageIndex structural filter → Vector search → top-5 → LLM."""
    from providers.embedding_provider import get_token_counter, reset_token_counter

    t0 = time.perf_counter()
    result = {"method": "page_index", "query": query}

    reset_token_counter()
    qdrant_queries = 0

    try:
        from db.database import CloudRepository
        from services.retrieval.bm25_retriever import BM25Retriever
        from services.retrieval.page_index_retriever import PageIndexRetriever
        from services.retrieval.vector_retriever import VectorRetriever

        bm25_results = BM25Retriever().search(query, all_chunks, top_k=20)
        bm25_chunks = [c for c, _ in bm25_results]

        _, candidate_pages, c_ids = PageIndexRetriever().filter_and_rank(
            query, bm25_chunks
        )

        repo = CloudRepository()
        vec_results = VectorRetriever(repository=repo).search(
            query=query,
            fast_path=False,
            candidate_page_ids=candidate_pages,
            candidate_chunk_ids=c_ids,
            top_k=10,
        )
        qdrant_queries += 1
        top5 = [c for c, _ in vec_results[:5]]
        context = "\n\n".join(c.text for c in top5)
        embed_usage = get_token_counter().copy()

        from services.llm.llm_service import call as llm_call

        t_llm = time.perf_counter()
        llm_resp = llm_call(query, context)
        llm_latency = (time.perf_counter() - t_llm) * 1000.0
        llm_usage = llm_resp.get("usage", {"input_tokens": 0, "output_tokens": 0})

        answer = llm_resp.get("answer", "NOT_FOUND")
        result.update(
            {
                "answer": answer,
                "context_snippet": context[:500],
                "context_chunks": len(top5),
                "candidate_pages": len(candidate_pages),
                "latency_ms": (time.perf_counter() - t0) * 1000.0,
                "llm_latency_ms": llm_latency,
                "llm_input_tokens": llm_usage.get("input_tokens", 0),
                "llm_output_tokens": llm_usage.get("output_tokens", 0),
                "llm_calls": 0 if answer == "NOT_FOUND" and not context.strip() else 1,
                "embed_input_tokens": embed_usage.get("embed_input_tokens", 0),
                "embed_calls": embed_usage.get("embed_calls", 0),
                "qdrant_queries": qdrant_queries,
            }
        )
    except Exception as e:
        result["error"] = str(e)
        result["latency_ms"] = (time.perf_counter() - t0) * 1000.0
        logger.error("page_index.failed query=%s error=%s", query[:50], e)

    return result


# ---------------------------------------------------------------------------
# Method C: OKF only
# ---------------------------------------------------------------------------


def run_okf_only(query: str) -> dict:
    """Entity extraction → DynamoDB lookup → answer from typed facts (zero LLM)."""
    t0 = time.perf_counter()
    result = {"method": "okf_only", "query": query}

    try:
        from services.retrieval.okf_retriever import OKFRetriever
        from services.retrieval.planner import extract_entities

        entities = extract_entities(query)
        t_okf = time.perf_counter()
        props = OKFRetriever().lookup(entities)
        okf_latency = (time.perf_counter() - t_okf) * 1000.0

        if props:
            answer = "; ".join(
                f"{p.get('concept')} — {p.get('property_name')}: {p.get('property_value')}"
                for p in props[:5]
            )
        else:
            answer = "NOT_FOUND"

        result.update(
            {
                "answer": answer,
                "context_snippet": "",
                "entities_extracted": entities,
                "properties_found": len(props),
                "okf_latency_ms": okf_latency,
                "latency_ms": (time.perf_counter() - t0) * 1000.0,
                "llm_calls": 0,
                "llm_input_tokens": 0,
                "llm_output_tokens": 0,
                "embed_input_tokens": 0,
                "embed_calls": 0,
                "qdrant_queries": 0,
            }
        )
    except Exception as e:
        result["error"] = str(e)
        result["latency_ms"] = (time.perf_counter() - t0) * 1000.0
        logger.error("okf_only.failed query=%s error=%s", query[:50], e)

    return result


# ---------------------------------------------------------------------------
# Method D: KRE full pipeline
# ---------------------------------------------------------------------------


def run_kre(query: str) -> dict:
    """KRE full pipeline: BM25+PageIndex+OKF+Vector+Rerank+Fidelity+LLM."""
    from providers.embedding_provider import get_token_counter, reset_token_counter

    t0 = time.perf_counter()
    result = {"method": "kre", "query": query}

    reset_token_counter()

    try:
        from services.langgraph_pipeline import pipeline

        response = pipeline.run(query)
        embed_usage = get_token_counter().copy()

        result.update(
            {
                "answer": response.answer,
                "context_snippet": response.context_snippet,
                "fast_path": response.fast_path,
                "confidence_score": response.confidence_score,
                "latency_ms": (time.perf_counter() - t0) * 1000.0,
                "stage_timings": response.stage_timings,
                "citation_count": len(response.citations),
                "llm_calls": 0 if response.fast_path else 1,
                "llm_input_tokens": 0,  # pipeline doesn't expose per-call usage yet — tracked via llm_service log
                "llm_output_tokens": 0,
                "embed_input_tokens": embed_usage.get("embed_input_tokens", 0),
                "embed_calls": embed_usage.get("embed_calls", 0),
                "qdrant_queries": len(
                    [k for k in response.stage_timings if "vector" in k]
                ),
            }
        )
    except Exception as e:
        result["error"] = str(e)
        result["latency_ms"] = (time.perf_counter() - t0) * 1000.0
        logger.error("kre.failed query=%s error=%s", query[:50], e)

    return result


# ---------------------------------------------------------------------------
# Ingest documents
# ---------------------------------------------------------------------------


def ingest_all() -> list:
    """Ingest all advance/ documents and return all chunks.

    PDFs → ODL Lambda (odl-parser-lambda-prod via boto3, env=prod).
    CSVs → row-level sentence chunking.
    All others → parse_file from ingestion.parse_service.
    """
    from ingestion.parse_service import parse_file

    from config import settings
    from db.database import CloudRepository
    from ingestion.embed_service import embed_chunks_dual
    from ingestion.okf_builder import build_okf
    from schemas.models import Document

    repo = CloudRepository()
    all_chunks = []

    for doc_path in DOCS:
        if not doc_path.exists():
            logger.warning("benchmark.doc_not_found path=%s — skipping", doc_path)
            continue
        logger.info("benchmark.ingesting path=%s", doc_path.name)
        t0 = time.perf_counter()
        try:
            doc = parse_file(doc_path)
            doc_id = doc.id
            doc_filename = doc.filename
            doc_format = doc.source_format
            chunks_raw = list(doc.chunks)
            raw_count = len(chunks_raw)
            logger.info(
                "benchmark.parsed doc=%s raw_chunks=%d", doc_path.name, raw_count
            )

            # Cap chunks for very large documents
            chunks_to_embed = chunks_raw[:DOC_CHUNK_CAP]
            if raw_count > DOC_CHUNK_CAP:
                logger.warning(
                    "benchmark.doc_capped doc=%s total=%d using=%d",
                    doc_path.name,
                    raw_count,
                    DOC_CHUNK_CAP,
                )

            # Embed: fast path (BGE ONNX) + full path (Titan API)
            orig_env = settings.ENVIRONMENT
            settings.ENVIRONMENT = "dev"  # force local ONNX for fast embeddings
            embedded_chunks = embed_chunks_dual(chunks_to_embed, provider="prod")
            settings.ENVIRONMENT = orig_env

            # Save to Qdrant + DynamoDB
            full_doc = Document(
                doc_id, doc_filename, doc_format, tuple(embedded_chunks)
            )
            repo.save(full_doc)
            all_chunks.extend(embedded_chunks)

            # OKF extraction (capped to control Nova Micro LLM budget)
            capped = embedded_chunks[:OKF_CHUNK_CAP]
            if len(embedded_chunks) > OKF_CHUNK_CAP:
                logger.warning(
                    "benchmark.okf_capped doc=%s total=%d capped=%d",
                    doc_path.name,
                    len(embedded_chunks),
                    OKF_CHUNK_CAP,
                )
            cap_doc = Document(doc_id, doc_filename, doc_format, tuple(capped))
            build_okf(cap_doc)

            logger.info(
                "benchmark.ingested doc=%s chunks=%d latency_ms=%.2f",
                doc_path.name,
                len(embedded_chunks),
                (time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            import traceback

            logger.error(
                "benchmark.ingest_failed doc=%s error=%s\n%s",
                doc_path.name,
                e,
                traceback.format_exc(),
            )

    if not all_chunks:
        logger.error("benchmark.no_chunks_ingested — all documents failed. Aborting.")
        sys.exit(1)

    return all_chunks


# ---------------------------------------------------------------------------
# Load and sample queries
# ---------------------------------------------------------------------------


def load_queries() -> list[dict]:
    """Load queries from query.json, randomly sample QUERY_SAMPLE_SIZE (seed=42)."""
    if QUERY_FILE.exists():
        with open(QUERY_FILE, encoding="utf-8") as f:
            all_queries = json.load(f)
        logger.info(
            "benchmark.queries_loaded total=%d from %s",
            len(all_queries),
            QUERY_FILE.name,
        )

        if len(all_queries) > QUERY_SAMPLE_SIZE:
            rng = random.Random(QUERY_SAMPLE_SEED)
            sampled = rng.sample(all_queries, QUERY_SAMPLE_SIZE)
            logger.info(
                "benchmark.queries_sampled count=%d seed=%d",
                QUERY_SAMPLE_SIZE,
                QUERY_SAMPLE_SEED,
            )
            return sampled
        return all_queries
    else:
        logger.warning(
            "benchmark.query_file_missing path=%s — using 0 queries", QUERY_FILE
        )
        return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def preflight_check():
    """Verify Bedrock is accessible before running the full benchmark."""
    logger.info("benchmark.preflight_check verifying Bedrock connection...")
    try:
        from providers.llm_provider import generate_completion

        text, usage = generate_completion(
            system_prompt="You are a helpful assistant.",
            user_prompt="Say 'OK' if you can read this.",
        )
        logger.info(
            "benchmark.preflight_check OK response=%r tokens=%s", text[:30], usage
        )
    except Exception as e:
        logger.error("benchmark.preflight_check FAILED error=%s", e)
        sys.exit(1)


def main():
    if OUT_FILE.exists():
        backup = OUT_FILE.with_suffix(f".{int(time.time())}.bak.json")
        OUT_FILE.rename(backup)
        logger.warning(
            "benchmark.prev_result_backed_up old=%s new=%s", OUT_FILE.name, backup.name
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    QUERIES = load_queries()
    if not QUERIES:
        logger.error("benchmark.no_queries — aborting")
        sys.exit(1)

    logger.info("benchmark.start docs=%d queries=%d", len(DOCS), len(QUERIES))

    # Pre-flight
    preflight_check()

    # Create tables if not exist
    from aws.infra import setup_infrastructure

    setup_infrastructure()

    # Ingest
    all_chunks = ingest_all()
    logger.info("benchmark.ingest_complete total_chunks=%d", len(all_chunks))

    # Keep ENVIRONMENT="dev" for the benchmark run
    # (Qdrant uses cloud by default, dev uses local endpoints for DynamoDB etc)

    results = {
        "queries": [],
        "summary": {},
        "metadata": {
            "query_sample_seed": QUERY_SAMPLE_SEED,
            "query_count": len(QUERIES),
            "doc_chunk_cap": DOC_CHUNK_CAP,
            "docs_ingested": [d.name for d in DOCS if d.exists()],
        },
    }

    # ---------------------------------------------------------------------------
    # Run each query against all 4 methods
    # ---------------------------------------------------------------------------
    for q in QUERIES:
        qid = q["id"]
        query = q["query"]
        qtype = q.get("type", "unknown")
        expected = q.get("expected_answer")
        source_file = q.get("source_file", "")
        logger.info("benchmark.query qid=%s type=%s query=%s", qid, qtype, query[:60])

        row = {
            "id": qid,
            "query": query,
            "type": qtype,
            "expected_answer": expected,
            "source_file": source_file,
            "methods": {},
        }

        time.sleep(1.5)  # Respect NVIDIA 40 RPM reranker limit

        # Method A: Traditional RAG
        logger.info("benchmark.method=traditional_rag qid=%s", qid)
        a_result = run_traditional_rag(query, all_chunks)
        a_answer = a_result.get("answer", "")
        a_context = a_result.get("context_snippet", "")
        a_faith, a_faith_usage = _faithfulness_score(query, a_answer, a_context)
        a_rouge = _rouge_l(a_answer, expected) if expected else None
        a_result["faithfulness"] = round(a_faith, 3)
        a_result["rouge_l"] = round(a_rouge, 3) if a_rouge is not None else None
        a_result["faith_input_tokens"] = a_faith_usage.get("input_tokens", 0)
        row["methods"]["traditional_rag"] = a_result
        logger.info(
            "benchmark.traditional_rag qid=%s latency_ms=%.2f answer_len=%d faith=%.2f",
            qid,
            a_result.get("latency_ms", 0),
            len(a_answer),
            a_faith,
        )

        # Method B: PageIndex
        logger.info("benchmark.method=page_index qid=%s", qid)
        b_result = run_page_index(query, all_chunks)
        b_answer = b_result.get("answer", "")
        b_context = b_result.get("context_snippet", "")
        b_faith, b_faith_usage = _faithfulness_score(query, b_answer, b_context)
        b_rouge = _rouge_l(b_answer, expected) if expected else None
        b_result["faithfulness"] = round(b_faith, 3)
        b_result["rouge_l"] = round(b_rouge, 3) if b_rouge is not None else None
        b_result["faith_input_tokens"] = b_faith_usage.get("input_tokens", 0)
        row["methods"]["page_index"] = b_result
        logger.info(
            "benchmark.page_index qid=%s latency_ms=%.2f pages=%d faith=%.2f",
            qid,
            b_result.get("latency_ms", 0),
            b_result.get("candidate_pages", 0),
            b_faith,
        )

        # Method C: OKF only
        logger.info("benchmark.method=okf_only qid=%s", qid)
        c_result = run_okf_only(query)
        c_answer = c_result.get("answer", "")
        c_faith, c_faith_usage = _faithfulness_score(query, c_answer, "")
        c_rouge = _rouge_l(c_answer, expected) if expected else None
        c_result["faithfulness"] = round(c_faith, 3)
        c_result["rouge_l"] = round(c_rouge, 3) if c_rouge is not None else None
        c_result["faith_input_tokens"] = c_faith_usage.get("input_tokens", 0)
        row["methods"]["okf_only"] = c_result
        logger.info(
            "benchmark.okf_only qid=%s latency_ms=%.2f props=%d faith=%.2f",
            qid,
            c_result.get("latency_ms", 0),
            c_result.get("properties_found", 0),
            c_faith,
        )

        # Method D: KRE
        logger.info("benchmark.method=kre qid=%s", qid)
        d_result = run_kre(query)
        d_answer = d_result.get("answer", "")
        d_context = d_result.get("context_snippet", "")
        d_faith, d_faith_usage = _faithfulness_score(query, d_answer, d_context)
        d_rouge = _rouge_l(d_answer, expected) if expected else None
        d_result["faithfulness"] = round(d_faith, 3)
        d_result["rouge_l"] = round(d_rouge, 3) if d_rouge is not None else None
        d_result["faith_input_tokens"] = d_faith_usage.get("input_tokens", 0)
        row["methods"]["kre"] = d_result
        logger.info(
            "benchmark.kre qid=%s latency_ms=%.2f fast_path=%s conf=%.3f faith=%.2f",
            qid,
            d_result.get("latency_ms", 0),
            d_result.get("fast_path"),
            d_result.get("confidence_score", 0),
            d_faith,
        )

        results["queries"].append(row)

    # ---------------------------------------------------------------------------
    # Compute summary stats per method — comprehensive cost + quality metrics
    # ---------------------------------------------------------------------------
    for method in ("traditional_rag", "page_index", "okf_only", "kre"):
        method_rows = [
            r["methods"][method] for r in results["queries"] if method in r["methods"]
        ]
        n = len(method_rows)
        if n == 0:
            continue

        latencies = [r.get("latency_ms", 0) for r in method_rows]
        not_found = sum(
            1 for r in method_rows if r.get("answer", "") in ("NOT_FOUND", "")
        )
        errors = sum(1 for r in method_rows if "error" in r)
        avg_lat = sum(latencies) / n
        p95_lat = sorted(latencies)[int(n * 0.95)]

        # LLM token usage
        llm_in_tokens = [r.get("llm_input_tokens", 0) for r in method_rows]
        llm_out_tokens = [r.get("llm_output_tokens", 0) for r in method_rows]
        total_llm_calls = sum(r.get("llm_calls", 0) for r in method_rows)
        avg_in = sum(llm_in_tokens) / n
        avg_out = sum(llm_out_tokens) / n
        max_in = max(llm_in_tokens) if llm_in_tokens else 0
        min_in = (
            min(t for t in llm_in_tokens if t > 0)
            if any(t > 0 for t in llm_in_tokens)
            else 0
        )

        # Embedding token usage
        embed_tokens = [r.get("embed_input_tokens", 0) for r in method_rows]
        total_embed_tokens = sum(embed_tokens)
        avg_embed = sum(embed_tokens) / n

        # Qdrant query count
        qdrant_queries = [r.get("qdrant_queries", 0) for r in method_rows]
        total_qdrant = sum(qdrant_queries)

        # ROUGE-L
        rouge_vals = [r["rouge_l"] for r in method_rows if r.get("rouge_l") is not None]
        avg_rouge = sum(rouge_vals) / len(rouge_vals) if rouge_vals else None

        # Faithfulness (only over answered queries)
        faith_vals = [
            r["faithfulness"]
            for r in method_rows
            if r.get("answer", "NOT_FOUND") not in ("NOT_FOUND", "")
        ]
        avg_faith = sum(faith_vals) / len(faith_vals) if faith_vals else None

        results["summary"][method] = {
            "query_count": n,
            "avg_latency_ms": round(avg_lat, 1),
            "p95_latency_ms": round(p95_lat, 1),
            "not_found_count": not_found,
            "not_found_rate_pct": round(not_found / n * 100, 1),
            "error_count": errors,
            "total_llm_calls": total_llm_calls,
            "avg_llm_input_tokens": round(avg_in, 1),
            "avg_llm_output_tokens": round(avg_out, 1),
            "max_llm_input_tokens": max_in,
            "min_llm_input_tokens": min_in,
            "total_embed_input_tokens": total_embed_tokens,
            "avg_embed_input_tokens": round(avg_embed, 1),
            "total_qdrant_queries": total_qdrant,
            "avg_rouge_l": round(avg_rouge, 3) if avg_rouge is not None else "N/A",
            "avg_faithfulness": round(avg_faith, 3) if avg_faith is not None else "N/A",
        }

    # Save
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info("benchmark.saved path=%s", OUT_FILE)

    # ---------------------------------------------------------------------------
    # Print full comparison table — NO SUGARCOATING
    # ---------------------------------------------------------------------------
    W = 130
    print("\n" + "=" * W)
    print("BASELINE COMPARISON RESULTS — NO SUGARCOATING")
    print("=" * W)
    print(
        f"{'Method':<20} {'Lat(avg)':>9} {'Lat(p95)':>9} {'NOT_FOUND%':>11} {'Errors':>7} "
        f"{'LLM calls':>10} {'InTok(avg)':>11} {'OutTok(avg)':>12} {'EmbTok(avg)':>12} "
        f"{'QdrantQ':>8} {'ROUGE-L':>8} {'Faith':>7}"
    )
    print("-" * W)
    for method, s in results["summary"].items():
        rouge_str = (
            f"{s['avg_rouge_l']:>8.3f}"
            if isinstance(s["avg_rouge_l"], float)
            else f"{'N/A':>8}"
        )
        faith_str = (
            f"{s['avg_faithfulness']:>7.3f}"
            if isinstance(s["avg_faithfulness"], float)
            else f"{'N/A':>7}"
        )
        print(
            f"{method:<20} {s['avg_latency_ms']:>9.1f} {s['p95_latency_ms']:>9.1f} "
            f"{s['not_found_rate_pct']:>10.1f}% {s['error_count']:>7} "
            f"{s['total_llm_calls']:>10} {s['avg_llm_input_tokens']:>11.0f} "
            f"{s['avg_llm_output_tokens']:>12.0f} {s['avg_embed_input_tokens']:>12.0f} "
            f"{s['total_qdrant_queries']:>8} {rouge_str} {faith_str}"
        )
    print("=" * W)
    print(f"\nFull results: {OUT_FILE}")
    print(
        f"Queries: {len(QUERIES)} (sampled from {QUERY_FILE.name} with seed={QUERY_SAMPLE_SEED})"
    )


if __name__ == "__main__":
    main()
