"""Reranker provider — API-based document reranking.

Model Provider Matrix (ARCHITECTURE.md rev 5):
  - Prod: cohere.rerank-v3-5 (Bedrock)
  - Dev:  nvidia/llama-nemotron-rerank-vl-1b-v2 (OpenRouter)

Rule 6: Reranker runs before compression. Always.
Rule 28: All reranker calls route through this module.
"""

import json
import logging
import os
import requests

from kre.shared.providers.provider_client import get_active_provider, enforce_rate_limit
from kre.shared.config import get_reranker_model, get_boto3_client

logger = logging.getLogger(__name__)


def rerank_documents(query: str, documents: list[str], provider: str | None = None) -> list[float]:
    """Score a list of document strings against a query using the active reranker provider.

    Returns a list of relevance scores (floats) aligned with the input documents list.
    """
    active = provider or get_active_provider()
    model_id = get_reranker_model(active)
    enforce_rate_limit(model_id)

    if active == "prod":
        try:
            from kre.shared.config import get_boto3_client
            client = get_boto3_client("bedrock-runtime")

            body = {
                "query": query,
                "documents": documents,
                "top_n": len(documents),
            }

            response = client.invoke_model(
                modelId=model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )

            response_body = json.loads(response.get("body").read())
            results = response_body.get("results", [])

            scores = [0.0] * len(documents)
            for r in results:
                scores[r["index"]] = float(r["relevance_score"])

            return scores

        except Exception as e:
            logger.error("Prod reranker failed: %s", str(e))

    elif active == "dev":
        openrouter_key = os.environ.get("OPENROUTER_API_KEY")
        if openrouter_key:
            try:
                response = requests.post(
                    "https://openrouter.ai/api/v1/rerank",
                    headers={
                        "Authorization": f"Bearer {openrouter_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_id,
                        "query": query,
                        "documents": documents,
                        "top_n": len(documents),
                    },
                    timeout=30.0,
                )
                if response.status_code == 200:
                    data = response.json()
                    results = data.get("results", [])
                    scores = [0.0] * len(documents)
                    for r in results:
                        scores[r["index"]] = float(r["relevance_score"])
                    return scores
                else:
                    logger.error("Dev reranker returned %d: %s", response.status_code, response.text)
            except Exception as e:
                logger.error("Dev reranker request failed: %s", str(e))

    # Deterministic fallback — Jaccard word overlap scoring
    scores = []
    query_words = set(query.lower().split())
    if not query_words:
        return [0.0] * len(documents)

    for doc in documents:
        doc_words = set(doc.lower().split())
        if not doc_words:
            scores.append(0.0)
            continue
        intersection = len(query_words.intersection(doc_words))
        union = len(query_words.union(doc_words))
        scores.append(float(intersection) / float(union))

    return scores
