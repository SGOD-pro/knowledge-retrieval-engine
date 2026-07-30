import json
import logging
import re
import time
from typing import Any

from kre.shared.providers.llm_provider import generate_completion

logger = logging.getLogger(__name__)

# Simple regex-based token counter estimate (1 token ~ 4 chars for rough limits)
# For strict 1200 token limits, we enforce a character limit of 1200 * 4 = 4800 characters
# The compressor should already be limiting this, but we enforce it here.
MAX_CONTEXT_CHARS = 4800

def _strip_markdown_json(text: str) -> str:
    """Strip markdown code blocks around JSON."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()

def call(query: str, compressed_context: str, provider: str | None = None) -> dict[str, Any]:
    """
    Call the LLM using the provided query and compressed context.
    Enforces Rule 2 (one call limit is implicit by calling this function once),
    Rule 4 (1200 token limit), and Rule 15 (strip markdown JSON, ensure no confidence in schema).
    """
    start_time = time.perf_counter()
    
    # Enforce 1200 token (~4800 char) context limit programmatically
    if len(compressed_context) > MAX_CONTEXT_CHARS:
        logger.warning("Context exceeded 4800 chars. Truncating to enforce Rule 4.")
        compressed_context = compressed_context[:MAX_CONTEXT_CHARS]
        
    system_prompt = (
        "You are a factual knowledge retrieval assistant. Your goal is to answer "
        "the user's query using strictly the provided context.\n"
        "Return a JSON object with the following schema:\n"
        "{\n"
        '  "answer": "Your detailed answer to the query",\n'
        '  "citations": ["chunk_id_1", "chunk_id_2"]\n'
        "}\n"
        "Do NOT include a 'confidence', 'certainty', or 'score' field (Rule 15).\n"
        "If the answer cannot be found in the context, return NOT_FOUND for the answer."
    )
    
    user_prompt = f"Context:\n{compressed_context}\n\nQuery: {query}"
    
    raw_response = generate_completion(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        provider=provider,
        temperature=0.0
    )
    
    cleaned_json = _strip_markdown_json(raw_response)
    
    try:
        parsed_response = json.loads(cleaned_json)
    except json.JSONDecodeError:
        logger.error("LLM returned invalid JSON: %s", raw_response)
        parsed_response = {"answer": "NOT_FOUND", "citations": []}
        
    # Enforce Rule 15 programmatically just in case
    for forbidden_key in ["confidence", "certainty", "score"]:
        if forbidden_key in parsed_response:
            del parsed_response[forbidden_key]
            
    latency_ms = (time.perf_counter() - start_time) * 1000.0
    # LLM module also needs to log latency and confidence score according to Rule 10
    # Wait, confidence is scored in Stage 10. LLM just logs latency and maybe a placeholder confidence?
    # "Every module logs latency_ms and confidence_score"
    # We will log confidence_score=0.0 here since confidence is computed later.
    logger.info("llm.latency_ms=%.2f llm.confidence_score=0.00", latency_ms)
    
    return parsed_response
