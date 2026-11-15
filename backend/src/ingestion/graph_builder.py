"""Deterministic Knowledge Graph Builder and Grounding Validator.

Extracts canonical entity relationships from:
1. Structured CSV tabular documents using verified schema foreign keys and directional headers.
2. Validates verbatim directional syntax evidence for candidate relationships, rejecting
   unsupported claims, co-occurrence alone, reversed relationships, and negated claims.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import re
from typing import Any, Literal, Sequence
from pydantic import BaseModel, ConfigDict, Field

try:
    from src.schemas.contracts.knowledge_graph import (
        KnowledgeGraphEntity,
        KnowledgeGraphManifest,
        KnowledgeGraphRelationship,
        canonicalize_predicate,
        compute_entity_id,
        compute_relationship_id,
        generate_publication_id,
        normalize_mention,
    )
    from src.schemas.contracts.location import TableLocation
    from src.db.knowledge_graph_repo import KnowledgeGraphRepositoryBase, get_knowledge_graph_repository
except ImportError:
    from schemas.contracts.knowledge_graph import (
        KnowledgeGraphEntity,
        KnowledgeGraphManifest,
        KnowledgeGraphRelationship,
        canonicalize_predicate,
        compute_entity_id,
        compute_relationship_id,
        generate_publication_id,
        normalize_mention,
    )
    from schemas.contracts.location import TableLocation
    from db.knowledge_graph_repo import KnowledgeGraphRepositoryBase, get_knowledge_graph_repository

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class DirectionalSchemaRelation(BaseModel):
    """Schema descriptor defining a verified directional relationship between columns."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_column: str = Field(..., max_length=128)
    predicate: str = Field(..., max_length=128)
    target_column: str = Field(..., max_length=128)
    source_type: Literal["organization", "person", "location", "product", "concept", "table", "other"] = "organization"
    target_type: Literal["organization", "person", "location", "product", "concept", "table", "other"] = "organization"
    disambiguation_column: str | None = Field(default=None, max_length=128)


