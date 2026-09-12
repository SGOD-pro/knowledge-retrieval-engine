"""Regression tests anchored to exact query strings from repro cases.

Case E: Out-of-corpus abstention ("what is capital of france")
Case D: BM25 cache determinism (same query × 5 → consistent results)
Case B: No heading-only answers ("what is bidirectional linear attention")
Case C: chunk_util merge guards (heading/page boundary checks)
Case A: Synthesis query routing ("what is the multihead attension machanish")
Case F: Fast-path routing embeds via Titan (unconditional for centroid routing)
Case H: Frontend faithfulness null vs || 98 (unit-level check)
"""

import re
import pytest

from schemas.models import Chunk
from services.langgraph_pipeline import end_fast_path
from ingestion.adapters.chunk_util import merge_and_split_chunks


# -----------------------------------------------------------------------
# Shared fixtures
# -----------------------------------------------------------------------

def _heading_chunk(text: str, page: int = 1, chunk_id: str = "h1") -> Chunk:
    return Chunk(
        id=chunk_id,
        document_id="doc_test",
        text=text,
        source_format="pdf",
        page_number=page,
        element_type="heading",
        section_path=("Root",),
        structural_weight=2.5,
        workspace_id="ws_test",
    )


def _paragraph_chunk(text: str, page: int = 1, chunk_id: str = "p1") -> Chunk:
    return Chunk(
        id=chunk_id,
        document_id="doc_test",
        text=text,
        source_format="pdf",
        page_number=page,
        element_type="paragraph",
        section_path=("Root",),
        structural_weight=1.0,
        similarity_score=0.85,
        workspace_id="ws_test",
    )


# -----------------------------------------------------------------------
# Case E: Out-of-corpus abstention
# -----------------------------------------------------------------------

class TestCaseE:
    """Query: "what is capital of france" — must abstain, not hallucinate."""

    def test_out_of_corpus_abstention(self):
        # Corpus has nothing about France — only ML/attention content
        chunks = [
            _paragraph_chunk(
                "The multi-head attention mechanism allows the model to jointly attend "
                "to information from different representation subspaces at different positions. "
                "Each head computes scaled dot-product attention independently.",
                chunk_id="p_ml_1",
            ),
            _paragraph_chunk(
                "Transformer architectures have become the dominant paradigm in natural "
                "language processing. The self-attention mechanism is the core component "
                "that enables parallel processing of sequences.",
                chunk_id="p_ml_2",
            ),
        ]
        state = {
            "query": "what is capital of france",
            "top_chunks": chunks,
            "candidate_chunks": chunks,
            "stage_timings": {},
        }
        result = end_fast_path(state)
        # Must NOT produce an answer about Paris (or anything substantive)
        answer = result["final_answer"]
        assert "paris" not in answer.lower(), (
            f"Hallucinated out-of-corpus answer: {answer!r}"
        )


# -----------------------------------------------------------------------
# Case D: BM25 cache determinism
# -----------------------------------------------------------------------

class TestCaseD:
    """Same query × 5 must produce consistent results."""

    def test_deterministic_retrieval(self):
        from services.retrieval.bm25_retriever import BM25Retriever

        chunks = [
            _paragraph_chunk(
                "Bidirectional linear attention enables efficient processing of "
                "sequences by computing attention in both forward and backward directions. "
                "This approach significantly reduces computational complexity.",
                chunk_id=f"p_{i}",
                page=i,
            )
            for i in range(1, 6)
        ]

        retriever = BM25Retriever()
        query = "what is bidirectional linear attention"

        results = []
        for _ in range(5):
            r = retriever.search(query, chunks, top_k=3)
            results.append([c[0].id for c in r])

        # All 5 runs must return identical chunk IDs in identical order
        for i in range(1, 5):
            assert results[i] == results[0], (
                f"Run {i+1} differed from run 1: {results[i]} vs {results[0]}"
            )


# -----------------------------------------------------------------------
# Case B: No heading-only answers
# -----------------------------------------------------------------------

