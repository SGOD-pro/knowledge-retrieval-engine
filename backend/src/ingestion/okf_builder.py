"""OKF Builder — extract and persist knowledge graph to DynamoDB.

System 1: Smart Extraction
  Runs gate_and_rank_chunks() from concept_service to filter junk chunks.
  Dynamic batching: groups gated chunks into ~10,000-token prompts for Nova
  Micro. Cuts API calls by ~90% compared to the old brute-force 20-chunk
  batches.

System 2: Deterministic Knowledge Graph Edges
  Three edge types written to okf_relations table (no LLM):
  1. CHILD_OF      — paragraph → parent heading (from section_path)
  2. LOCATED_ON    — table cell → page node
  3. CONTAINS_FACT — source chunk → OKF Entity Node
  4. SEMANTICALLY_RELATED — cross-document: Qdrant cosine > 0.85 (max 5/node)

DynamoDB tables:
  okf_entities   PK=ENTITY#{concept_id}  SK=META          → concept metadata
  okf_relations  PK=ENTITY#{from_id}     SK=REL#{rel}#{to_id} → edges
  okf_properties PK=ENTITY#{concept_id}  SK=PROP#{name}#{chunk_id} → typed facts
  kre-table      PK=DOC#{doc_id}         SK=OKF_USAGE     → pre-aggregated token usage

Token tracking uses DynamoDB ADD (atomic increment) — never re-aggregate on read.
"""

import json
import logging
import os
import time
from decimal import Decimal

logger = logging.getLogger(__name__)

# Approximate tokens per character (conservative — real average is ~4 chars/token)
_CHARS_PER_TOKEN = 4
_MAX_PROMPT_TOKENS = 10_000
_MAX_PROMPT_CHARS = _MAX_PROMPT_TOKENS * _CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_okf(document) -> None:
    """Extract OKF facts from a Document and persist to DynamoDB.

    Called by the ingestion pipeline after chunk parsing.
    Silent on failure — OKF is additive; a failure here never blocks ingestion.
    """
    t0 = time.perf_counter()
    doc_id = str(document.id)
    chunks = list(document.chunks)

    logger.info("okf_builder.start doc_id=%s chunk_count=%d", doc_id, len(chunks))

    try:
        # --- System 1: Smart Tier-1 extraction ---
        from ingestion.concept_service import gate_and_rank_chunks, extract_tier1_patterns

        gated = gate_and_rank_chunks(chunks)
        gated_chunks = [c for c, _ in gated]

        tier1_props = extract_tier1_patterns(gated_chunks)
        logger.info("okf_builder.tier1 doc_id=%s property_count=%d", doc_id, len(tier1_props))

        # --- System 1: Smart Tier-3 extraction (dynamic batching) ---
        tier3_props, token_stats = _extract_tier3_smart(gated_chunks)
        logger.info(
            "okf_builder.tier3 doc_id=%s property_count=%d "
            "input_tokens=%d output_tokens=%d calls=%d",
            doc_id, len(tier3_props),
            token_stats["input_tokens"], token_stats["output_tokens"], token_stats["calls"],
        )

        all_props = tier1_props + tier3_props

        if not all_props:
            logger.info("okf_builder.no_properties doc_id=%s", doc_id)
        else:
            # Cluster / normalize entity names
            from ingestion.normalize_service import cluster_entities
            entity_names = list({p["concept"] for p in all_props if p.get("concept")})
            canonical_map = cluster_entities(entity_names)
            logger.info("okf_builder.normalize doc_id=%s clusters=%d", doc_id, len(set(canonical_map.values())))

            # Build concept registry
            concepts: dict[str, dict] = {}
            for prop in all_props:
                raw_name = prop.get("concept", "")
                if not raw_name:
                    continue
                canon = canonical_map.get(raw_name, raw_name)
                concept_id = _concept_key(canon)
                if concept_id not in concepts:
                    concepts[concept_id] = {
                        "concept_id": concept_id,
                        "name": canon,
                        "document_ids": set(),
                        "property_count": 0,
                    }
                concepts[concept_id]["document_ids"].add(doc_id)
                concepts[concept_id]["property_count"] += 1

            # Write entities + properties
            repo = _get_repo()
            if repo is not None:
                _write_entities(repo, concepts)
                _write_properties(repo, all_props, canonical_map, doc_id)
                _update_token_usage(repo, doc_id, token_stats)

                # --- System 2: Deterministic Knowledge Graph Edges ---
                _write_structural_edges(repo, chunks, doc_id)
                _write_entity_chunk_edges(repo, all_props, canonical_map, doc_id)
                _write_semantic_edges(repo, gated_chunks, doc_id)

        latency_ms = (time.perf_counter() - t0) * 1000.0
        logger.info(
            "okf_builder.done doc_id=%s prop_count=%d latency_ms=%.2f",
            doc_id, len(all_props), latency_ms,
        )

    except Exception as e:
        logger.error("okf_builder.failed doc_id=%s error=%s", doc_id, e, exc_info=True)