class TableForeignKey(BaseModel):
    """Schema descriptor defining a verified foreign key relationship across tables."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_column: str = Field(..., max_length=128)
    foreign_table: str = Field(..., max_length=128)
    target_column: str = Field(..., max_length=128)
    predicate: str = Field(default="PARENT_OF", max_length=128)
    source_type: Literal["organization", "person", "location", "product", "concept", "table", "other"] = "organization"
    target_type: Literal["organization", "person", "location", "product", "concept", "table", "other"] = "organization"
    disambiguation_column: str | None = Field(default=None, max_length=128)


_NEGATION_WORDS = {
    "not", "never", "no", "neither", "nor", "none", "cannot", "without",
    "unlikely", "disproved", "failed", "refused", "denied", "denies",
    "didnt", "didn't", "wasnt", "wasn't", "isnt", "isn't", "hasnt", "hasn't",
}


def _clean_text_tokens(text: str) -> list[str]:
    return [t.lower() for t in re.findall(r"\b\w+\b", text)]


def validate_relationship_grounding(
    chunk_text: str,
    source_entity_name: str,
    predicate: str,
    target_entity_name: str,
) -> tuple[bool, str | None]:
    """Validate that candidate relationship is verbatim and directionally grounded in chunk text.
    
    Rejects:
    - Missing entities or predicate.
    - Co-occurrence alone without directional syntax connecting them.
    - Reversed relationships (where source and target roles are swapped relative to syntax).
    - Negated relationships (where text explicitly asserts that relationship does not exist).
    
    Returns:
        (True, None) if valid, or (False, rejection_reason).
    """
    if not chunk_text or not source_entity_name or not target_entity_name or not predicate:
        return False, "missing_input_fields"

    text_lower = chunk_text.lower()
    source_lower = source_entity_name.strip().lower()
    target_lower = target_entity_name.strip().lower()
    pred_lower = predicate.strip().lower().replace("_", " ")

    # 1. Verbatim presence checks
    if source_lower not in text_lower:
        return False, f"source_entity_not_found: '{source_entity_name}'"
    if target_lower not in text_lower:
        return False, f"target_entity_not_found: '{target_entity_name}'"

    # Search for predicate or stem
    pred_tokens = pred_lower.split()
    primary_pred_word = pred_tokens[0] if pred_tokens else pred_lower
    # Allow stem match (e.g. "acquire" for "acquired")
    pred_stem = primary_pred_word[:-1] if len(primary_pred_word) > 5 and primary_pred_word.endswith("d") else primary_pred_word

    if primary_pred_word not in text_lower and pred_stem not in text_lower:
        return False, f"predicate_not_grounded: '{predicate}' (co-occurrence alone rejected)"

    # 2. Sentence-level analysis
    # Break text into sentences to isolate relational context
    sentences = re.split(r"[.!?\n]+", text_lower)
    matched_sentences = [
        s for s in sentences
        if source_lower in s and target_lower in s and (primary_pred_word in s or pred_stem in s)
    ]

    if not matched_sentences:
        # Source, target, and predicate do not co-occur in any single sentence
        return False, "co_occurrence_across_disjoint_sentences"

    # Evaluate directional syntax and negation in matching sentences
    for sentence in matched_sentences:
        s_idx = sentence.find(source_lower)
        t_idx = sentence.find(target_lower)
        p_match = re.search(r"\b" + re.escape(pred_stem) + r"\w*\b", sentence)
        if not p_match:
            continue
        p_idx = p_match.start()
        p_end = p_match.end()

        # Check for negation words in the sentence near the predicate
        words_before_p = sentence[:p_idx].split()[-4:]  # 4 words before predicate
        if any(w in _NEGATION_WORDS for w in words_before_p):
            return False, "negated_relationship"

        # Check passive voice: e.g. "<target> was <predicate> by <source>"
        # e.g. "Beta Tech was acquired by Acme Corp"
        is_passive = bool(re.search(r"\b(?:was|is|been|were)\s+" + re.escape(pred_stem) + r"\w*\s+by\b", sentence))

        if is_passive:
            # In passive voice: Target appears before predicate, Source appears after 'by'
            # e.g. Target (Beta Tech) was acquired by Source (Acme Corp) -> Source is true acquirer.
            if t_idx < p_idx < s_idx:
                # Asserted: (Source, acquired, Target) -> this is valid passive syntax!
                return True, None
            elif s_idx < p_idx < t_idx:
                # "Acme Corp was acquired by Beta Tech" but asserted (Acme Corp, acquired, Beta Tech)
                # This is reversed relative to fact!
                return False, "reversed_relationship"
        else:
            # Active voice: Source should appear before predicate, Target after predicate
            # e.g. Source (Acme Corp) acquired Target (Beta Tech)
            if s_idx < p_idx < t_idx:
                return True, None
            elif t_idx < p_idx < s_idx:
                # Target appears before predicate and Source after in active voice
                # e.g. "Beta Tech acquired Acme Corp" but asserted (Acme Corp, acquired, Beta Tech)
                return False, "reversed_relationship"

    return False, "co_occurrence_without_directional_syntax"


def extract_tabular_relationships(
    workspace_id: str,
    document_id: str,
    document_version: str,
    table_name: str,
    rows: Sequence[dict[str, Any]],
    schema_relations: Sequence[DirectionalSchemaRelation | TableForeignKey] | None = None,
    publication_id: str | None = None,
    source_chunk_ids_by_row: Sequence[Sequence[str] | str] | None = None,
    source_chunks: Sequence[Any] | None = None,
) -> tuple[list[KnowledgeGraphEntity], list[KnowledgeGraphRelationship]]:
    """Extract canonical entity relationships from CSV rows strictly using verified directional schema.
    
    Relationships are derived exclusively from explicit schema relations or foreign keys.
    Row co-occurrence alone or speculative column name similarity produces zero relationships.
    Preserves actual source chunk bindings or derives grounded CSV adapter row chunk locators.
    """
    if not schema_relations or not rows:
        return [], []

    pub_id = publication_id or generate_publication_id()
    now_str = _now_utc()

    entities_map: dict[str, KnowledgeGraphEntity] = {}
    relationships: list[KnowledgeGraphRelationship] = []

    for row_idx, row in enumerate(rows):
        # Resolve source chunk bindings for this row
        row_chunk_ids: list[str] = []
        if source_chunk_ids_by_row and row_idx < len(source_chunk_ids_by_row):
            val = source_chunk_ids_by_row[row_idx]
            if isinstance(val, (list, tuple)):
                row_chunk_ids.extend([str(x) for x in val if x])
            elif val:
                row_chunk_ids.append(str(val))
        elif source_chunks and row_idx < len(source_chunks):
            sc = source_chunks[row_idx]
            sc_id = getattr(sc, "id", None) or (sc.get("id") if isinstance(sc, dict) else None)
            if sc_id:
                row_chunk_ids.append(str(sc_id))
        elif isinstance(row, dict):
            c_keys = ["source_chunk_ids", "source_chunk_id", "chunk_id", "_chunk_id", "chunk_ids"]
            for ck in c_keys:
                if ck in row and row[ck]:
                    if isinstance(row[ck], (list, tuple)):
                        row_chunk_ids.extend([str(x) for x in row[ck] if x])
                    else:
                        row_chunk_ids.append(str(row[ck]))
                    break

        if not row_chunk_ids:
            # Deterministic binding matching csv_adapter row chunks (data row 0 is Row 2)
            row_num = row_idx + 2 if isinstance(row_idx, int) else 2
            row_chunk_ids.append(f"{document_id}:csv:row{row_num}")

        for schema_rel in schema_relations:
            src_col = schema_rel.source_column
            tgt_col = schema_rel.target_column

            if src_col not in row or tgt_col not in row:
                continue

            src_val_raw = row[src_col]
            tgt_val_raw = row[tgt_col]

            if src_val_raw is None or tgt_val_raw is None:
                continue

            src_val = str(src_val_raw).strip()
            tgt_val = str(tgt_val_raw).strip()

            if not src_val or not tgt_val or src_val == tgt_val:
                continue

            # Optional disambiguation token
            disambig_token = None
            disambig_col = getattr(schema_rel, "disambiguation_column", None)
            if disambig_col and disambig_col in row and row[disambig_col] is not None:
                disambig_token = str(row[disambig_col]).strip()

            # Deterministic entity identities
            # Default to document-scoped entity ID to prevent false cross-document homonym joins
            src_ent_id = compute_entity_id(
                workspace_id=workspace_id,
                document_id=document_id,
                entity_type=schema_rel.source_type,
                name=src_val,
                disambiguation_token=disambig_token,
            )
            tgt_ent_id = compute_entity_id(
                workspace_id=workspace_id,
                document_id=document_id,
                entity_type=schema_rel.target_type,
                name=tgt_val,
                disambiguation_token=disambig_token,
            )

            # Record canonical entities
            if src_ent_id not in entities_map:
                entities_map[src_ent_id] = KnowledgeGraphEntity(
                    entity_id=src_ent_id,
                    workspace_id=workspace_id,
                    name=src_val,
                    canonical_name=normalize_mention(src_val),
                    entity_type=schema_rel.source_type,
                    source_document_id=document_id,
                    disambiguation_token=disambig_token,
                    aliases=(),
                )

            if tgt_ent_id not in entities_map:
                entities_map[tgt_ent_id] = KnowledgeGraphEntity(
                    entity_id=tgt_ent_id,
                    workspace_id=workspace_id,
                    name=tgt_val,
                    canonical_name=normalize_mention(tgt_val),
                    entity_type=schema_rel.target_type,
                    source_document_id=document_id,
                    disambiguation_token=disambig_token,
                    aliases=(),
                )

            canonical_pred = canonicalize_predicate(schema_rel.predicate)
            rel_id = compute_relationship_id(
                workspace_id=workspace_id,
                source_entity_id=src_ent_id,
                canonical_predicate=canonical_pred,
                target_entity_id=tgt_ent_id,
                document_id=document_id,
                document_version=document_version,
                publication_id=pub_id,
            )

            loc = TableLocation(
                table_id=table_name,
                row_id=row_idx,
                column_id=f"{src_col}->{tgt_col}",
            )

            verbatim_ev = f"Row {row_idx}: {src_col}={src_val} -> {tgt_col}={tgt_val}"

            rel = KnowledgeGraphRelationship(
                relationship_id=rel_id,
                workspace_id=workspace_id,
                source_entity_id=src_ent_id,
                source_entity_name=src_val,
                predicate=schema_rel.predicate,
                canonical_predicate=canonical_pred,
                target_entity_id=tgt_ent_id,
                target_entity_name=tgt_val,
                directed=True,
                confidence=1.0,  # Deterministic schema foreign key / directional header
                source_document_id=document_id,
                source_document_version=document_version,
                publication_id=pub_id,
                source_chunk_ids=tuple(row_chunk_ids),
                location=loc,
                verbatim_evidence=verbatim_ev,
                relationship_state="unique",
                created_at_utc=now_str,
            )
            relationships.append(rel)

    return list(entities_map.values()), relationships


class KnowledgeGraphBuilder:
    """Builder service extracting and persisting deterministic Knowledge Graph representations."""

    def __init__(self, repository: KnowledgeGraphRepositoryBase | None = None) -> None:
        self.repository = repository or get_knowledge_graph_repository()

    def build_and_persist_tabular_graph(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        table_name: str,
        rows: Sequence[dict[str, Any]],
        schema_relations: Sequence[DirectionalSchemaRelation | TableForeignKey] | None = None,
        base_publication_id: str | None = None,
        source_chunk_ids_by_row: Sequence[Sequence[str] | str] | None = None,
        source_chunks: Sequence[Any] | None = None,
    ) -> KnowledgeGraphManifest:
        """Extract deterministic CSV relationships, stage them, and atomically commit the manifest."""
        pub_id = generate_publication_id()
        now_str = _now_utc()
        content_hash = hashlib.sha256(json.dumps(list(rows), sort_keys=True).encode("utf-8")).hexdigest()

        entities, relationships = extract_tabular_relationships(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
            table_name=table_name,
            rows=rows,
            schema_relations=schema_relations,
            publication_id=pub_id,
            source_chunk_ids_by_row=source_chunk_ids_by_row,
            source_chunks=source_chunks,
        )

        if relationships:
            # 1. Stage records under publication_id
            self.repository.stage_relationships(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                publication_id=pub_id,
                entities=entities,
                relationships=relationships,
            )

            manifest = KnowledgeGraphManifest(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                publication_id=pub_id,
                base_publication_id=base_publication_id,
                status="ready",
                entity_count=len(entities),
                relationship_count=len(relationships),
                ingested_at_utc=now_str,
                content_hash=content_hash,
            )
        else:
            manifest = KnowledgeGraphManifest(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                publication_id=pub_id,
                base_publication_id=base_publication_id,
                status="empty",
                entity_count=0,
                relationship_count=0,
                ingested_at_utc=now_str,
                content_hash=content_hash,
            )

        # 2. Atomic visibility commit
        self.repository.commit_manifest(
            workspace_id=workspace_id,
            manifest=manifest,
            base_publication_id=base_publication_id,
        )

        return manifest
