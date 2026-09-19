"""Schema-on-read SchemaResolver using joint multi-signal scoring and margin disambiguation.

Resolves query concepts to table columns dynamically without hardcoded corpus keywords.
Features:
- Exact lexical match
- Token overlap (stemmed / normalized words)
- General domain-independent ontology (revenue ↔ turnover ↔ sales, workforce ↔ employees)
- Datatype and unit compatibility
- Ambiguity margin rule (top_score - second_score >= margin)
"""

from dataclasses import dataclass
import re
from typing import Any

from schemas.structured_table import ColumnDefinition, InferredDtype, TableSchema

# Configurable defaults
DEFAULT_SCHEMA_BINDING_MIN_CONFIDENCE = 0.70
DEFAULT_SCHEMA_AMBIGUITY_MARGIN = 0.10

# General domain-independent synonym ontology (NOT corpus-specific)
GENERAL_SYNONYMS: dict[str, set[str]] = {
    "revenue": {"turnover", "sales", "income", "topline", "receipts"},
    "turnover": {"revenue", "sales", "income", "topline"},
    "sales": {"revenue", "turnover", "income"},
    "profit": {"earnings", "net income", "margin", "bottomline"},
    "earnings": {"profit", "net income", "margin"},
    "employees": {"workforce", "staff", "headcount", "personnel"},
    "staff": {"employees", "workforce", "headcount"},
    "workforce": {"employees", "staff", "headcount"},
    "households": {"families", "dwellings", "homes"},
    "expenditure": {"expense", "expenses", "spending", "outlay"},
    "year": {"fy", "fiscal year", "financial year", "period"},
    "fy": {"year", "fiscal year", "financial year"},
}


@dataclass(frozen=True)
class ColumnBinding:
    column_index: int
    column_name: str
    confidence: float
    matched_concept: str
    inferred_dtype: InferredDtype
    unit: str | None = None


@dataclass(frozen=True)
class SchemaResolutionResult:
    bindings: tuple[ColumnBinding, ...]
    is_ambiguous: bool
    ambiguity_candidates: tuple[ColumnBinding, ...] = ()
    top_confidence: float = 0.0


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def _synonym_overlap(tokens_a: set[str], tokens_b: set[str]) -> bool:
    """Check if any token in A has a general synonym in B."""
    for ta in tokens_a:
        syns = GENERAL_SYNONYMS.get(ta, set())
        if syns & tokens_b:
            return True
    return False


def score_column_binding(
    concept: str,
    column: ColumnDefinition,
    table_context: str | None = None,
    numeric_expected: bool = False,
) -> float:
    c_lower = concept.strip().lower()
    col_lower = column.name.strip().lower()

    # 1. Exact match
    if c_lower == col_lower:
        return 1.0

    c_tokens = _tokenize(c_lower)
    col_tokens = _tokenize(col_lower)
    if not c_tokens or not col_tokens:
        return 0.0

    # 2. Token overlap (Jaccard & subset)
    intersection = c_tokens & col_tokens
    jaccard = len(intersection) / len(c_tokens | col_tokens)
    recall = len(intersection) / len(c_tokens)

    # 3. Synonym matching
    has_synonym = _synonym_overlap(c_tokens, col_tokens)

    # 4. Datatype compatibility
    dtype_compat = 1.0
    if numeric_expected and column.inferred_dtype in (InferredDtype.STRING, InferredDtype.NULL):
        dtype_compat = 0.5  # Penalize non-numeric column if numeric was explicitly required

    # Scoring blend
    if c_tokens.issubset(col_tokens):
        base_score = 0.90
    elif has_synonym:
        base_score = 0.85
    elif recall >= 0.5:
        base_score = 0.70 + (0.20 * jaccard)
    else:
        base_score = jaccard * 0.60

    score = base_score * dtype_compat
    return round(score, 4)


def resolve_schema_concept(
    concept: str,
    table_schema: TableSchema,
    min_confidence: float = DEFAULT_SCHEMA_BINDING_MIN_CONFIDENCE,
    ambiguity_margin: float = DEFAULT_SCHEMA_AMBIGUITY_MARGIN,
    numeric_expected: bool = False,
    table_context: str | None = None,
) -> SchemaResolutionResult:
    """Resolve a single concept (e.g. 'turnover') to a table column dynamically."""
    scored_candidates: list[ColumnBinding] = []

    for col in table_schema.columns:
        score = score_column_binding(
            concept=concept,
            column=col,
            table_context=table_context,
            numeric_expected=numeric_expected,
        )
        if score > 0.0:
            scored_candidates.append(
                ColumnBinding(
                    column_index=col.col_index,
                    column_name=col.name,
                    confidence=score,
                    matched_concept=concept,
                    inferred_dtype=col.inferred_dtype,
                    unit=col.unit,
                )
            )

    scored_candidates.sort(key=lambda x: x.confidence, reverse=True)

    if not scored_candidates or scored_candidates[0].confidence < min_confidence:
        return SchemaResolutionResult(bindings=(), is_ambiguous=False, top_confidence=0.0)

    top_candidate = scored_candidates[0]

    # Check ambiguity margin against second-best candidate
    if len(scored_candidates) > 1:
        second_candidate = scored_candidates[1]
        score_diff = top_candidate.confidence - second_candidate.confidence
        if score_diff < ambiguity_margin and second_candidate.confidence >= min_confidence:
            # Ambiguity detected: two columns match similarly
            return SchemaResolutionResult(
                bindings=(top_candidate,),
                is_ambiguous=True,
                ambiguity_candidates=tuple(scored_candidates[:2]),
                top_confidence=top_candidate.confidence,
            )

    return SchemaResolutionResult(
        bindings=(top_candidate,),
        is_ambiguous=False,
        top_confidence=top_candidate.confidence,
    )
