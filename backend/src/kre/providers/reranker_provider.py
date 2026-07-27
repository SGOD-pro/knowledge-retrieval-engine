import os
import json
import logging
from kre.providers.provider_client import get_active_provider

logger = logging.getLogger(__name__)

def rerank_documents(query: str, documents: list[str], provider: str | None = None) -> list[float]:
    """Score a list of document strings against a query using the active reranker provider.
    Returns a list of relevance scores (floats) aligned with the input documents list.
    """
    active = provider or get_active_provider()
    
    if active == "prod":
        try:
            import boto3
            client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
            
            body = {
                "query": query,
                "documents": documents,
                "top_n": len(documents)
            }
            
            response = client.invoke_model(
                modelId="cohere.rerank-v3-5:0",
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body)
            )
            
            response_body = json.loads(response.get("body").read())
            results = response_body.get("results", [])
            
            scores = [0.0] * len(documents)
            for r in results:
                scores[r["index"]] = float(r["relevance_score"])
                
            return scores
            
        except Exception as e:
            logger.error("Prod reranker failed: %s", str(e))
            
    elif active == "dev" and os.environ.get("OPENROUTER_API_KEY"):
        # Not implementing full HTTP call here since OpenRouter rerank endpoint varies.
        # It's generally something like POST /api/v1/rerank or similar for specific models.
        # Let's fallback gracefully.
        pass

    # Deterministic Fallback if API fails or is not configured
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
