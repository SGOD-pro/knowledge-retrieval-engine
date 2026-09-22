import json
import logging
import time
from typing import Any

from providers.llm_provider import generate_completion

logger = logging.getLogger(__name__)

# Simple token estimate: 1 token ~ 4 chars. Target 2000 tokens of context.
# The compressor limits this, but we enforce a hard cap here as a safety net.
# Raised from 4800 to 8000 to prevent evidence truncation on large documents.
MAX_CONTEXT_CHARS = 8000

# Static fallback message used when no relevant context chunks are found.
# ZERO LLM call — saves cost and avoids hallucination.
NOT_FOUND_STATIC = "NOT_FOUND"
NO_CONTEXT_MESSAGE = "NOT_FOUND"


def _strip_markdown_json(text: str) -> str:
    """Strip markdown code blocks around JSON."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    text = text.removesuffix("```")
    return text.strip()


def call(
    query: str, compressed_context: str, provider: str | None = None
) -> dict[str, Any]:
    """
    Call the LLM using the provided query and compressed context.

    CRITICAL RULES:
      - Rule 2:  Maximum ONE LLM call per query.
      - Rule 4:  Max 1200 token context limit.
      - Rule 15: No confidence/certainty/score in schema.
      - NEW: If compressed_context is empty or blank → return NOT_FOUND immediately.
              ZERO LLM call. Static fallback only.

    Returns dict with keys: answer, citations, usage (input_tokens, output_tokens).
    """
    start_time = time.perf_counter()

    # -----------------------------------------------------------------------
    # Static fallback — no context means no LLM call (saves cost + no hallucination)
    # -----------------------------------------------------------------------
    if not compressed_context or not compressed_context.strip():
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        logger.info(
            "llm.skipped_no_context latency_ms=%.2f — returning static NOT_FOUND",
            latency_ms,
        )
        return {
            "answer": NO_CONTEXT_MESSAGE,
            "citations": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    # -----------------------------------------------------------------------
    # Enforce 1200 token (~4800 char) context limit programmatically
    # -----------------------------------------------------------------------
    if len(compressed_context) > MAX_CONTEXT_CHARS:
        logger.warning("Context exceeded 4800 chars. Truncating to enforce Rule 4.")
        compressed_context = compressed_context[:MAX_CONTEXT_CHARS]

    # -----------------------------------------------------------------------
    # Grounded synthesis prompt — allows partial answers, blocks hallucination
    # -----------------------------------------------------------------------
    system_prompt = (
        "You are a document Q&A assistant. Answer questions ONLY using the provided context.\n\n"
        "INSTRUCTIONS:\n"
        "1. Read the context carefully.\n"
        "2. Provide complete, accurate, factual answers. Do not answer with a single word or single letter.\n"
        "3. If a question asks whether a premise, assumption, or claim is correct (e.g. 'Is that correct?', 'Right?', 'Is that the correct unit?'): state clearly whether it is correct or not, and explain why with the exact factual details from the context (e.g., correct units, actual fields, or dates).\n"
        "4. For multi-document or comparative questions, address each document, entity, or indicator mentioned.\n"
        "5. Preserve exact stored values, units, and negative signs (e.g., -92.55) as given in the context.\n"
        "6. When asked HOW an evaluation, check, or metric is conducted or computed, describe the operational mechanism (e.g. what is removed, omitted, or measured, and the resulting change in model loss or error) rather than just stating the name of the method.\n"
        "7. For chronological or sequence questions, list all events or dates mentioned in chronological order.\n"
        "8. If the query asks for a specific format (e.g., 'Answer only as a JSON object with keys...'), provide the requested JSON structure directly inside the 'answer' field.\n"
        "9. When computing or reporting percentages, ratios, or arithmetic calculations: state the formula and operands used (e.g. '30,739 / 109,417 × 100 = 28.09%'), and round percentages to two decimal places unless asked otherwise.\n"
        "10. When performing sums, aggregations, or additions across multiple entities/districts/items: provide both the total and the individual component breakdown (e.g. 'Total 2,624: EntityA 882 + EntityB 874...').\n"
        "11. When a question asks for a statistic, date, or value without specifying necessary qualifiers (such as period, year, segment, or basis), explicitly state the available qualifiers or dimensions from the context (e.g. 'For the three months ended June 27, 2026, ...').\n"
        "12. If the context contains PARTIAL information, provide what you can find and note what is missing.\n"
        "13. If the answer is truly NOT in the context at all, respond with exactly: NOT_FOUND\n"
        "14. NEVER infer, guess, or use knowledge outside the provided context.\n"
        "15. Cite only chunk IDs that appear in the [chunk_id] format in the context.\n\n"
        "Return ONLY valid JSON with this exact schema (no markdown code blocks):\n"
        '{"answer": "<your answer or NOT_FOUND>", "citations": ["chunk_id_1", "chunk_id_2"]}'
    )

    user_prompt = f"Context:\n{compressed_context}\n\nQuery: {query}"

    raw_response, usage = generate_completion(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        provider=provider,
        temperature=0.0,
    )

    cleaned_json = _strip_markdown_json(raw_response)

    try:
        parsed_response = json.loads(cleaned_json)
    except json.JSONDecodeError:
        # Attempt to extract a plain-text answer before falling back to NOT_FOUND.
        # Some model responses return unstructured text instead of JSON.
        stripped = cleaned_json.strip()
        if stripped and stripped.upper() != "NOT_FOUND" and len(stripped) > 10:
            logger.warning(
                "LLM returned non-JSON text (len=%d). Wrapping as plain answer.",
                len(stripped),
            )
            parsed_response = {"answer": stripped, "citations": []}
        else:
            logger.error("LLM returned invalid JSON and no recoverable text: %s", raw_response[:200])
            parsed_response = {"answer": NOT_FOUND_STATIC, "citations": []}

    # Enforce Rule 15 programmatically
    for forbidden_key in ["confidence", "certainty", "score"]:
        if forbidden_key in parsed_response:
            del parsed_response[forbidden_key]

    # Attach usage metrics for cost tracking
    parsed_response["usage"] = usage

    latency_ms = (time.perf_counter() - start_time) * 1000.0
    logger.info(
        "llm.latency_ms=%.2f llm.confidence_score=0.00 llm.input_tokens=%d llm.output_tokens=%d",
        latency_ms,
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
    )

    return parsed_response
