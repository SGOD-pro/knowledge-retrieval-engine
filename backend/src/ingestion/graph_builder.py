"""Deterministic Knowledge Graph Builder and Grounding Validator.

Extracts canonical entity relationships from:
1. Structured CSV tabular documents using verified schema foreign keys and directional headers.
2. Validates verbatim directional syntax evidence for candidate relationships, rejecting
   unsupported claims, co-occurrence alone, reversed relationships, and negated claims.
"""

from __future__ import annotations

from collections.abc import Callable
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Literal, Sequence
from pydantic import BaseModel, ConfigDict, Field

from config import settings

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
    from src.schemas.contracts.location import DocumentLocation, TableLocation
    from src.db.knowledge_graph_repo import (
        KnowledgeGraphRepositoryBase,
        disable_client_retries,
        get_knowledge_graph_repository,
    )
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
    from schemas.contracts.location import DocumentLocation, TableLocation
    from db.knowledge_graph_repo import (
        KnowledgeGraphRepositoryBase,
        disable_client_retries,
        get_knowledge_graph_repository,
    )

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
    "wont", "won't", "cant", "can't", "shouldnt", "shouldn't",
}

_UNSUPPORTED_MODALITY_REGEX = re.compile(
    r"\b(?:might|could|would|may|if|whether|rumored|rumor|alleged|allegedly|supposedly|purportedly|"
    r"speculated|speculation|hypothetically|hypothetical|assuming|suppose|supposing|considering|"
    r"proposed|reported\s+that|claimed\s+that|claims\s+that|said\s+that|stated\s+that|"
    r"disputed|unconfirmed|said|says|stated|states|claimed|claims|reported|reports|believed|believes)\b",
    re.IGNORECASE,
)

_CLAUSE_DELIMITER_REGEX = re.compile(
    r";|,\s*(?:while|whereas|although|though|but|however|yet|meanwhile|despite|when|after|before|since|because|where|and)\b|\b(?:while|whereas|although|though|meanwhile)\b",
    re.IGNORECASE,
)

_ALLOWED_PRE_PREDICATE_WORDS = {
    "has", "had", "have", "having", "is", "was", "were", "are", "been", "being",
    "will", "would", "shall", "should", "can", "could", "may", "might", "must",
    "did", "does", "do",
    "recently", "officially", "formally", "successfully", "directly", "indirectly",
    "mutually", "jointly", "also", "already", "now", "then", "soon", "currently",
    "previously", "ultimately", "finally", "subsequently", "unanimously", "proudly",
    "further", "actively", "to",
}

_ALLOWED_ACTIVE_INTERVENING_WORDS = {
    "the", "a", "an", "its", "their", "both", "all", "several", "each",
    "with", "in", "to", "into", "for", "of", "on", "from", "over", "by", "as", "at",
    "subsidiary", "majority", "stake", "shares", "equity", "assets", "firm", "company",
    "technologies", "corp", "inc", "ltd", "new", "key", "strategic", "global", "local",
    "primary", "recently", "successfully", "fully", "directly", "indirectly", "partner",
    "division", "business", "unit", "interest", "entire",
}

_ALLOWED_PASSIVE_AUXILIARIES = {
    "was", "were", "is", "are", "been", "being", "has", "have", "had",
    "will", "would", "shall", "should", "can", "could", "may", "might", "must",
    "recently", "officially", "formally", "successfully", "directly", "indirectly",
    "jointly", "also", "already", "now", "then", "soon", "currently", "previously",
    "ultimately", "finally", "subsequently", "unanimously", "proudly", "to",
}

_ALLOWED_PASSIVE_BY_WORDS = {
    "by", "the", "its", "their", "a", "an", "new", "strategic", "global",
    "local", "subsidiary", "firm", "company", "division", "partner", "wholly",
    "owned", "directly", "indirectly", "jointly", "majority", "stake",
}


def _clean_text_tokens(text: str) -> list[str]:
    return [t.lower() for t in re.findall(r"\b\w+\b", text)]


def _find_enclosing_sentence(text: str, start_idx: int, end_idx: int) -> str:
    """Locate the enclosing sentence surrounding a verbatim evidence span in source text."""
    prev_text = text[:start_idx]
    last_term = -1
    for m in re.finditer(r"[.!?\n]", prev_text):
        last_term = m.end()
    sent_start = last_term if last_term != -1 else 0

    after_text = text[end_idx:]
    term_match = re.search(r"[.!?\n]", after_text)
    if term_match:
        sent_end = end_idx + term_match.end()
    else:
        sent_end = len(text)

    return text[sent_start:sent_end].strip()


