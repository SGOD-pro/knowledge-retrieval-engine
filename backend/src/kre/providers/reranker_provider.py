"""Reranker provider — API-based document reranking.

Model Provider Matrix (ARCHITECTURE.md rev 5):
  - Prod: cohere.rerank-v3-5 (Bedrock)
  - Dev:  Same as Prod

Rule 6: Reranker runs before compression. Always.
Rule 28: All reranker calls route through this module.
"""

import json
import logging
import os

from kre.providers.provider_client import get_active_provider
from kre.shared.reranker_config import get_reranker_client
from kre.shared.bedrock_models import get_reranker_model

logger = logging.getLogger(__name__)


def rerank_documents(query: str, documents: list[str], provider: str | None = None) -> list[float]:
    """Score a list of document strings against a query using Bedrock reranker.

    Returns a list of relevance scores (floats) aligned with the input documents list.
    """
    try:
        client = get_reranker_client()

        body = {
            "query": query,
            "documents": documents,
            "top_n": len(documents),
            "api_version": 2,
        }

        response = client.invoke_model(
            modelId=get_reranker_model(),
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
        logger.error("Reranker failed: %s", str(e))

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
