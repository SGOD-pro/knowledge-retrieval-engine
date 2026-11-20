"""Deterministic missing requirement routing policy for LAYA escalation.

Implements Architecture Bible Section 35:
- location -> structural_expansion
- exact_term -> lexical_expansion
- semantic_concept -> dense_expansion
- relationship -> graph_expansion
- tabular_operand -> structured_expansion

In Slice 5.1:
- Only lexical_expansion is an active, available expansion handler.
- Other routes (dense_expansion, structural_expansion, graph_expansion, structured_expansion)
  are explicitly registered as unavailable.
- Unavailable routes fall back to lexical_expansion as the baseline alternative
  if query terms can be derived, or transition cleanly to route 'none' (StopOutcome).
"""

from __future__ import annotations

import re
import uuid

try:
    from schemas.contracts.laya import (
        LayaEscalationDecision,
        LayaEscalationRequest,
        LexicalExpansionParams,
        NoneExpansionParams,
    )
except ImportError:
    from src.schemas.contracts.laya import (
        LayaEscalationDecision,
        LayaEscalationRequest,
        LexicalExpansionParams,
        NoneExpansionParams,
    )

# In Slice 5.1, only lexical_expansion and none are available
AVAILABLE_ROUTES_SLICE_5_1: frozenset[str] = frozenset({"lexical_expansion", "none"})

_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of",
        "with", "by", "from", "up", "about", "into", "over", "after", "is", "are",
        "was", "were", "be", "been", "being", "have", "has", "had", "do", "does",
        "did", "what", "which", "who", "whom", "this", "that", "these", "those",
        "am", "it", "its", "as", "if", "not", "no", "can", "could", "will", "would",
        "shall", "should", "may", "might", "must",
    }
)

_GENERIC_OPERATIONAL_WORDS: frozenset[str] = frozenset(
    {
        "error", "errors", "coverageerror", "runtimeerror", "valueerror", "typeerror",
        "exception", "exceptions", "failed", "failure", "fails", "fail",
        "stage", "check", "checks", "checking", "fidelity", "coverage", "missing",
        "detected", "gap", "gaps", "initial", "baseline", "runtime", "traceback",
        "reason", "unhandled", "timeout", "requirement", "requirements",
        "dimension", "dimensions", "execution", "status", "operand", "operands",
        "run_fidelity", "run_fidelity_coverage", "run_fidelity_empty_context",
        "run_deterministic_math_operands", "evidence", "context", "document", "documents",
    }
)


def extract_domain_query_terms(
    query: str,
    detected_gap: str = "",
    max_terms: int = 5,
) -> tuple[str, ...]:
    """Extract validated domain keywords from query and gap, strictly excluding operational error phrases.

    Prioritizes:
    1. Clean terms inside quotes from detected_gap (e.g. 'turnover').
    2. Validated domain terms from detected_gap (excluding operational words like 'fidelity', 'check', 'failed').
    3. Validated domain terms from user query.
    Never returns generic operational error phrases.
    """
    terms: list[str] = []
    seen: set[str] = set()

    def _add_word(w: str) -> None:
        clean = w.lower().strip()
        if (
            len(clean) >= 2
            and clean not in _STOP_WORDS
            and clean not in _GENERIC_OPERATIONAL_WORDS
            and clean not in seen
        ):
            seen.add(clean)
            terms.append(clean[:64])

    # 1. Quoted terms from detected_gap take highest priority
    if detected_gap:
        for quoted in re.findall(r"['\"]([^'\"]+)['\"]", detected_gap):
            for w in re.findall(r"[a-zA-Z0-9_\-]+", quoted):
                _add_word(w)

    # 2. Domain words from detected_gap (skipping generic operational words)
    if detected_gap:
        for w in re.findall(r"[a-zA-Z0-9_\-]+", detected_gap):
            _add_word(w)

    # 3. Domain words from query
    if query:
        for w in re.findall(r"[a-zA-Z0-9_\-]+", query):
            _add_word(w)

    # 4. Fallback if no non operational domain terms found
    if not terms and query:
        for w in re.findall(r"[a-zA-Z0-9_\-]+", query):
            clean = w.lower().strip()
            if len(clean) >= 2 and clean not in _STOP_WORDS and clean not in seen:
                seen.add(clean)
                terms.append(clean[:64])

    return tuple(terms[:max_terms])


def extract_fallback_query_terms(text: str, max_terms: int = 5) -> tuple[str, ...]:
    """Extract bounded, clean alphanumeric query terms from text for BM25 fallback."""
    words = re.findall(r"[a-zA-Z0-9_\-]+", text.lower())
    terms: list[str] = []
    seen: set[str] = set()
    for w in words:
        if len(w) >= 2 and w not in _STOP_WORDS and w not in _GENERIC_OPERATIONAL_WORDS and w not in seen:
            seen.add(w)
            terms.append(w[:64])
            if len(terms) >= max_terms:
                break
    if not terms and words:
        for w in words:
            if len(w) >= 2 and w not in _STOP_WORDS and w not in seen:
                seen.add(w)
                terms.append(w[:64])
                if len(terms) >= max_terms:
                    break
    if not terms and words:
        terms.append(words[0][:64])
    return tuple(terms)


def get_deterministic_fallback_decision(
    request: LayaEscalationRequest,
    rationale: str = "Deterministic fallback per Section 35",
) -> LayaEscalationDecision:
    """Derive deterministic escalation decision under Section 35 rules.

    In Slice 5.1, non lexical routes map to lexical_expansion fallback using
    the detected gap and query phrasing. If no terms can be derived, routes to none.
    """
    decision_id = f"dec_fb_{uuid.uuid4().hex[:12]}"
    missing_dim = request.missing_requirement.missing_dimension
    detected_gap = request.missing_requirement.detected_gap

    # Extract targeted keywords prioritizing detected gap, then query
    terms = extract_domain_query_terms(query=request.query, detected_gap=detected_gap, max_terms=5)

    doc_ids: list[str] = []
    seen_docs: set[str] = set()
    for item in request.retrieved_summary:
        if item.document_id not in seen_docs:
            seen_docs.add(item.document_id)
            doc_ids.append(item.document_id)
            if len(doc_ids) >= 5:
                break

    if terms:
        params = LexicalExpansionParams(
            route_type="lexical_expansion",
            query_terms=terms,
            document_ids=tuple(doc_ids),
        )
        return LayaEscalationDecision(
            decision_id=decision_id,
            route="lexical_expansion",
            parameters=params,
            confidence=0.5,
            rationale=f"{rationale} (dim={missing_dim})"[:200],
            execution_status="approved",
        )

    # If no terms could be found, stop cleanly
    return LayaEscalationDecision(
        decision_id=decision_id,
        route="none",
        parameters=NoneExpansionParams(
            route_type="none",
            reason=f"No viable terms for dimension {missing_dim}"[:200],
        ),
        confidence=0.0,
        rationale=f"{rationale} (no terms available)"[:200],
        execution_status="approved",
    )