def validate_chunk_provenance(
    chunk: Any,
    workspace_id: str,
    document_id: str,
    document_version: str,
) -> tuple[bool, str | None]:
    """Validate that chunk has exact, authoritative, and non-conflicting source provenance.

    Rejects:
    - Missing chunk ID or unversioned chunks.
    - Conflicting document_id, workspace_id, or version fields across chunk attributes and metadata.
    - Stale metadata versions not matching the requested document_version.
    - Workspace or document boundaries not matching the request.
    """
    cid = str(getattr(chunk, "id", None) or getattr(chunk, "chunk_id", None) or "").strip()
    if not cid:
        return False, "missing_chunk_id"

    meta = getattr(chunk, "metadata", None)
    if not isinstance(meta, dict):
        meta = {}

    # 1. Document ID validation
    ch_doc = getattr(chunk, "document_id", None)
    ch_doc_str = str(ch_doc).strip() if (ch_doc is not None and str(ch_doc).strip()) else None
    meta_doc = meta.get("document_id")
    meta_doc_str = str(meta_doc).strip() if (meta_doc is not None and str(meta_doc).strip()) else None

    if ch_doc_str and meta_doc_str and ch_doc_str != meta_doc_str:
        return False, f"conflicting_document_id: attribute '{ch_doc_str}' != metadata '{meta_doc_str}'"

    resolved_doc = ch_doc_str or meta_doc_str
    if not resolved_doc:
        return False, "missing_document_id"
    if resolved_doc != document_id.strip():
        return False, f"mismatched_document_id: resolved '{resolved_doc}' != expected '{document_id}'"

    # 2. Workspace ID validation
    ch_ws = getattr(chunk, "workspace_id", None)
    ch_ws_str = str(ch_ws).strip() if (ch_ws is not None and str(ch_ws).strip()) else None
    meta_ws = meta.get("workspace_id")
    meta_ws_str = str(meta_ws).strip() if (meta_ws is not None and str(meta_ws).strip()) else None

    if ch_ws_str and meta_ws_str and ch_ws_str != meta_ws_str:
        return False, f"conflicting_workspace_id: attribute '{ch_ws_str}' != metadata '{meta_ws_str}'"

    resolved_ws = ch_ws_str or meta_ws_str
    if not resolved_ws:
        return False, "missing_workspace_id"
    if resolved_ws != workspace_id.strip():
        return False, f"mismatched_workspace_id: resolved '{resolved_ws}' != expected '{workspace_id}'"

    # 3. Document Version validation
    ch_ver = getattr(chunk, "document_version", None)
    ch_ver_str = str(ch_ver).strip() if (ch_ver is not None and str(ch_ver).strip()) else None
    meta_ver = meta.get("document_version")
    meta_ver_str = str(meta_ver).strip() if (meta_ver is not None and str(meta_ver).strip()) else None
    meta_v = meta.get("version")
    meta_v_str = str(meta_v).strip() if (meta_v is not None and str(meta_v).strip()) else None

    # Check for conflicts between metadata version and metadata document_version
    if meta_ver_str and meta_v_str and meta_ver_str != meta_v_str:
        return False, f"conflicting_metadata_versions: document_version '{meta_ver_str}' != version '{meta_v_str}'"

    # Check for conflicts between attribute and metadata
    if ch_ver_str and meta_ver_str and ch_ver_str != meta_ver_str:
        return False, f"conflicting_version_bindings: attribute '{ch_ver_str}' != metadata document_version '{meta_ver_str}'"
    if ch_ver_str and meta_v_str and ch_ver_str != meta_v_str:
        return False, f"conflicting_version_bindings: attribute '{ch_ver_str}' != metadata version '{meta_v_str}'"

    resolved_ver = ch_ver_str or meta_ver_str or meta_v_str
    if not resolved_ver:
        return False, "unverifiable_version_binding: chunk has no document_version or version metadata"

    if resolved_ver != document_version.strip():
        return False, f"stale_document_version: resolved '{resolved_ver}' != expected '{document_version}'"

    return True, None