# ---------------------------------------------------------------------------
# System 1: Smart Tier-3 extraction with dynamic batching
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Rough token count estimate: characters / 4."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _extract_tier3_smart(gated_chunks: list) -> tuple[list[dict], dict]:
    """Call Nova Micro with dynamic batching capped at ~10k tokens per prompt.

    Returns (properties_list, token_stats_dict).
    token_stats: {"input_tokens": int, "output_tokens": int, "calls": int}
    """
    token_stats = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    from config import settings
    if settings.ENVIRONMENT == "test":
        return [], token_stats

    if not gated_chunks:
        return [], token_stats

    from providers.bedrock_models import get_concept_model
    from providers.provider_client import enforce_rate_limit
    from aws.infra import get_client

    model_id = get_concept_model()
    enforce_rate_limit(model_id)
    client = get_client("bedrock-runtime")

    system_prompt = (
        "You are a strict knowledge extraction system. "
        "Extract typed facts from the provided text chunks.\n"
        "Return a JSON array — each object must have exactly these keys: "
        '{"concept": "EntityName", "property_name": "AttributeName", '
        '"property_value": "value", "source_chunk_id": "chunk_id", "confidence": 0.0-1.0}\n'
        "Only extract facts where you have high confidence. Return [] if nothing meaningful found."
    )

    results = []

    # Build dynamic batches up to _MAX_PROMPT_CHARS
    batches: list[list] = []
    current_batch: list = []
    current_chars = 0

    for chunk in gated_chunks:
        chunk_text = f"[{chunk.id}]: {chunk.text}"
        chunk_chars = len(chunk_text) + 5  # +5 for separator
        if current_chars + chunk_chars > _MAX_PROMPT_CHARS and current_batch:
            batches.append(current_batch)
            current_batch = [chunk]
            current_chars = chunk_chars
        else:
            current_batch.append(chunk)
            current_chars += chunk_chars

    if current_batch:
        batches.append(current_batch)

    logger.info(
        "okf_builder.tier3_batches gated_chunks=%d batches=%d",
        len(gated_chunks), len(batches),
    )

    for batch_idx, batch in enumerate(batches):
        batch_text = "\n---\n".join([f"[{c.id}]: {c.text}" for c in batch])
        user_prompt = f"Extract properties from these document chunks:\n{batch_text}"

        try:
            t0 = time.perf_counter()
            response = client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": user_prompt}]}],
                system=[{"text": system_prompt}],
                inferenceConfig={"temperature": 0.0, "maxTokens": 4096},
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0

            usage = response.get("usage", {})
            in_tok = int(usage.get("inputTokens", 0))
            out_tok = int(usage.get("outputTokens", 0))
            token_stats["input_tokens"] += in_tok
            token_stats["output_tokens"] += out_tok
            token_stats["calls"] += 1

            logger.info(
                "okf_builder.tier3_call batch_idx=%d chunks=%d input_tokens=%d output_tokens=%d latency_ms=%.2f",
                batch_idx, len(batch), in_tok, out_tok, latency_ms,
            )

            output_text = response["output"]["message"]["content"][0]["text"].strip()
            if output_text.startswith("```json"):
                output_text = output_text[7:]
            elif output_text.startswith("```"):
                output_text = output_text[3:]
            if output_text.endswith("```"):
                output_text = output_text[:-3]
            output_text = output_text.strip()

            extracted = json.loads(output_text)
            if isinstance(extracted, list):
                results.extend(extracted)

        except Exception as e:
            logger.error("okf_builder.tier3_call_failed batch_idx=%d error=%s", batch_idx, e)

    return results, token_stats