class TestCaseB:
    """Query: "what is bidirectional linear attention" — answer must not be
    composed of heading fragments."""

    def test_no_heading_only_answer(self):
        heading = _heading_chunk("Bidirectional Linear Attention", chunk_id="h_bla")
        heading2 = _heading_chunk("Bidirectional Sparse Attention", chunk_id="h_bsa")
        paragraph = _paragraph_chunk(
            "Bidirectional linear attention enables efficient processing of "
            "sequences by computing attention weights in both causal and anti-causal "
            "directions simultaneously. This approach reduces the quadratic complexity "
            "of standard attention to linear time while preserving the ability to "
            "capture long-range dependencies.",
            chunk_id="p_bla",
        )

        state = {
            "query": "what is bidirectional linear attention",
            "top_chunks": [heading, heading2, paragraph],
            "candidate_chunks": [heading, heading2, paragraph],
            "stage_timings": {},
        }
        result = end_fast_path(state)
        answer = result["final_answer"]

        if answer == "NOT_FOUND":
            return  # abstention is acceptable

        # Answer must contain at least one sentence with ≥ 6 words
        sentences = re.split(r"(?<=[.!?])\s+", answer)
        long_sentences = [s for s in sentences if len(s.split()) >= 6]
        assert len(long_sentences) >= 1, (
            f"Answer has no sentences with ≥6 words: {answer!r}"
        )

    def test_heading_chunks_excluded_from_scoring(self):
        """If ONLY heading chunks are provided, answer must be NOT_FOUND."""
        h1 = _heading_chunk("Bidirectional Linear Attention", chunk_id="h1")
        h2 = _heading_chunk("Linear Attention Mechanisms", chunk_id="h2")

        state = {
            "query": "what is bidirectional linear attention",
            "top_chunks": [h1, h2],
            "candidate_chunks": [h1, h2],
            "stage_timings": {},
        }
        result = end_fast_path(state)
        assert result["final_answer"] == "NOT_FOUND", (
            f"Heading-only chunks produced an answer: {result['final_answer']!r}"
        )


# -----------------------------------------------------------------------
# Case C: chunk_util merge guards
# -----------------------------------------------------------------------

class TestCaseC:
    """Verify merge_and_split_chunks respects page and heading boundaries."""

    def test_no_cross_page_merge(self):
        c1 = _paragraph_chunk("Short text.", page=1, chunk_id="p1")
        c2 = _paragraph_chunk("Another short text.", page=2, chunk_id="p2")
        merged = merge_and_split_chunks([c1, c2], min_tokens=100)
        # Two chunks on different pages must not merge
        assert len(merged) == 2, (
            f"Cross-page merge occurred: {len(merged)} chunks instead of 2"
        )

    def test_no_heading_paragraph_merge(self):
        h = _heading_chunk("Section Title", page=1, chunk_id="h1")
        p = _paragraph_chunk("Short paragraph text.", page=1, chunk_id="p1")
        merged = merge_and_split_chunks([h, p], min_tokens=100)
        # Heading must not merge with paragraph
        assert len(merged) == 2, (
            f"Heading-paragraph merge occurred: {len(merged)} chunks instead of 2"
        )
        # Verify the heading chunk retained its element_type
        heading_chunks = [c for c in merged if c.element_type == "heading"]
        assert len(heading_chunks) == 1, "Heading lost its element_type"

    def test_same_page_paragraph_merge_allowed(self):
        c1 = _paragraph_chunk("Short text.", page=1, chunk_id="p1")
        c2 = _paragraph_chunk("Another short text.", page=1, chunk_id="p2")
        # Same page, same section, both paragraph, both under min_tokens → should merge
        merged = merge_and_split_chunks([c1, c2], min_tokens=100)
        assert len(merged) == 1, (
            f"Same-page paragraph merge didn't happen: {len(merged)} chunks"
        )


# -----------------------------------------------------------------------
# Case A: Synthesis query routing
# -----------------------------------------------------------------------

class TestCaseA:
    """Query: "what is the multihead attension machanish" must route to full path
    (centroid routing classifies it as synthesis, not simple lookup)."""

    def test_synthesis_query_routes_full(self):
        from services.retrieval.planner import Planner
        from providers.embedding_provider import embed_text

        planner = Planner()
        query = "what is the multihead attension machanish"
        # In test env, embed_text falls back to deterministic 1024-dim vector
        # matching the centroid dimensions in centroids.py
        embedding = embed_text(query)
        plan = planner.route(query, embedding)

        # Task 3 fix: synthesis_flag now includes "what is" prefix keywords.
        # Keyword-only routing (no embedding) correctly classifies this as synthesis.
        plan_no_embed = planner.route(query)
        assert plan_no_embed.fast_path is False, (
            "Post-fix: keyword routing must classify 'what is X' as synthesis path "
            "(fast_path=False). synthesis_flag now includes 'what is' prefix."
        )
        assert "llm" in plan_no_embed.stages

        # With centroid routing active, also expect synthesis path.
        # (deterministic test embeddings lack semantic meaning so this may vary,
        # but the keyword-based early-return above fires first regardless.)
        assert not plan.fast_path, (
            "synthesis query must not use fast_path with or without embedding"
        )