def validate_relationship_grounding(
    chunk_text: str,
    source_entity_name: str,
    predicate: str,
    target_entity_name: str,
    evidence_span: str | None = None,
) -> tuple[bool, str | None]:
    """Validate that candidate relationship is verbatim and directionally grounded in chunk text.
    
    Rejects:
    - Missing entities or predicate.
    - Co-occurrence alone without directional syntax connecting them.
    - Reversed relationships (where source and target roles are swapped relative to syntax).
    - Negated relationships (where text explicitly asserts that relationship does not exist).
    - Hypothetical, conditional, or reported claims.
    - Entity pairs separated across unrelated clauses within a sentence.
    - Unsupported syntactic bindings where intervening nouns/clauses decouple predicate and target.
    
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
    pred_stem = primary_pred_word
    if primary_pred_word.endswith("ed") and len(primary_pred_word) > 4:
        cand = primary_pred_word[:-2]
        if cand in text_lower or (cand + "e") in text_lower:
            pred_stem = cand if cand in text_lower else (cand + "e")
        else:
            pred_stem = primary_pred_word[:-1]
    elif primary_pred_word.endswith("d") and len(primary_pred_word) > 4:
        pred_stem = primary_pred_word[:-1]
    elif primary_pred_word.endswith("ing") and len(primary_pred_word) > 5:
        pred_stem = primary_pred_word[:-3]

    if primary_pred_word not in text_lower and pred_stem not in text_lower:
        return False, f"predicate_not_grounded: '{predicate}' (co-occurrence alone rejected)"

    if evidence_span:
        ev_lower = evidence_span.strip().lower()
        if source_lower not in ev_lower or target_lower not in ev_lower:
            return False, "evidence_span_missing_entities"
        if primary_pred_word not in ev_lower and pred_stem not in ev_lower:
            return False, "evidence_span_missing_predicate"

    # 2. Sentence-level analysis
    sentences = re.split(r"[.!?\n]+", text_lower)
    matched_sentences = [
        s for s in sentences
        if source_lower in s and target_lower in s and (primary_pred_word in s or pred_stem in s)
    ]

    if not matched_sentences:
        return False, "co_occurrence_across_disjoint_sentences"

    for sentence in matched_sentences:
        tokens = _clean_text_tokens(sentence)
        if any(w in _NEGATION_WORDS for w in tokens):
            return False, "negated_relationship"

        if _UNSUPPORTED_MODALITY_REGEX.search(sentence):
            return False, "hypothetical_or_unsupported_claim"

        # Split sentence into clauses to prevent cross-clause false relationships
        clauses = _CLAUSE_DELIMITER_REGEX.split(sentence)
        matching_clauses = [
            c for c in clauses
            if source_lower in c and target_lower in c and (primary_pred_word in c or pred_stem in c)
        ]
        if not matching_clauses:
            return False, "unrelated_clauses"

        for clause in matching_clauses:
            s_matches = [m.start() for m in re.finditer(r"\b" + re.escape(source_lower) + r"\b", clause)]
            t_matches = [m.start() for m in re.finditer(r"\b" + re.escape(target_lower) + r"\b", clause)]
            if not s_matches or not t_matches:
                continue

            p_matches = list(re.finditer(r"\b" + re.escape(pred_stem) + r"\w*\b", clause))
            if not p_matches:
                continue

            is_passive = bool(re.search(r"\b(?:was|is|been|were|are)\b(?:\s+\w+){0,2}\s+" + re.escape(pred_stem) + r"\w*\s+by\b", clause))

            has_reversed = False
            for p_match in p_matches:
                p_idx = p_match.start()
                p_end = p_match.end()

                for s_idx in s_matches:
                    s_end = s_idx + len(source_lower)
                    for t_idx in t_matches:
                        t_end = t_idx + len(target_lower)
                        if is_passive:
                            # Passive voice: Target was <pred> by Source
                            if t_idx < p_idx < s_idx:
                                # Validate passive auxiliaries between target and predicate
                                aux_span = clause[t_end:p_idx].strip()
                                aux_tokens = _clean_text_tokens(aux_span)
                                if not all(tok in _ALLOWED_PASSIVE_AUXILIARIES or tok.endswith("ly") or tok.isdigit() for tok in aux_tokens):
                                    continue
                                # Validate passive by-phrase between predicate and source
                                by_span = clause[p_end:s_idx].strip()
                                by_tokens = _clean_text_tokens(by_span)
                                if "by" not in by_tokens:
                                    continue
                                if not all(tok in _ALLOWED_PASSIVE_BY_WORDS or tok.endswith("ly") or tok.isdigit() for tok in by_tokens):
                                    continue
                                return True, None
                            elif s_idx < p_idx < t_idx:
                                has_reversed = True
                        else:
                            # Active voice: Source <pred> Target
                            if s_idx < p_idx < t_idx:
                                # Validate actor-predicate binding: tokens between source and predicate
                                pre_span = clause[s_end:p_idx].strip()
                                pre_tokens = _clean_text_tokens(pre_span)
                                if not all(tok in _ALLOWED_PRE_PREDICATE_WORDS or tok.endswith("ly") or tok.isdigit() for tok in pre_tokens):
                                    continue
                                if len(pre_tokens) > 5:
                                    continue

                                intervening_span = clause[p_end:t_idx].strip()
                                inter_tokens = _clean_text_tokens(intervening_span)
                                if all(tok in _ALLOWED_ACTIVE_INTERVENING_WORDS or tok.isdigit() for tok in inter_tokens) and len(inter_tokens) <= 6:
                                    return True, None
                                else:
                                    return False, "unsupported_syntactic_binding"
                            elif t_idx < p_idx < s_idx:
                                has_reversed = True

            if has_reversed:
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


class KnowledgeGraphProviderConcurrencyController:
    """Global concurrency controller for Knowledge Graph Bedrock Converse extraction calls.

    Enforces:
    - Bounded concurrency across documents (default max 2 concurrent Bedrock calls).
    - Bounded admission queue with immediate load shedding (default max 4 queued calls).
    - Capacity retention: when a caller's deadline expires (caller timeout),
      the underlying worker slot in the semaphore and thread pool is retained until the
      underlying provider call finishes, preventing caller timeouts from flooding Bedrock.
    """

    def __init__(
        self,
        max_concurrency: int = 2,
        max_queue: int = 4,
    ) -> None:
        self.max_concurrency = max_concurrency
        self.max_queue = max_queue
        self._active_workers = 0
        self._queued_callers = 0
        self._semaphore = threading.Semaphore(self.max_concurrency)
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_concurrency,
            thread_name_prefix="kg_provider_worker",
        )

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active_workers

    @property
    def queued_count(self) -> int:
        with self._lock:
            return self._queued_callers

    def run_provider_call(
        self,
        func: Callable[..., Any],
        *args: Any,
        timeout_s: float = 10.0,
        **kwargs: Any,
    ) -> tuple[Any, str]:
        """Execute blocking provider call with capacity retention and caller timeout enforcement.

        Returns (result, status) where status is:
        - "completed": underlying call returned successfully
        - "caller_timeout": caller deadline expired while waiting for slot or completion
        - "admission_rejected": queue is saturated
        - "call_failed": underlying call raised an exception
        """
        with self._lock:
            if self._queued_callers >= self.max_queue:
                logger.warning(
                    "kg.provider_admission_rejected queued=%d max=%d",
                    self._queued_callers,
                    self.max_queue,
                )
                return None, "admission_rejected"
            self._queued_callers += 1

        t0 = time.perf_counter()
        acquired = self._semaphore.acquire(timeout=timeout_s)
        if not acquired:
            with self._lock:
                self._queued_callers -= 1
            logger.warning("kg.provider_timeout stage=queue_admission timeout_s=%.2f", timeout_s)
            return None, "caller_timeout"

        with self._lock:
            self._queued_callers -= 1
            self._active_workers += 1

        elapsed = time.perf_counter() - t0
        remaining_s = max(0.001, timeout_s - elapsed)

        outcome: dict[str, Any] = {"result": None, "error": None, "status": "running"}
        call_done = threading.Event()

        def _worker_task() -> None:
            try:
                outcome["result"] = func(*args, **kwargs)
                outcome["status"] = "completed"
            except Exception as exc:
                outcome["error"] = exc
                outcome["status"] = "call_failed"
            finally:
                with self._lock:
                    self._active_workers = max(0, self._active_workers - 1)
                self._semaphore.release()
                call_done.set()

        self._executor.submit(_worker_task)

        finished = call_done.wait(timeout=remaining_s)
        if finished:
            if outcome["error"] is not None:
                return outcome["error"], outcome["status"]
            return outcome["result"], outcome["status"]

        # Caller timeout: capacity in self._semaphore remains retained
        # until _worker_task actually finishes in the background!
        logger.warning("kg.provider_timeout stage=call_execution timeout_s=%.2f", timeout_s)
        return None, "caller_timeout"


_KG_PROVIDER_CONTROLLER: KnowledgeGraphProviderConcurrencyController | None = None
_KG_GLOBAL_LOCK = threading.Lock()


def get_kg_provider_controller() -> KnowledgeGraphProviderConcurrencyController:
    """Return the global shared Knowledge Graph provider concurrency controller."""
    global _KG_PROVIDER_CONTROLLER
    with _KG_GLOBAL_LOCK:
        if _KG_PROVIDER_CONTROLLER is None:
            max_c = int(getattr(settings, "KG_PROVIDER_MAX_CONCURRENCY", 2))
            max_q = int(getattr(settings, "KG_PROVIDER_MAX_QUEUE", 4))
            _KG_PROVIDER_CONTROLLER = KnowledgeGraphProviderConcurrencyController(
                max_concurrency=max_c,
                max_queue=max_q,
            )
        return _KG_PROVIDER_CONTROLLER


def reset_kg_provider_controller() -> None:
    """Reset the global KG provider concurrency controller for test isolation."""
    global _KG_PROVIDER_CONTROLLER
    with _KG_GLOBAL_LOCK:
        if _KG_PROVIDER_CONTROLLER is not None:
            try:
                _KG_PROVIDER_CONTROLLER._executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            _KG_PROVIDER_CONTROLLER = None


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

    def _fail_closed_manifest(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        base_publication_id: str | None,
        existing_manifest: KnowledgeGraphManifest | None,
        content_hash: str,
    ) -> KnowledgeGraphManifest:
        if existing_manifest is not None and existing_manifest.status == "ready":
            return existing_manifest
        manifest = KnowledgeGraphManifest(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
            publication_id=existing_manifest.publication_id if existing_manifest else generate_publication_id(),
            base_publication_id=base_publication_id,
            status="failed",
            entity_count=0,
            relationship_count=0,
            ingested_at_utc=_now_utc(),
            content_hash=content_hash,
        )
        try:
            self.repository.commit_manifest(workspace_id, manifest, base_publication_id=base_publication_id)
        except Exception:
            pass
        return manifest

    def build_and_persist_pdf_graph(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        chunks: Sequence[Any],
        client: Any | None = None,
        worker_id: str | None = None,
        base_publication_id: str | None = None,
        max_calls: int = 2,
        max_input_tokens: int = 4000,
        max_output_tokens: int = 800,
        max_total_tokens: int = 5600,
        lease_duration_s: float = 15.0,
        call_deadline_s: float = 10.0,
        authoritative_disambiguation: dict[str, str] | None = None,
    ) -> KnowledgeGraphManifest:
        """Extract canonical entity relationships from PDF text chunks using Bedrock converse and durable budgeting."""
        if not worker_id or not worker_id.strip():
            worker_id = getattr(self, "worker_id", None) or f"kg_worker_{os.getpid()}"
        worker_id = worker_id.strip()

        idempotency_key = hashlib.sha256(f"{workspace_id}:{document_id}:{document_version}:kg".encode("utf-8")).hexdigest()
        existing_man = self.repository.get_manifest(workspace_id, document_id, document_version, consistent_read=True)
        if existing_man is not None and existing_man.status in ("ready", "empty") and existing_man.content_hash == idempotency_key:
            return existing_man

        ledger = self.repository.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
        if ledger is not None:
            if ledger.status in ("exhausted", "failed", "completed") or ledger.reserved_calls >= max_calls:
                return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

        if not chunks:
            pub_id = generate_publication_id()
            manifest = KnowledgeGraphManifest(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                publication_id=pub_id,
                base_publication_id=base_publication_id,
                status="empty",
                entity_count=0,
                relationship_count=0,
                ingested_at_utc=_now_utc(),
                content_hash=idempotency_key,
            )
            self.repository.commit_manifest(workspace_id, manifest, base_publication_id=base_publication_id)
            return manifest

        user_content_blocks = []
        remaining_bytes = 8000
        for ch in chunks:
            is_prov_valid, prov_reason = validate_chunk_provenance(
                chunk=ch,
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
            )
            if not is_prov_valid:
                logger.warning("Chunk %s excluded from prompt due to provenance failure: %s", getattr(ch, "id", None), prov_reason)
                continue

            cid = str(getattr(ch, "id", None) or getattr(ch, "chunk_id", None) or "")
            text = str(getattr(ch, "text", None) or getattr(ch, "content", None) or "")
            page_val = getattr(ch, "page_number", None)
            if page_val is None:
                page_val = getattr(ch, "page", None)
            if page_val is None:
                page_val = (getattr(ch, "metadata", {}) or {}).get("page", 1)
            try:
                page = int(page_val)
            except (ValueError, TypeError):
                page = 1
            text_bytes = text.encode("utf-8")
            if len(text_bytes) > 2000:
                text = text_bytes[:2000].decode("utf-8", errors="ignore")
            block = f'<chunk id="{cid}" page="{page}">\n{text}\n</chunk>'
            block_bytes = len(block.encode("utf-8"))
            if block_bytes > remaining_bytes:
                break
            user_content_blocks.append(block)
            remaining_bytes -= (block_bytes + 2)

        if not user_content_blocks:
            pub_id = generate_publication_id()
            manifest = KnowledgeGraphManifest(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                publication_id=pub_id,
                base_publication_id=base_publication_id,
                status="empty",
                entity_count=0,
                relationship_count=0,
                ingested_at_utc=_now_utc(),
                content_hash=idempotency_key,
            )
            self.repository.commit_manifest(workspace_id, manifest, base_publication_id=base_publication_id)
            return manifest

        user_prompt = "\n\n".join(user_content_blocks)
        messages = [{"role": "user", "content": [{"text": user_prompt}]}]
        system = [{"text": BEDROCK_GRAPH_SYSTEM_PROMPT}]
        model_id = getattr(settings, "GRAPH_BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")

        if client is not None:
            disable_client_retries(client)
            if not hasattr(client, "count_tokens"):
                logger.error("Bedrock client missing mandatory count_tokens capability (AC-5)")
                return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)
            try:
                count_resp = client.count_tokens(
                    modelId=model_id,
                    input={"converse": {"messages": messages, "system": system}},
                )
                raw_tokens = count_resp.get("inputTokens", 0) if isinstance(count_resp, dict) else None
                if not isinstance(raw_tokens, int) or raw_tokens <= 0:
                    logger.error("Bedrock count_tokens returned non-positive tokens: %s", count_resp)
                    return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)
                estimated_prompt_tokens = raw_tokens
            except Exception as count_err:
                logger.error("Bedrock count_tokens failed: %s", count_err)
                return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)
        else:
            estimated_prompt_tokens = max(1, len(user_prompt) // 4 + len(BEDROCK_GRAPH_SYSTEM_PROMPT) // 4)

        if estimated_prompt_tokens > 2000:
            logger.error("Estimated prompt tokens %d exceed 2000 per-call limit", estimated_prompt_tokens)
            return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

        res = self.repository.reserve_extraction_budget(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
            worker_id=worker_id,
            estimated_prompt_tokens=estimated_prompt_tokens,
            max_output_tokens=max_output_tokens,
            max_calls=max_calls,
            max_total_tokens=max_total_tokens,
            lease_duration_s=lease_duration_s,
            max_input_tokens=max_input_tokens,
            max_output_ceiling=1600,
        )
        if not res.granted:
            logger.warning("Budget reservation denied: %s", res.reason)
            return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)
        fence_token = res.fence_token

        output_text = ""
        usage: dict[str, int] = {}
        dispatch_success = False

        if client is None:
            output_text = "{}"
            usage = {"inputTokens": estimated_prompt_tokens, "outputTokens": 0}
            dispatch_success = True
        else:
            controller = get_kg_provider_controller()
            max_attempts = 2
            start_time = time.perf_counter()

            for attempt in range(max_attempts):
                rem_deadline = max(0.001, call_deadline_s - (time.perf_counter() - start_time))
                if rem_deadline <= 0.001 and attempt > 0:
                    logger.warning("Provider call deadline expired before retry attempt %d", attempt)
                    self.repository.fail_extraction_budget(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        worker_id=worker_id,
                        fence_token=fence_token,
                        error_reason="caller_timeout",
                        prompt_tokens_spent=estimated_prompt_tokens,
                    )
                    return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

                if attempt > 0:
                    retry_res = self.repository.reserve_extraction_budget(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        worker_id=worker_id,
                        estimated_prompt_tokens=estimated_prompt_tokens,
                        max_output_tokens=max_output_tokens,
                        max_calls=max_calls,
                        max_total_tokens=max_total_tokens,
                        lease_duration_s=lease_duration_s,
                        max_input_tokens=max_input_tokens,
                        max_output_ceiling=1600,
                    )
                    if not retry_res.granted:
                        logger.warning("Retry budget reservation denied: %s", retry_res.reason)
                        self.repository.fail_extraction_budget(
                            workspace_id=workspace_id,
                            document_id=document_id,
                            document_version=document_version,
                            worker_id=worker_id,
                            fence_token=fence_token,
                            error_reason="retry_reservation_denied",
                            prompt_tokens_spent=estimated_prompt_tokens,
                        )
                        return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)
                    fence_token = retry_res.fence_token

                res_obj, status = controller.run_provider_call(
                    client.converse,
                    modelId=model_id,
                    messages=messages,
                    system=system,
                    inferenceConfig={"temperature": 0.0, "maxTokens": max_output_tokens},
                    timeout_s=rem_deadline,
                )

                if status == "caller_timeout":
                    logger.warning("Bedrock converse caller timeout exceeded call_deadline_s=%.2f", call_deadline_s)
                    self.repository.fail_extraction_budget(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        worker_id=worker_id,
                        fence_token=fence_token,
                        error_reason="caller_timeout",
                        prompt_tokens_spent=estimated_prompt_tokens,
                    )
                    return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

                if status == "admission_rejected":
                    logger.warning("Bedrock converse admission rejected due to queue saturation")
                    self.repository.fail_extraction_budget(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        worker_id=worker_id,
                        fence_token=fence_token,
                        error_reason="admission_rejected",
                        prompt_tokens_spent=0,
                    )
                    return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

                if status == "call_failed":
                    exc = res_obj
                    err_str = str(exc)
                    if ("ThrottlingException" in err_str or "429" in err_str) and attempt < max_attempts - 1:
                        logger.warning("Bedrock throttled, retrying once...")
                        time.sleep(1.0)
                        continue
                    else:
                        logger.error("Bedrock converse failed: %s", exc)
                        self.repository.fail_extraction_budget(
                            workspace_id=workspace_id,
                            document_id=document_id,
                            document_version=document_version,
                            worker_id=worker_id,
                            fence_token=fence_token,
                            error_reason=str(exc),
                            prompt_tokens_spent=estimated_prompt_tokens,
                        )
                        return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

                resp = res_obj
                raw_usage = resp.get("usage") if isinstance(resp, dict) else None
                if not isinstance(raw_usage, dict):
                    logger.error("Bedrock converse response missing or invalid usage dict: %r", raw_usage)
                    self.repository.fail_extraction_budget(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        worker_id=worker_id,
                        fence_token=fence_token,
                        error_reason="missing_provider_usage",
                        prompt_tokens_spent=estimated_prompt_tokens,
                    )
                    return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

                in_tok = raw_usage.get("inputTokens")
                out_tok = raw_usage.get("outputTokens")

                def _is_valid_token_int(val: Any) -> bool:
                    return not isinstance(val, bool) and isinstance(val, int)

                if not _is_valid_token_int(in_tok) or in_tok <= 0:
                    logger.error("Bedrock converse returned invalid inputTokens: %r", in_tok)
                    self.repository.fail_extraction_budget(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        worker_id=worker_id,
                        fence_token=fence_token,
                        error_reason="invalid_provider_usage",
                        prompt_tokens_spent=estimated_prompt_tokens,
                    )
                    return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

                if not _is_valid_token_int(out_tok) or out_tok < 0:
                    logger.error("Bedrock converse returned invalid outputTokens: %r", out_tok)
                    self.repository.fail_extraction_budget(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        worker_id=worker_id,
                        fence_token=fence_token,
                        error_reason="invalid_provider_usage",
                        prompt_tokens_spent=in_tok,
                    )
                    return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

                # 1. Per-call output token limit (e.g. reject outputTokens=801 when maxTokens=800)
                if out_tok > max_output_tokens:
                    logger.error("Bedrock converse outputTokens %d exceeds per-call limit %d", out_tok, max_output_tokens)
                    self.repository.fail_extraction_budget(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        worker_id=worker_id,
                        fence_token=fence_token,
                        error_reason="output_tokens_exceeded_limit",
                        prompt_tokens_spent=in_tok,
                        output_tokens_spent=out_tok,
                    )
                    return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

                # 2. Per-call input token limit: cannot exceed reserved allowance or configured ceiling (2000)
                if in_tok > estimated_prompt_tokens or in_tok > 2000:
                    logger.error("Bedrock converse inputTokens %d exceeds reservation %d or cap 2000", in_tok, estimated_prompt_tokens)
                    self.repository.fail_extraction_budget(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        worker_id=worker_id,
                        fence_token=fence_token,
                        error_reason="input_tokens_exceeded_reservation",
                        prompt_tokens_spent=in_tok,
                        output_tokens_spent=out_tok,
                    )
                    return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

                # 3. Aggregate document token limits across attempts and retries
                curr_ledger = self.repository.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
                prior_spt = curr_ledger.spent_prompt_tokens if curr_ledger else 0
                prior_sot = curr_ledger.spent_output_tokens if curr_ledger else 0
                prior_tot = curr_ledger.spent_tokens if curr_ledger else 0

                if (
                    prior_spt + in_tok > max_input_tokens
                    or prior_sot + out_tok > 1600
                    or prior_tot + in_tok + out_tok > max_total_tokens
                ):
                    logger.error(
                        "Aggregate tokens overrun document budget: prompt=%d/%d, output=%d/1600, total=%d/%d",
                        prior_spt + in_tok,
                        max_input_tokens,
                        prior_sot + out_tok,
                        prior_tot + in_tok + out_tok,
                        max_total_tokens,
                    )
                    self.repository.fail_extraction_budget(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        worker_id=worker_id,
                        fence_token=fence_token,
                        error_reason="aggregate_budget_overrun",
                        prompt_tokens_spent=in_tok,
                        output_tokens_spent=out_tok,
                    )
                    return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

                output_text = resp.get("output", {}).get("message", {}).get("content", [{}])[0].get("text", "")
                usage = {"inputTokens": in_tok, "outputTokens": out_tok}
                dispatch_success = True
                break

        if not dispatch_success:
            return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

        actual_in = usage["inputTokens"]
        actual_out = usage["outputTokens"]
        try:
            rec_res = self.repository.reconcile_extraction_budget(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                worker_id=worker_id,
                fence_token=fence_token,
                actual_prompt_tokens=actual_in,
                actual_output_tokens=actual_out,
            )
        except Exception as e:
            logger.error("Failed to reconcile extraction budget: %s", e)
            return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

        if not rec_res.reconciled or rec_res.fenced_out:
            logger.warning("Reconciliation denied: reason=%s fenced=%s", rec_res.reason, rec_res.fenced_out)
            return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

        pub_id = generate_publication_id()
        entities, relationships = extract_pdf_relationships(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
            chunks=chunks,
            model_output_json=output_text,
            publication_id=pub_id,
            authoritative_disambiguation=authoritative_disambiguation,
        )

        if relationships:
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
                ingested_at_utc=_now_utc(),
                content_hash=idempotency_key,
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
                ingested_at_utc=_now_utc(),
                content_hash=idempotency_key,
            )

        try:
            self.repository.commit_manifest(
                workspace_id=workspace_id,
                manifest=manifest,
                base_publication_id=base_publication_id,
                fence_token=fence_token,
                worker_id=worker_id,
            )
        except Exception as commit_err:
            logger.warning("Failed to commit manifest: %s", commit_err)
            return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

        return manifest


def _clean_json_payload(raw_text: str) -> str:
    cleaned = raw_text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


BEDROCK_GRAPH_SYSTEM_PROMPT = (
    "You are a strict, factual Knowledge Graph extractor.\n"
    "Extract only canonical directed entity relationships that are explicitly and verbatim stated in the provided chunks.\n"
    "Rules:\n"
    "1. Direction matters: source_entity is the subject/actor, target_entity is the object (e.g. 'Acme acquired Beta' -> source: Acme, target: Beta).\n"
    "2. Do not infer relationships from co-occurrence alone. Entities merely mentioned together without directional syntax connecting them must be omitted.\n"
    "3. If text contains negation (e.g. 'did not acquire', 'never partnered', 'failed to merge'), do NOT extract any relationship.\n"
    "4. If text describes reversed roles (e.g. 'Beta was bought by Acme'), ensure source is the buyer (Acme) and target is the acquired (Beta).\n"
    "5. Entities are document-scoped by default unless backed by an explicit disambiguation token (registration ID, ticker).\n"
    "6. Output MUST be valid JSON only, with no surrounding commentary or markdown, matching this schema:\n"
    "{\n"
    '  "entities": [{"name": "...", "entity_type": "organization"|"person"|"location"|"product"|"concept"|"other", "disambiguation_token": null}],\n'
    '  "relationships": [{"source_entity": "...", "predicate": "...", "target_entity": "...", "chunk_id": "...", "verbatim_evidence": "..."}]\n'
    "}"
)


def extract_pdf_relationships(
    workspace_id: str,
    document_id: str,
    document_version: str,
    chunks: Sequence[Any],
    model_output_json: str,
    publication_id: str,
    authoritative_disambiguation: dict[str, str] | None = None,
) -> tuple[list[KnowledgeGraphEntity], list[KnowledgeGraphRelationship]]:
    """Strictly parse model output JSON and ground candidate entities and relationships against chunk evidence."""
    try:
        cleaned = _clean_json_payload(model_output_json)
        data = json.loads(cleaned)
    except Exception as exc:
        logger.warning("Failed to parse model output JSON: %s", exc)
        return [], []

    if not isinstance(data, dict):
        return [], []

    raw_entities = data.get("entities", [])
    raw_relationships = data.get("relationships", [])
    if not isinstance(raw_relationships, list):
        return [], []

    chunks_map: dict[str, tuple[Any, str, int]] = {}
    for ch in chunks:
        cid = str(getattr(ch, "id", None) or getattr(ch, "chunk_id", None) or "")
        if not cid:
            continue
        # Strict provenance: validate workspace_id, document_id, and document_version
        is_prov_valid, prov_reason = validate_chunk_provenance(
            chunk=ch,
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
        )
        if not is_prov_valid:
            logger.warning("Chunk %s rejected during extraction: %s", cid, prov_reason)
            continue

        text = str(getattr(ch, "text", None) or getattr(ch, "content", None) or "")
        page_val = getattr(ch, "page_number", None)
        if page_val is None:
            page_val = getattr(ch, "page", None)
        if page_val is None:
            page_val = (getattr(ch, "metadata", {}) or {}).get("page", 1)
        try:
            page = int(page_val)
        except (ValueError, TypeError):
            page = 1
        chunks_map[cid] = (ch, text, page)

    auth_map: dict[str, str] = {}
    if authoritative_disambiguation:
        for k, v in authoritative_disambiguation.items():
            if k and v:
                auth_map[normalize_mention(str(k))] = str(v).strip()

    entity_info: dict[str, tuple[str, str | None]] = {}
    if isinstance(raw_entities, list):
        for ent in raw_entities:
            if isinstance(ent, dict):
                ename = str(ent.get("name", "")).strip()
                etype = str(ent.get("entity_type", "concept")).strip().lower()
                disambig_raw = ent.get("disambiguation_token")
                # Never trust model-supplied disambiguation token without authoritative verification!
                verified_disambig: str | None = None
                if ename:
                    norm = normalize_mention(ename)
                    if disambig_raw and auth_map:
                        auth_tok = auth_map.get(norm)
                        if auth_tok and auth_tok == str(disambig_raw).strip():
                            verified_disambig = auth_tok
                    entity_info[norm] = (etype, verified_disambig)

    extracted_entities: dict[str, KnowledgeGraphEntity] = {}
    extracted_relationships: list[KnowledgeGraphRelationship] = []

    for rel in raw_relationships:
        if not isinstance(rel, dict):
            continue
        src_name = str(rel.get("source_entity", "")).strip()
        pred = str(rel.get("predicate", "")).strip()
        tgt_name = str(rel.get("target_entity", "")).strip()
        chunk_id = str(rel.get("chunk_id", "")).strip()
        verbatim_ev = str(rel.get("verbatim_evidence", "")).strip()

        if not src_name or not pred or not tgt_name or not chunk_id:
            continue

        if not verbatim_ev:
            logger.info("Candidate relationship rejected: empty verbatim_evidence")
            continue

        if chunk_id not in chunks_map:
            logger.info("Candidate relationship cited unknown/unauthorized chunk_id: %s", chunk_id)
            continue

        ch_obj, ch_text, ch_page = chunks_map[chunk_id]

        if verbatim_ev not in ch_text:
            logger.info("Candidate relationship rejected: verbatim_evidence '%s' not present in chunk text", verbatim_ev)
            continue

        # Preserve source context during grounding:
        # Locate the evidence in its surrounding source sentence or clause.
        # Reject evidence that removes conditional, negated, hypothetical, reported, or disputed context.
        # Cover repeated evidence strings in different contexts.
        ev_matches = list(re.finditer(re.escape(verbatim_ev), ch_text))
        if not ev_matches:
            logger.info("Candidate relationship rejected: verbatim evidence not found in chunk text")
            continue

        grounded_occurrence_found = False
        rejection_reasons = []
        for m in ev_matches:
            surrounding_sentence = _find_enclosing_sentence(ch_text, m.start(), m.end())
            is_valid, reason = validate_relationship_grounding(
                chunk_text=surrounding_sentence,
                source_entity_name=src_name,
                predicate=pred,
                target_entity_name=tgt_name,
                evidence_span=verbatim_ev,
            )
            if is_valid:
                grounded_occurrence_found = True
                break
            else:
                rejection_reasons.append(reason or "unsupported")

        if not grounded_occurrence_found:
            logger.info(
                "Candidate relationship (%s, %s, %s) rejected by grounding in surrounding source context: %s",
                src_name, pred, tgt_name, "; ".join(rejection_reasons),
            )
            continue

        norm_src = normalize_mention(src_name)
        norm_tgt = normalize_mention(tgt_name)
        src_type, src_disambig = entity_info.get(norm_src, ("concept", None))
        tgt_type, tgt_disambig = entity_info.get(norm_tgt, ("concept", None))

        src_ent_id = compute_entity_id(
            workspace_id=workspace_id,
            document_id=document_id,
            entity_type=src_type,
            name=src_name,
            disambiguation_token=src_disambig,
        )
        tgt_ent_id = compute_entity_id(
            workspace_id=workspace_id,
            document_id=document_id,
            entity_type=tgt_type,
            name=tgt_name,
            disambiguation_token=tgt_disambig,
        )

        canon_pred = canonicalize_predicate(pred)
        rel_id = compute_relationship_id(
            workspace_id=workspace_id,
            source_entity_id=src_ent_id,
            canonical_predicate=canon_pred,
            target_entity_id=tgt_ent_id,
            document_id=document_id,
            document_version=document_version,
            publication_id=publication_id,
        )

        if src_ent_id not in extracted_entities:
            extracted_entities[src_ent_id] = KnowledgeGraphEntity(
                entity_id=src_ent_id,
                workspace_id=workspace_id,
                name=src_name,
                canonical_name=norm_src,
                entity_type=src_type if src_type in ("organization", "person", "location", "product", "concept", "table", "other") else "concept",
                source_document_id=document_id if not src_disambig else None,
                disambiguation_token=src_disambig,
            )

        if tgt_ent_id not in extracted_entities:
            extracted_entities[tgt_ent_id] = KnowledgeGraphEntity(
                entity_id=tgt_ent_id,
                workspace_id=workspace_id,
                name=tgt_name,
                canonical_name=norm_tgt,
                entity_type=tgt_type if tgt_type in ("organization", "person", "location", "product", "concept", "table", "other") else "concept",
                source_document_id=document_id if not tgt_disambig else None,
                disambiguation_token=tgt_disambig,
            )

        location = DocumentLocation(page=ch_page)
        rel_model = KnowledgeGraphRelationship(
            relationship_id=rel_id,
            workspace_id=workspace_id,
            source_entity_id=src_ent_id,
            source_entity_name=src_name,
            predicate=pred,
            canonical_predicate=canon_pred,
            target_entity_id=tgt_ent_id,
            target_entity_name=tgt_name,
            directed=True,
            confidence=1.0,
            source_document_id=document_id,
            source_document_version=document_version,
            publication_id=publication_id,
            source_chunk_ids=(chunk_id,),
            location=location,
            verbatim_evidence=verbatim_ev,
            relationship_state="unique",
            created_at_utc=_now_utc(),
        )
        extracted_relationships.append(rel_model)

    return list(extracted_entities.values()), extracted_relationships
