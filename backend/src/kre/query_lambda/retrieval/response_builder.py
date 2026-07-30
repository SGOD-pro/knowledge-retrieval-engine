from dataclasses import asdict, dataclass
from typing import Any, Sequence

from kre.shared.models import Chunk


@dataclass(frozen=True)
class Citation:
    chunk_id: str
    document_id: str
    source_format: str
    bounding_box: dict[str, float] | None
    location_reference: str | None
    text_snippet: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FastPathResponse:
    answer: str
    citations: list[Citation]
    confidence_score: float
    confidence_band: str
    fast_path: bool
    retrieval_path: list[str]
    latency_breakdown: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["citations"] = [c.to_dict() for c in self.citations]
        return d


def build_citation(chunk: Chunk) -> Citation:
    """Build a citation enforcing Rule 20 (zero null location responses)."""
    bbox = chunk.bounding_box
    loc_ref = chunk.location_reference

    # Rule 20 fallback check
    if chunk.source_format == "pdf":
        if bbox is None:
            bbox = {"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0, "page_number": chunk.page_number or 1}
    else:
        if not loc_ref:
            if chunk.element_type == "heading":
                loc_ref = f"Heading: {chunk.text[:30]}"
            elif chunk.page_number:
                loc_ref = f"Page: {chunk.page_number}"
            else:
                loc_ref = f"Section: {chunk.element_type}"

    return Citation(
        chunk_id=chunk.id,
        document_id=str(chunk.document_id),
        source_format=chunk.source_format,
        bounding_box=bbox,
        location_reference=loc_ref,
        text_snippet=chunk.text[:200],
    )


def build_fast_path_response(
    query: str,
    scored_chunks: Sequence[tuple[Chunk, float]],
    latency_breakdown: dict[str, float],
) -> FastPathResponse:
    if not scored_chunks:
        return FastPathResponse(
            answer="NOT_FOUND: No relevant documents found matching query.",
            citations=[],
            confidence_score=0.0,
            confidence_band="LOW",
            fast_path=True,
            retrieval_path=["bm25", "page_index", "vector"],
            latency_breakdown=latency_breakdown,
        )

    top_chunks = [c for c, _ in scored_chunks[:3]]
    citations = [build_citation(c) for c in top_chunks]

    # Combine top chunk texts into factual answer summary without LLM call (Rule 1)
    combined_answers = " ".join([c.text for c in top_chunks])
    answer = combined_answers[:500]

    avg_score = sum(s for _, s in scored_chunks[:3]) / max(1, len(top_chunks))
    # Coverage ratio estimate
    query_words = set(query.lower().split())
    answer_words = set(answer.lower().split())
    coverage = len(query_words.intersection(answer_words)) / max(1, len(query_words))

    confidence = min(1.0, max(0.0, (avg_score * 0.6) + (coverage * 0.4)))

    if confidence >= 0.75:
        band = "HIGH"
    elif confidence >= 0.50:
        band = "MEDIUM"
    else:
        band = "LOW"

    return FastPathResponse(
        answer=answer,
        citations=citations,
        confidence_score=round(confidence, 4),
        confidence_band=band,
        fast_path=True,
        retrieval_path=["bm25", "page_index", "vector"],
        latency_breakdown=latency_breakdown,
    )