# ---------------------------------------------------------------------------
# System 2: Deterministic Knowledge Graph Edges
# ---------------------------------------------------------------------------

def _write_structural_edges(repo, chunks: list, doc_id: str) -> None:
    """Edge Type 1 & 2: CHILD_OF and LOCATED_ON edges from chunk structure."""
    # Build chunk_id → chunk lookup
    chunk_map = {str(c.id): c for c in chunks}

    # Build heading hierarchy from section_path (index 0 = top-level heading)
    # We create edges from each chunk to its structural parent
    for chunk in chunks:
        try:
            chunk_id = str(chunk.id)
            section_path = list(chunk.section_path) if chunk.section_path else []

            # LOCATED_ON: table cell → page (for all non-text chunks with a page number)
            if chunk.element_type in ("cell", "table") and chunk.page_number is not None:
                _put_edge(repo, f"CHUNK#{chunk_id}", "LOCATED_ON", f"PAGE#{doc_id}#{chunk.page_number}")

            # CHILD_OF: if section_path has a parent heading, draw the edge
            if len(section_path) > 1:
                parent_heading = section_path[-2]  # immediate parent heading text
                parent_id = f"HEADING#{_concept_key(parent_heading)}"
                _put_edge(repo, f"CHUNK#{chunk_id}", "CHILD_OF", parent_id)

        except Exception as e:
            logger.warning("okf_builder.structural_edge_failed chunk_id=%s error=%s", chunk.id, e)


def _write_entity_chunk_edges(repo, props: list[dict], canonical_map: dict, doc_id: str) -> None:
    """Edge Type 3: CONTAINS_FACT — source chunk → OKF Entity Node."""
    for prop in props:
        try:
            chunk_id = str(prop.get("source_chunk_id", ""))
            raw_name = prop.get("concept", "")
            if not chunk_id or not raw_name:
                continue
            canon = canonical_map.get(raw_name, raw_name)
            entity_id = _concept_key(canon)
            _put_edge(repo, f"CHUNK#{chunk_id}", "CONTAINS_FACT", f"ENTITY#{entity_id}")
        except Exception as e:
            logger.warning("okf_builder.entity_edge_failed error=%s", e)


def _write_semantic_edges(repo, gated_chunks: list, doc_id: str) -> None:
    """Edge Type 4: SEMANTICALLY_RELATED — cross-doc cosine > 0.85 via Qdrant (max 5/node)."""
    try:
        from db.database import CloudRepository
        db = CloudRepository()

        # Cap semantic edge discovery to top 15 highest-priority gated chunks per doc to keep ingestion fast
        for chunk in gated_chunks[:15]:
            if not chunk.embedding_full:
                continue
            try:
                results = db.qclient.query_points(
                    collection_name=db.collection_name,
                    query=chunk.embedding_full,
                    using="embedding_full",
                    limit=10,
                    with_payload=True,
                )
                edge_count = 0
                for point in results.points:
                    if edge_count >= 5:
                        break
                    score = float(point.score)
                    if score < 0.85:
                        continue
                    related_chunk_id = point.payload.get("original_id", "")
                    related_doc_id = point.payload.get("document_id", "")
                    # Skip self-matches
                    if related_chunk_id == str(chunk.id):
                        continue
                    _put_edge(
                        repo,
                        f"CHUNK#{chunk.id}",
                        "SEMANTICALLY_RELATED",
                        f"CHUNK#{related_chunk_id}",
                        extra={"score": Decimal(str(round(score, 4))), "related_doc_id": related_doc_id},
                    )
                    edge_count += 1
            except Exception as e:
                logger.debug("okf_builder.semantic_edge_skip chunk_id=%s error=%s", chunk.id, e)

    except Exception as e:
        logger.warning("okf_builder.semantic_edges_failed error=%s", e)


def _put_edge(repo, from_id: str, rel_type: str, to_id: str, extra: dict | None = None) -> None:
    """Write a single directed edge to okf_relations table."""
    item = {
        "PK": from_id,
        "SK": f"REL#{rel_type}#{to_id}",
        "rel_type": rel_type,
        "to_id": to_id,
    }
    if extra:
        item.update(extra)
    repo.okf_relations_table.put_item(Item=item)