# -----------------------------------------------------------------------
# Case F: route_query uses unconditional embed
# -----------------------------------------------------------------------

class TestCaseF:
    """Verify embed_text is called for every query (centroid routing requires it)."""

    def test_embed_called_unconditionally(self):
        """The current route_query must call embed_text before planner.route
        so centroid routing works. This is a structural assertion."""
        import inspect
        from services.langgraph_pipeline import route_query

        source = inspect.getsource(route_query)
        # embed_text must be called before planner.route
        embed_pos = source.find("embed_text")
        route_pos = source.find("planner.route")
        assert embed_pos != -1, "embed_text not found in route_query source"
        assert route_pos != -1, "planner.route not found in route_query source"
        assert embed_pos < route_pos, (
            "embed_text must be called before planner.route for centroid routing"
        )

        # query_embedding must be passed to planner.route
        assert "planner.route(state[\"query\"], query_embedding)" in source or \
               "planner.route(state['query'], query_embedding)" in source, (
            "query_embedding not passed to planner.route — centroid routing is broken"
        )

# -----------------------------------------------------------------------
# Case H: Faithfulness fallback (unit-level)
# -----------------------------------------------------------------------

class TestCaseH:
    """response.faithfulness of null must not become 98."""

    def test_null_faithfulness_not_coerced(self):
        """Verify the fix: ?? null instead of || 98.
        This is a source-level structural check since the store is TypeScript."""
        import pathlib
        store_path = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "store" / "useChatStore.ts"
        if not store_path.exists():
            pytest.skip("Frontend source not available")
        content = store_path.read_text()
        assert "response.faithfulness || 98" not in content, (
            "Hardcoded faithfulness fallback still present: || 98"
        )
        assert "response.faithfulness ??" in content, (
            "Nullish coalescing fix not found in useChatStore.ts"
        )


# -----------------------------------------------------------------------
# Task 3 regression: synthesis misrouting fix
# -----------------------------------------------------------------------

class TestTask3SynthesisMisrouting:
    """Verify that synthesis/definitional queries are never sent to fast_path.

    Before the fix:
      "what is the multihead attension machanish"
        → synthesis_flag=False (keyword miss)
        → centroid comparison decides (unreliable for typo queries)
        → may land on fast_path → extractive answer on mechanism query

    After the fix:
      synthesis_flag=True ("what is" prefix present)
      → early-return override at Rule 3
      → fast_path=False guaranteed, LLM synthesis path used
    """

    def _route(self, query: str) -> "Plan":
        from services.retrieval.planner import compute_complexity, Planner
        # Route WITHOUT embedding so centroid path is skipped —
        # tests that the keyword-based early-return fires unconditionally
        planner = Planner()
        return planner.route(query, query_embedding=None)

    def test_mechanism_query_routes_to_synthesis(self):
        """Exact repro query from the directive — typo-laden 'what is X machanish'."""
        plan = self._route("what is the multihead attension machanish")
        assert not plan.fast_path, (
            "Synthesis/mechanism query must NOT use fast_path; "
            f"got fast_path=True, stages={plan.stages}"
        )
        assert "llm" in plan.stages, "Synthesis path must include LLM stage"

    def test_what_is_x_routes_to_synthesis(self):
        """Generic 'what is X' pattern routes to synthesis."""
        plan = self._route("what is attention mechanism")
        assert not plan.fast_path

    def test_how_does_routes_to_synthesis(self):
        """'how does X work' routes to synthesis."""
        plan = self._route("how does multi-head attention work")
        assert not plan.fast_path

    def test_explain_routes_to_synthesis(self):
        """'explain X' was already covered by old keywords, still works."""
        plan = self._route("explain transformer architecture")
        assert not plan.fast_path

    def test_ooc_abstention_query_routes_to_synthesis_not_fast_path(self):
        """'what is capital of france' contains 'what is' — routes synthesis.
        The synthesis path still abstains (fidelity_check rejects OOC),
        so wrong answers are NOT produced.  This test confirms routing only.
        """
        plan = self._route("what is capital of france")
        # Synthesis path — abstention is handled downstream by fidelity_check/LLM
        assert not plan.fast_path, (
            "OOC 'what is' query must use synthesis path (LLM abstention), "
            "not fast_path (extractive scorer may produce wrong confident answer)"
        )
        assert "fidelity_check" in plan.stages
        assert "llm" in plan.stages
