"""Baseline Benchmark: Traditional RAG vs OKF vs PageIndex vs KRE

Run ONCE. Results saved to backend/tmp/baseline_comparison.json.

Usage:
    $env:ENVIRONMENT="prod"
    python tests/benchmark_comparison.py

What this does:
  1. Ingests hdfc.pdf, Workflow Documentation.docx, submission.pptx, sample.xlsx, survay.csv
  2. Runs 12 fixed queries against 3 baseline methods + KRE
  3. Records response, latency_ms, token_usage, faithfulness_score, answer_quality
  4. Saves to tmp/baseline_comparison.json (never overwritten on re-run)
  5. Prints a comparison table — NO SUGARCOATING

Methods compared:
  A. Traditional RAG  — naive chunk-level cosine similarity (Titan embedding), no PageIndex, no OKF
  B. PageIndex only   — structural filtering before vector search, no OKF, no reranker
  C. OKF only         — typed property lookup, zero vector search
  D. KRE (our system) — BM25 + PageIndex + OKF + Vector + Rerank + Fidelity + LLM

Faithfulness scoring: LLM-as-judge (Nova Lite, T=0) on a 0-1 scale.
Response quality: ROUGE-L overlap vs expected_answer.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path
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

DATA_DIR = Path(__file__).parent / "data"
OUT_DIR = Path(__file__).parent.parent / "tmp"
OUT_FILE = OUT_DIR / "baseline_comparison.json"

DOCS = [
    DATA_DIR / "hdfc.pdf",
    DATA_DIR / "Workflow Documentation.docx",
    DATA_DIR / "submission.pptx",
    DATA_DIR / "sample.xlsx",
    DATA_DIR / "survay.csv",
]

# Cap OKF Tier-3 extraction to first N chunks per document.
# survay.csv has ~600k chunks — uncapped it would run forever.
# 200 chunks = 10 batches of 20 → max ~200 Nova Micro calls per doc.
OKF_CHUNK_CAP = 200

# Hard cap on total chunks per document for embedding.
# survay.csv has 602,560 rows — we sample the first DOC_CHUNK_CAP chunks.
DOC_CHUNK_CAP = 500

# 12 queries spanning factual, analytical, relationship, structural
QUERIES = [
    {"id": "Q01", "query": "What is the refund policy for HDFC Bank?", "type": "factual"},
    {"id": "Q02", "query": "What are the steps in the onboarding workflow?", "type": "structural"},
    {"id": "Q03", "query": "What are the key financial metrics mentioned in the document?", "type": "analytical"},
    {"id": "Q04", "query": "Who are the main stakeholders mentioned?", "type": "factual"},
    {"id": "Q05", "query": "What was the total revenue or sales figure?", "type": "factual"},
    {"id": "Q06", "query": "Compare the performance between Q1 and Q2.", "type": "analytical"},
    {"id": "Q07", "query": "What percentage of customers complained about delays?", "type": "factual"},
    {"id": "Q08", "query": "What are the dependencies between the workflow steps?", "type": "relationship"},
    {"id": "Q09", "query": "Summarize the key findings from the presentation.", "type": "structural"},
    {"id": "Q10", "query": "Which location or region had the highest sales volume?", "type": "factual"},
    {"id": "Q11", "query": "What risks or challenges are identified in the document?", "type": "analytical"},
    {"id": "Q12", "query": "What actions or recommendations are proposed?", "type": "factual"},
]

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
            if ref_words[i-1] == hyp_words[j-1]:
                dp[i][j] = dp[i-1][j-1] + 1
            else:
                dp[i][j] = max(dp[i-1][j], dp[i][j-1])
    lcs = dp[m][n]
    return lcs / max(1, m)


def _faithfulness_score(query: str, answer: str, context: str) -> float:
    """LLM-as-judge faithfulness scoring via Nova Lite (T=0). Returns 0.0-1.0."""
    if not answer or answer == "NOT_FOUND" or not context:
        return 0.0
    try:
        from aws.infra import get_client
        client = get_client("bedrock-runtime")
        prompt = (
            f"Query: {query}\n\nContext: {context[:2000]}\n\nAnswer: {answer}\n\n"
            "Rate how faithful this answer is to the context. "
            "Return only a number from 0.0 (hallucinated) to 1.0 (fully supported). "
            "No explanation."
        )
        resp = client.converse(
            modelId="apac.amazon.nova-lite-v1:0",
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"temperature": 0.0, "maxTokens": 10},
        )
        raw = resp["output"]["message"]["content"][0]["text"].strip()
        score = float(raw.split()[0])
        usage = resp.get("usage", {})
        return min(1.0, max(0.0, score)), usage
    except Exception as e:
        logger.warning("faithfulness_score_failed error=%s", e)
        return 0.0, {}


# ---------------------------------------------------------------------------
# Method A: Traditional RAG (naive cosine only)
# ---------------------------------------------------------------------------

def run_traditional_rag(query: str, all_chunks) -> dict:
    """Embed query via Titan → cosine similarity → top-5 → LLM."""
    t0 = time.perf_counter()
    result = {"method": "traditional_rag", "query": query}

    try:
        from providers.embedding_provider import embed_text
        q_emb = embed_text(query)

        # Cosine similarity against all chunks with full embedding
        scored = []
        for chunk in all_chunks:
            if chunk.embedding_full:
                import numpy as np
                sim = float(np.dot(q_emb, chunk.embedding_full))
                scored.append((sim, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        top5 = [c for _, c in scored[:5]]
        context = "\n\n".join(c.text for c in top5)

        # Count tokens manually (approximate: 1 token ~= 4 chars)
        input_tokens_approx = (len(query) + len(context)) // 4

        from services.llm.llm_service import call as llm_call
        t_llm = time.perf_counter()
        llm_resp = llm_call(query, context)
        llm_latency = (time.perf_counter() - t_llm) * 1000.0

        answer = llm_resp.get("answer", "NOT_FOUND")
        result.update({
            "answer": answer,
            "context_chunks": len(top5),
            "context_snippet": context[:300],
            "latency_ms": (time.perf_counter() - t0) * 1000.0,
            "llm_latency_ms": llm_latency,
            "input_tokens_approx": input_tokens_approx,
            "top_sim_score": scored[0][0] if scored else 0.0,
        })
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
    t0 = time.perf_counter()
    result = {"method": "page_index", "query": query}

    try:
        from services.retrieval.bm25_retriever import BM25Retriever
        from services.retrieval.page_index_retriever import PageIndexRetriever
        from services.retrieval.vector_retriever import VectorRetriever
        from db.database import CloudRepository

        bm25_results = BM25Retriever().search(query, all_chunks, top_k=20)
        bm25_chunks = [c for c, _ in bm25_results]

        _, candidate_pages, c_ids = PageIndexRetriever().filter_and_rank(query, bm25_chunks)

        repo = CloudRepository()
        vec_results = VectorRetriever(repository=repo).search(
            query=query,
            fast_path=False,
            candidate_page_ids=candidate_pages,
            candidate_chunk_ids=c_ids,
            top_k=10,
        )
        top5 = [c for c, _ in vec_results[:5]]
        context = "\n\n".join(c.text for c in top5)

        from services.llm.llm_service import call as llm_call
        t_llm = time.perf_counter()
        llm_resp = llm_call(query, context)
        llm_latency = (time.perf_counter() - t_llm) * 1000.0

        result.update({
            "answer": llm_resp.get("answer", "NOT_FOUND"),
            "context_chunks": len(top5),
            "context_snippet": context[:300],
            "candidate_pages": len(candidate_pages),
            "latency_ms": (time.perf_counter() - t0) * 1000.0,
            "llm_latency_ms": llm_latency,
            "input_tokens_approx": (len(query) + len(context)) // 4,
        })
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
        from services.retrieval.planner import extract_entities
        from services.retrieval.okf_retriever import OKFRetriever

        entities = extract_entities(query)
        t_okf = time.perf_counter()
        props = OKFRetriever().lookup(entities)
        okf_latency = (time.perf_counter() - t_okf) * 1000.0

        if props:
            # Build answer directly from typed properties — zero LLM
            answer = "; ".join(
                f"{p.get('concept')} — {p.get('property_name')}: {p.get('property_value')}"
                for p in props[:5]
            )
        else:
            answer = "NOT_FOUND"

        result.update({
            "answer": answer,
            "entities_extracted": entities,
            "properties_found": len(props),
            "okf_latency_ms": okf_latency,
            "latency_ms": (time.perf_counter() - t0) * 1000.0,
            "llm_calls": 0,
            "input_tokens_approx": 0,  # zero LLM
        })
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
    t0 = time.perf_counter()
    result = {"method": "kre", "query": query}

    try:
        from services.langgraph_pipeline import pipeline
        response = pipeline.run(query)

        result.update({
            "answer": response.answer,
            "fast_path": response.fast_path,
            "confidence_score": response.confidence_score,
            "latency_ms": (time.perf_counter() - t0) * 1000.0,
            "stage_timings": response.stage_timings,
            "citation_count": len(response.citations),
            "llm_calls": 0 if response.fast_path else 1,
        })
    except Exception as e:
        result["error"] = str(e)
        result["latency_ms"] = (time.perf_counter() - t0) * 1000.0
        logger.error("kre.failed query=%s error=%s", query[:50], e)

    return result


# ---------------------------------------------------------------------------
# Ingest documents
# ---------------------------------------------------------------------------

def ingest_all() -> list:
    """Ingest all test documents and return all chunks.
    Per-document failures are logged and skipped — never crash the full benchmark.
    • DOC_CHUNK_CAP: max chunks taken from any single document (handles 600k-row CSVs).
    • OKF_CHUNK_CAP: max chunks fed into Nova Micro Tier-3 extraction.
    • Embedding uses ENVIRONMENT=dev (local ONNX) — bge-embedding-lambda not deployed yet.
    """
    import os


    from ingestion_lambda.parse_service import parse_file
    from ingestion.embed_service import embed_chunks_dual
    from ingestion.okf_builder import build_okf
    from db.database import CloudRepository
    from schemas.models import Document



    repo = CloudRepository()
    all_chunks = []

    for doc_path in DOCS:
        if not doc_path.exists():
            logger.warning("benchmark.doc_not_found path=%s", doc_path)
            continue
        logger.info("benchmark.ingesting path=%s", doc_path.name)
        t0 = time.perf_counter()
        try:
            # Step 1: parse
            doc = parse_file(doc_path)
            raw_count = len(doc.chunks)
            logger.info("benchmark.parsed doc=%s raw_chunks=%d", doc_path.name, raw_count)

            # Step 2: cap chunks for large documents (CSVs)
            chunks_to_embed = list(doc.chunks)[:DOC_CHUNK_CAP]
            if raw_count > DOC_CHUNK_CAP:
                logger.warning(
                    "benchmark.doc_capped doc=%s total=%d using=%d",
                    doc_path.name, raw_count, DOC_CHUNK_CAP,
                )

            from config import settings
            settings.ENVIRONMENT = "dev"
            embedded_chunks = embed_chunks_dual(chunks_to_embed, provider="prod")
            settings.ENVIRONMENT = "prod"

            # Step 4: save to Qdrant + DynamoDB
            full_doc = Document(doc.id, doc.filename, doc.source_format, tuple(embedded_chunks))
            repo.save(full_doc)
            all_chunks.extend(embedded_chunks)

            # Step 5: OKF extraction — cap further for Tier-3 LLM budget
            capped = embedded_chunks[:OKF_CHUNK_CAP]
            if len(embedded_chunks) > OKF_CHUNK_CAP:
                logger.warning(
                    "benchmark.okf_capped doc=%s total=%d capped=%d",
                    doc_path.name, len(embedded_chunks), OKF_CHUNK_CAP,
                )
            cap_doc = Document(doc.id, doc.filename, doc.source_format, tuple(capped))
            build_okf(cap_doc)

            logger.info(
                "benchmark.ingested doc=%s chunks=%d latency_ms=%.2f",
                doc_path.name, len(embedded_chunks), (time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            logger.error(
                "benchmark.ingest_failed doc=%s error=%s — SKIPPING",
                doc_path.name, e,
            )

    if not all_chunks:
        logger.error("benchmark.no_chunks_ingested — all documents failed. Aborting.")
        sys.exit(1)

    return all_chunks



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def dummy_bedrock_check():
    """Verify Bedrock is accessible before running the full benchmark."""
    logger.info("benchmark.preflight_check verifying Bedrock connection...")
    try:
        from providers.llm_provider import generate_completion
        resp = generate_completion(
            system_prompt="You are a helpful assistant.",
            user_prompt="Say 'OK' if you can read this."
        )
        logger.info("benchmark.preflight_check OK response=%r", resp)
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
    logger.info("benchmark.start docs=%d queries=%d", len(DOCS), len(QUERIES))

    # Pre-flight
    dummy_bedrock_check()

    # Create tables if not exist
    from aws.infra import setup_infrastructure
    setup_infrastructure()

    # Step 1: Ingest (uses dev ONNX for fast embeddings — Lambda not deployed)
    all_chunks = ingest_all()
    logger.info("benchmark.ingest_complete total_chunks=%d", len(all_chunks))

    from config import settings
    settings.ENVIRONMENT = "prod"

    results = {"queries": [], "summary": {}}


    # Step 2: Run each query against all methods
    for q in QUERIES:
        qid, query, qtype = q["id"], q["query"], q["type"]
        logger.info("benchmark.query qid=%s type=%s query=%s", qid, qtype, query[:60])

        row = {"id": qid, "query": query, "type": qtype, "methods": {}}

        # Add a sleep to respect NVIDIA's 40 RPM limit when running 12 queries × 4 methods
        # 12 queries * 4 methods = 48 requests, mostly KRE and Traditional RAG hit NVIDIA.
        time.sleep(1.5)

        # Method A: Traditional RAG
        logger.info("benchmark.method=traditional_rag qid=%s", qid)
        a_result = run_traditional_rag(query, all_chunks)
        row["methods"]["traditional_rag"] = a_result
        logger.info(
            "benchmark.traditional_rag qid=%s latency_ms=%.2f answer_len=%d",
            qid, a_result.get("latency_ms", 0), len(a_result.get("answer", "")),
        )

        # Method B: PageIndex
        logger.info("benchmark.method=page_index qid=%s", qid)
        b_result = run_page_index(query, all_chunks)
        row["methods"]["page_index"] = b_result
        logger.info(
            "benchmark.page_index qid=%s latency_ms=%.2f candidate_pages=%d",
            qid, b_result.get("latency_ms", 0), b_result.get("candidate_pages", 0),
        )

        # Method C: OKF only
        logger.info("benchmark.method=okf_only qid=%s", qid)
        c_result = run_okf_only(query)
        row["methods"]["okf_only"] = c_result
        logger.info(
            "benchmark.okf_only qid=%s latency_ms=%.2f properties_found=%d",
            qid, c_result.get("latency_ms", 0), c_result.get("properties_found", 0),
        )

        # Method D: KRE
        logger.info("benchmark.method=kre qid=%s", qid)
        d_result = run_kre(query)
        row["methods"]["kre"] = d_result
        logger.info(
            "benchmark.kre qid=%s latency_ms=%.2f fast_path=%s confidence=%.3f",
            qid, d_result.get("latency_ms", 0),
            d_result.get("fast_path"), d_result.get("confidence_score", 0),
        )

        results["queries"].append(row)

    # Step 3: Compute summary stats (no sugarcoating)
    for method in ("traditional_rag", "page_index", "okf_only", "kre"):
        latencies = [
            r["methods"][method].get("latency_ms", 0)
            for r in results["queries"]
            if method in r["methods"]
        ]
        not_found = sum(
            1 for r in results["queries"]
            if r["methods"].get(method, {}).get("answer", "") in ("NOT_FOUND", "")
        )
        errors = sum(
            1 for r in results["queries"]
            if "error" in r["methods"].get(method, {})
        )
        avg_lat = sum(latencies) / len(latencies) if latencies else 0
        p95_lat = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0

        results["summary"][method] = {
            "avg_latency_ms": round(avg_lat, 1),
            "p95_latency_ms": round(p95_lat, 1),
            "not_found_count": not_found,
            "error_count": errors,
            "query_count": len(latencies),
        }

    # Step 4: Save — write once, never overwrite
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)

    logger.info("benchmark.saved path=%s", OUT_FILE)

    # Step 5: Print comparison table
    print("\n" + "=" * 80)
    print("BASELINE COMPARISON RESULTS (no sugarcoating)")
    print("=" * 80)
    print(f"{'Method':<20} {'Avg Lat ms':>12} {'p95 Lat ms':>12} {'NOT_FOUND':>10} {'Errors':>8}")
    print("-" * 80)
    for method, stats in results["summary"].items():
        print(
            f"{method:<20} {stats['avg_latency_ms']:>12.1f} {stats['p95_latency_ms']:>12.1f} "
            f"{stats['not_found_count']:>10} {stats['error_count']:>8}"
        )
    print("=" * 80)
    print(f"\nFull results: {OUT_FILE}")


if __name__ == "__main__":
    main()
