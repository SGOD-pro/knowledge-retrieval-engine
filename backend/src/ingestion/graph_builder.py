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
import os
import re
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
    from schemas.contracts.location import DocumentLocation, TableLocation
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
    # Allow stem match (e.g. "acquire" for "acquired", "partner" for "partnered")
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
        s_matches = [m.start() for m in re.finditer(r"\b" + re.escape(source_lower) + r"\b", sentence)]
        t_matches = [m.start() for m in re.finditer(r"\b" + re.escape(target_lower) + r"\b", sentence)]
        if not s_matches or not t_matches:
            continue

        p_matches = list(re.finditer(r"\b" + re.escape(pred_stem) + r"\w*\b", sentence))
        if not p_matches:
            continue

        for p_match in p_matches:
            p_idx = p_match.start()

            # Check for negation words in the sentence
            tokens = _clean_text_tokens(sentence)
            if any(w in _NEGATION_WORDS for w in tokens):
                return False, "negated_relationship"

            # Check passive voice: e.g. "<target> was <predicate> by <source>"
            # e.g. "Beta Tech was acquired by Acme Corp"
            is_passive = bool(re.search(r"\b(?:was|is|been|were)\s+" + re.escape(pred_stem) + r"\w*\s+by\b", sentence))

            has_reversed = False
            for s_idx in s_matches:
                for t_idx in t_matches:
                    if is_passive:
                        # In passive voice: Target appears before predicate, Source appears after 'by'
                        if t_idx < p_idx < s_idx:
                            return True, None
                        elif s_idx < p_idx < t_idx:
                            has_reversed = True
                    else:
                        # Active voice: Source should appear before predicate, Target after predicate
                        if s_idx < p_idx < t_idx:
                            return True, None
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
            cid = str(getattr(ch, "id", None) or getattr(ch, "chunk_id", None) or "")
            text = str(getattr(ch, "text", None) or getattr(ch, "content", None) or "")
            page = int(getattr(ch, "page", None) or (getattr(ch, "metadata", {}) or {}).get("page", 1))
            text_bytes = text.encode("utf-8")
            if len(text_bytes) > 2000:
                text = text_bytes[:2000].decode("utf-8", errors="ignore")
            block = f'<chunk id="{cid}" page="{page}">\n{text}\n</chunk>'
            block_bytes = len(block.encode("utf-8"))
            if block_bytes > remaining_bytes:
                break
            user_content_blocks.append(block)
            remaining_bytes -= (block_bytes + 2)

        user_prompt = "\n\n".join(user_content_blocks)
        messages = [{"role": "user", "content": [{"text": user_prompt}]}]
        system = [{"text": BEDROCK_GRAPH_SYSTEM_PROMPT}]
        model_id = getattr(settings, "GRAPH_BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")

        if client is not None:
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
        usage = {"inputTokens": estimated_prompt_tokens, "outputTokens": 0}
        dispatch_success = False

        if client is None:
            output_text = "{}"
            dispatch_success = True
        else:
            max_attempts = 2
            for attempt in range(max_attempts):
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

                try:
                    resp = client.converse(
                        modelId=model_id,
                        messages=messages,
                        system=system,
                        inferenceConfig={"temperature": 0.0, "maxTokens": max_output_tokens},
                    )
                    output_text = resp.get("output", {}).get("message", {}).get("content", [{}])[0].get("text", "")
                    raw_usage = resp.get("usage", {})
                    usage = {
                        "inputTokens": raw_usage.get("inputTokens", estimated_prompt_tokens),
                        "outputTokens": raw_usage.get("outputTokens", 0),
                    }
                    dispatch_success = True
                    break
                except Exception as exc:
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

        if not dispatch_success:
            return self._fail_closed_manifest(workspace_id, document_id, document_version, base_publication_id, existing_man, idempotency_key)

        actual_in = usage.get("inputTokens", estimated_prompt_tokens)
        actual_out = usage.get("outputTokens", 0)
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
        text = str(getattr(ch, "text", None) or getattr(ch, "content", None) or "")
        page = int(getattr(ch, "page", None) or (getattr(ch, "metadata", {}) or {}).get("page", 1))
        if cid:
            chunks_map[cid] = (ch, text, page)

    entity_info: dict[str, tuple[str, str | None]] = {}
    if isinstance(raw_entities, list):
        for ent in raw_entities:
            if isinstance(ent, dict):
                ename = str(ent.get("name", "")).strip()
                etype = str(ent.get("entity_type", "concept")).strip().lower()
                disambig = ent.get("disambiguation_token")
                if disambig:
                    disambig = str(disambig).strip()
                if ename:
                    norm = normalize_mention(ename)
                    entity_info[norm] = (etype, disambig)

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

        if chunk_id not in chunks_map:
            logger.info("Candidate relationship cited unknown/unauthorized chunk_id: %s", chunk_id)
            continue

        ch_obj, ch_text, ch_page = chunks_map[chunk_id]

        is_valid, reason = validate_relationship_grounding(
            chunk_text=ch_text,
            source_entity_name=src_name,
            predicate=pred,
            target_entity_name=tgt_name,
        )
        if not is_valid:
            logger.info("Candidate relationship (%s, %s, %s) rejected by grounding: %s", src_name, pred, tgt_name, reason)
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
            verbatim_evidence=verbatim_ev or ch_text[:300],
            relationship_state="unique",
            created_at_utc=_now_utc(),
        )
        extracted_relationships.append(rel_model)

    return list(extracted_entities.values()), extracted_relationships