# ---------------------------------------------------------------------------
# DynamoDB writes
# ---------------------------------------------------------------------------

def _concept_key(name: str) -> str:
    """Canonical concept ID — uppercase stripped."""
    return name.strip().upper().replace(" ", "_")


def _get_repo():
    """Return CloudRepository or None in test env."""
    from config import settings
    if settings.ENVIRONMENT == "test":
        return None
    from db.database import CloudRepository
    return CloudRepository()


def _write_entities(repo, concepts: dict) -> None:
    """Write/update concept META items in okf_entities table."""
    for concept_id, meta in concepts.items():
        try:
            repo.okf_entities_table.put_item(Item={
                "PK": f"ENTITY#{concept_id}",
                "SK": "META",
                "name": meta["name"],
                "document_ids": list(meta["document_ids"]),
                "property_count": Decimal(str(meta["property_count"])),
            })
        except Exception as e:
            logger.warning("okf_builder.write_entity_failed concept_id=%s error=%s", concept_id, e)


def _write_properties(repo, props: list[dict], canonical_map: dict, doc_id: str) -> None:
    """Write property items to okf_properties table."""
    for prop in props:
        raw_name = prop.get("concept", "")
        if not raw_name:
            continue
        canon = canonical_map.get(raw_name, raw_name)
        concept_id = _concept_key(canon)
        prop_name = str(prop.get("property_name", ""))
        chunk_id = str(prop.get("source_chunk_id", ""))
        if not prop_name or not chunk_id:
            continue

        try:
            repo.okf_properties_table.put_item(Item={
                "PK": f"ENTITY#{concept_id}",
                "SK": f"PROP#{prop_name}#{chunk_id}",
                "property_name": prop_name,
                "property_value": str(prop.get("property_value", "")),
                "source_chunk_id": chunk_id,
                "confidence": Decimal(str(round(float(prop.get("confidence", 1.0)), 4))),
                "doc_id": doc_id,
            })
        except Exception as e:
            logger.warning(
                "okf_builder.write_property_failed concept=%s prop=%s error=%s",
                concept_id, prop_name, e,
            )


def _update_token_usage(repo, doc_id: str, token_stats: dict) -> None:
    """Atomically update pre-aggregated OKF token usage for the document.

    DynamoDB ADD is atomic — safe to call concurrently.
    Pattern: PK=DOC#{doc_id}, SK=OKF_USAGE — SUMMARY item (never re-aggregate on read).
    """
    if token_stats["calls"] == 0:
        return
    try:
        repo.table.update_item(
            Key={"PK": f"DOC#{doc_id}", "SK": "OKF_USAGE"},
            UpdateExpression=(
                "ADD input_tokens :i, output_tokens :o, extraction_calls :c "
                "SET updated_at = :t"
            ),
            ExpressionAttributeValues={
                ":i": Decimal(str(token_stats["input_tokens"])),
                ":o": Decimal(str(token_stats["output_tokens"])),
                ":c": Decimal(str(token_stats["calls"])),
                ":t": str(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            },
        )
        logger.info(
            "okf_builder.token_usage_saved doc_id=%s input_tokens=%d output_tokens=%d calls=%d",
            doc_id,
            token_stats["input_tokens"],
            token_stats["output_tokens"],
            token_stats["calls"],
        )
    except Exception as e:
        logger.warning("okf_builder.token_usage_write_failed doc_id=%s error=%s", doc_id, e)



# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_okf(document) -> None:
    """Extract OKF facts from a Document and persist to DynamoDB.

    Called by the ingestion pipeline after chunk parsing.
    Silent on failure — OKF is additive; a failure here never blocks ingestion.

    Args:
        document: schemas.models.Document instance with .id and .chunks.
    """
    t0 = time.perf_counter()
    doc_id = str(document.id)
    chunks = list(document.chunks)

    logger.info("okf_builder.start doc_id=%s chunk_count=%d", doc_id, len(chunks))

    try:
        # Tier 1: fast regex extraction (no LLM)
        from ingestion.concept_service import extract_tier1_patterns
        tier1_props = extract_tier1_patterns(chunks)
        logger.info("okf_builder.tier1 doc_id=%s property_count=%d", doc_id, len(tier1_props))

        # Tier 3: Nova Micro LLM extraction (tracks tokens)
        tier3_props, token_stats = _extract_tier3_with_tracking(chunks)
        logger.info(
            "okf_builder.tier3 doc_id=%s property_count=%d "
            "input_tokens=%d output_tokens=%d calls=%d",
            doc_id, len(tier3_props),
            token_stats["input_tokens"], token_stats["output_tokens"], token_stats["calls"],
        )

        all_props = tier1_props + tier3_props

        if not all_props:
            logger.info("okf_builder.no_properties doc_id=%s", doc_id)
            return

        # Cluster / normalize entity names
        from ingestion.normalize_service import cluster_entities
        entity_names = list({p["concept"] for p in all_props if p.get("concept")})
        canonical_map = cluster_entities(entity_names)
        logger.info("okf_builder.normalize doc_id=%s clusters=%d", doc_id, len(set(canonical_map.values())))

        # Build concept registry
        concepts: dict[str, dict] = {}
        for prop in all_props:
            raw_name = prop.get("concept", "")
            if not raw_name:
                continue
            canon = canonical_map.get(raw_name, raw_name)
            concept_id = _concept_key(canon)
            if concept_id not in concepts:
                concepts[concept_id] = {
                    "concept_id": concept_id,
                    "name": canon,
                    "document_ids": set(),
                    "property_count": 0,
                }
            concepts[concept_id]["document_ids"].add(doc_id)
            concepts[concept_id]["property_count"] += 1

        # Write to DynamoDB
        repo = _get_repo()
        if repo is None:
            return

        _write_entities(repo, concepts)
        _write_properties(repo, all_props, canonical_map, doc_id)
        _update_token_usage(repo, doc_id, token_stats)

        latency_ms = (time.perf_counter() - t0) * 1000.0
        logger.info(
            "okf_builder.done doc_id=%s entity_count=%d prop_count=%d latency_ms=%.2f",
            doc_id, len(concepts), len(all_props), latency_ms,
        )

    except Exception as e:
        logger.error("okf_builder.failed doc_id=%s error=%s", doc_id, e, exc_info=True)


# ---------------------------------------------------------------------------
# Tier 3 extraction with token tracking
# ---------------------------------------------------------------------------

def _extract_tier3_with_tracking(chunks) -> tuple[list[dict], dict]:
    """Call Nova Micro and capture token usage from Bedrock converse response.

    Returns (properties_list, token_stats_dict).
    token_stats: {"input_tokens": int, "output_tokens": int, "calls": int}
    """
    token_stats = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    from config import settings
    if settings.ENVIRONMENT == "test":
        # No LLM calls in test mode
        return [], token_stats

    from providers.bedrock_models import get_concept_model
    from providers.provider_client import enforce_rate_limit

    model_id = get_concept_model()
    enforce_rate_limit(model_id)

    results = []
    batch_size = 20

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i: i + batch_size]
        batch_text = "\n---\n".join([f"[{c.id}]: {c.text}" for c in batch])

        system_prompt = (
            "You are a strict knowledge extraction system. "
            "Extract typed facts from the provided text chunks.\n"
            "Return a JSON array — each object must have exactly these keys: "
            '{"concept": "EntityName", "property_name": "AttributeName", '
            '"property_value": "value", "source_chunk_id": "chunk_id", "confidence": 0.0-1.0}'
        )
        user_prompt = f"Extract properties from these chunks:\n{batch_text}"

        try:
            from aws.infra import get_client
            client = get_client("bedrock-runtime")

            t0 = time.perf_counter()
            response = client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": user_prompt}]}],
                system=[{"text": system_prompt}],
                inferenceConfig={"temperature": 0.0, "maxTokens": 4096},
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0

            # Token usage from Bedrock converse API
            usage = response.get("usage", {})
            in_tok = int(usage.get("inputTokens", 0))
            out_tok = int(usage.get("outputTokens", 0))
            token_stats["input_tokens"] += in_tok
            token_stats["output_tokens"] += out_tok
            token_stats["calls"] += 1

            logger.info(
                "okf_builder.tier3_call batch_idx=%d input_tokens=%d output_tokens=%d latency_ms=%.2f",
                i // batch_size, in_tok, out_tok, latency_ms,
            )

            output_text = response["output"]["message"]["content"][0]["text"]
            # Strip markdown fences
            output_text = output_text.strip()
            if output_text.startswith("```json"):
                output_text = output_text[7:]
            elif output_text.startswith("```"):
                output_text = output_text[3:]
            if output_text.endswith("```"):
                output_text = output_text[:-3]
            output_text = output_text.strip()

            extracted = json.loads(output_text)
            if isinstance(extracted, list):
                results.extend(extracted)

        except Exception as e:
            logger.error("okf_builder.tier3_call_failed batch_idx=%d error=%s", i // batch_size, e)

    return results, token_stats


# ---------------------------------------------------------------------------
# DynamoDB writes
# ---------------------------------------------------------------------------

def _concept_key(name: str) -> str:
    """Canonical concept ID — uppercase stripped."""
    return name.strip().upper().replace(" ", "_")


def _get_repo():
    """Return CloudRepository or None in test env."""
    from config import settings
    if settings.ENVIRONMENT == "test":
        return None
    from db.database import CloudRepository
    return CloudRepository()


def _write_entities(repo, concepts: dict) -> None:
    """Write/update concept META items in okf_entities table."""
    for concept_id, meta in concepts.items():
        try:
            repo.okf_entities_table.put_item(Item={
                "PK": f"ENTITY#{concept_id}",
                "SK": "META",
                "name": meta["name"],
                "document_ids": list(meta["document_ids"]),
                "property_count": Decimal(str(meta["property_count"])),
            })
        except Exception as e:
            logger.warning("okf_builder.write_entity_failed concept_id=%s error=%s", concept_id, e)


def _write_properties(repo, props: list[dict], canonical_map: dict, doc_id: str) -> None:
    """Write property items to okf_properties table."""
    for prop in props:
        raw_name = prop.get("concept", "")
        if not raw_name:
            continue
        canon = canonical_map.get(raw_name, raw_name)
        concept_id = _concept_key(canon)
        prop_name = str(prop.get("property_name", ""))
        chunk_id = str(prop.get("source_chunk_id", ""))
        if not prop_name or not chunk_id:
            continue

        try:
            repo.okf_properties_table.put_item(Item={
                "PK": f"ENTITY#{concept_id}",
                "SK": f"PROP#{prop_name}#{chunk_id}",
                "property_name": prop_name,
                "property_value": str(prop.get("property_value", "")),
                "source_chunk_id": chunk_id,
                "confidence": Decimal(str(round(float(prop.get("confidence", 1.0)), 4))),
                "doc_id": doc_id,
            })
        except Exception as e:
            logger.warning(
                "okf_builder.write_property_failed concept=%s prop=%s error=%s",
                concept_id, prop_name, e,
            )


def _update_token_usage(repo, doc_id: str, token_stats: dict) -> None:
    """Atomically update pre-aggregated OKF token usage for the document.

    DynamoDB ADD is atomic — safe to call concurrently.
    Pattern: PK=DOC#{doc_id}, SK=OKF_USAGE — SUMMARY item (never re-aggregate on read).
    """
    if token_stats["calls"] == 0:
        return
    try:
        repo.table.update_item(
            Key={"PK": f"DOC#{doc_id}", "SK": "OKF_USAGE"},
            UpdateExpression=(
                "ADD input_tokens :i, output_tokens :o, extraction_calls :c "
                "SET updated_at = :t"
            ),
            ExpressionAttributeValues={
                ":i": Decimal(str(token_stats["input_tokens"])),
                ":o": Decimal(str(token_stats["output_tokens"])),
                ":c": Decimal(str(token_stats["calls"])),
                ":t": str(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            },
        )
        logger.info(
            "okf_builder.token_usage_saved doc_id=%s input_tokens=%d output_tokens=%d calls=%d",
            doc_id,
            token_stats["input_tokens"],
            token_stats["output_tokens"],
            token_stats["calls"],
        )
    except Exception as e:
        logger.warning("okf_builder.token_usage_write_failed doc_id=%s error=%s", doc_id, e)
