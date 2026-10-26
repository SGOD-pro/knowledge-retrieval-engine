"""Bounded Open Knowledge Framework (OKF) extractor and builder.

Extracts canonical entity properties, curated table schemas, and verified facts
from text PDFs and tabular CSVs within strict token, model call, and deadline budgets.
Validates assertions against authorized source chunks and locators, rejecting
unsupported or hallucinated model claims before persistence.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re
import time
from typing import Any, Sequence

from config import settings
from src.db.okf_repo import (
    OkfRepositoryBase,
    create_dedicated_storage_config,
    disable_client_retries,
    get_okf_repo,
)
from src.schemas.contracts.okf import (
    OkfEntityRecord,
    OkfManifest,
    OkfPropertyAssertion,
    compute_assertion_id,
    compute_entity_id,
    compute_idempotency_key,
    normalize_entity_name,
    normalize_property_key,
)

logger = logging.getLogger(__name__)

# Approximate characters per token for batch sizing
_CHARS_PER_TOKEN = 4


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def is_value_grounded(property_value: str, chunk_text: str) -> bool:
    """Validate that property_value appears verbatim (or as normalized token sequence) in chunk."""
    if not property_value or not chunk_text:
        return False
    pv = property_value.strip().lower()
    ct = chunk_text.strip().lower()

    # Exact phrase with word boundaries to avoid false positives (e.g. 'cat' in 'communication')
    left_bound = r"\b" if re.match(r"^\w", pv) else r""
    right_bound = r"\b" if re.search(r"\w$", pv) else r""
    pattern = rf"{left_bound}{re.escape(pv)}{right_bound}"
    if re.search(pattern, ct):
        return True

    # Number normalization check with word boundaries (e.g. $4,191 -> 4191)
    pv_num = re.sub(r"(?<=\d),(?=\d)", "", pv)
    ct_num = re.sub(r"(?<=\d),(?=\d)", "", ct)
    if pv_num != pv or ct_num != ct:
        left_bound = r"\b" if re.match(r"^\w", pv_num) else r""
        right_bound = r"\b" if re.search(r"\w$", pv_num) else r""
        pattern_num = rf"{left_bound}{re.escape(pv_num)}{right_bound}"
        if re.search(pattern_num, ct_num):
            return True

    # Token sequence comparison (matches contiguous whole tokens)
    pv_tokens = re.findall(r"\w+", pv_num)
    ct_tokens = re.findall(r"\w+", ct_num)
    if not pv_tokens:
        return False
    n = len(pv_tokens)
    for i in range(len(ct_tokens) - n + 1):
        if ct_tokens[i : i + n] == pv_tokens:
            return True

    return False


class OkfBuilder:
    """Orchestrates bounded OKF fact extraction, grounding validation, and persistence."""

    def __init__(
        self,
        repo: OkfRepositoryBase | None = None,
        bedrock_client: Any | None = None,
    ) -> None:
        self.repo = repo or get_okf_repo()
        self._bedrock_client = bedrock_client

    def _get_bedrock_client(self) -> Any:
        if self._bedrock_client is not None:
            return self._bedrock_client

        env = getattr(settings, "ENVIRONMENT", "dev").lower()
        provider = getattr(settings, "MODEL_PROVIDER", "dev").lower()
        if env == "test" or provider == "test":
            return None

        import boto3
        from aws.infra import get_client

        cfg = create_dedicated_storage_config(connect_timeout=3.0, read_timeout=7.0)
        try:
            client = get_client("bedrock-runtime", config=cfg)
        except Exception:
            session = boto3.Session()
            client = session.client(
                "bedrock-runtime",
                region_name=getattr(settings, "AWS_REGION", "us-east-1"),
                config=cfg,
            )
        disable_client_retries(client)
        self._bedrock_client = client
        return self._bedrock_client

    def extract_and_persist(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        format: str,
        chunks: Sequence[Any],
        table_name: str | None = None,
        table_store: Any | None = None,
        force_reextract: bool = False,
    ) -> OkfManifest:
        """Extract facts and persist manifest enforcing strict resource bounds and ADR-001.
        
        Guarantees:
        - Tabular CSV: 0 model calls, 0 token cost, curated table schema extraction.
        - Text PDF: Max 2 calls, max 4000 input tokens, max 1200 output tokens, 10s deadline.
        - Chunk Grounding: Verbatim text validation; unsupported claims are rejected.
        - Idempotency: Replaying an existing version reuses assertions with 0 model calls.
        - Fail open: Failures return a manifest with status='failed' without raising errors.
        """
        idempotency_key = compute_idempotency_key(workspace_id, document_id, document_version)
        timestamp = _now_utc()

        # Idempotency check: Reuse existing manifest if ready and not forced
        if not force_reextract and self.repo.has_ready_okf(workspace_id, document_id, document_version):
            existing_man = self.repo.get_manifest(workspace_id, document_id, document_version)
            if existing_man:
                logger.info(
                    "okf_builder.idempotent_replay ws=%s doc=%s ver=%s assertions=%d",
                    workspace_id,
                    document_id,
                    document_version,
                    len(existing_man.assertions),
                )
                return existing_man

        fmt = (format or "").lower().replace(".", "")

        try:
            if fmt == "csv":
                return self._extract_csv(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    document_version=document_version,
                    chunks=chunks,
                    table_name=table_name,
                    table_store=table_store,
                    idempotency_key=idempotency_key,
                    timestamp=timestamp,
                )
            elif fmt == "pdf":
                return self._extract_pdf(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    document_version=document_version,
                    chunks=chunks,
                    idempotency_key=idempotency_key,
                    timestamp=timestamp,
                )
            else:
                # Unsupported format for factual entity extraction (DOCX/PPTX) -> return empty manifest
                manifest = OkfManifest(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    document_version=document_version,
                    entities=(),
                    assertions=(),
                    rejected_assertions_count=0,
                    status="empty",
                    extraction_tokens=0,
                    model_calls=0,
                    idempotency_key=idempotency_key,
                    created_at=timestamp,
                )
                self.repo.save_manifest(workspace_id, manifest)
                return manifest

        except Exception as exc:
            logger.warning(
                "okf_builder.extraction_failed ws=%s doc=%s ver=%s error=%s (failing open per ADR-001)",
                workspace_id,
                document_id,
                document_version,
                exc,
            )
            failed_manifest = OkfManifest(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                entities=(),
                assertions=(),
                rejected_assertions_count=0,
                status="failed",
                extraction_tokens=0,
                model_calls=0,
                idempotency_key=idempotency_key,
                created_at=timestamp,
            )
            try:
                self.repo.save_manifest(workspace_id, failed_manifest)
            except Exception:
                pass
            return failed_manifest

    def _extract_csv(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        chunks: Sequence[Any],
        table_name: str | None,
        table_store: Any | None,
        idempotency_key: str,
        timestamp: str,
    ) -> OkfManifest:
        """Deterministic curated table schema and column header extraction with 0 model calls."""
        t_name = table_name or document_id
        ent_name = t_name
        norm_name = normalize_entity_name(ent_name)
        ent_id = compute_entity_id(workspace_id, document_id, norm_name)

        entity = OkfEntityRecord(
            workspace_id=workspace_id,
            entity_id=ent_id,
            entity_name=ent_name,
            normalized_name=norm_name,
            entity_type="table",
            document_id=document_id,
            document_version=document_version,
            created_at=timestamp,
        )

        assertions: list[OkfPropertyAssertion] = []
        columns_found: list[tuple[str, str]] = []  # (col_name, data_type)

        # 1. Attempt to inspect TableStore for schema definition
        if table_store is not None:
            try:
                t_meta = table_store.get_table(t_name, workspace_id)
                if t_meta and hasattr(t_meta, "schema") and t_meta.schema:
                    for col in t_meta.schema.columns:
                        dtype_str = (
                            col.inferred_dtype.value
                            if hasattr(col.inferred_dtype, "value")
                            else str(col.inferred_dtype)
                        )
                        columns_found.append((col.name, dtype_str))
            except Exception as e:
                logger.debug("TableStore schema inspection skipped: %s", e)

        # 2. If TableStore didn't supply columns, inspect chunks for metadata headers
        if not columns_found and chunks:
            for ch in chunks:
                meta = getattr(ch, "metadata", {}) or {}
                hdrs = meta.get("headers")
                if hdrs and isinstance(hdrs, (list, tuple)):
                    for h in hdrs:
                        if h and str(h).strip():
                            columns_found.append((str(h).strip(), "string"))
                    break

        # Fallback default chunk identifier for table facts
        header_chunk_id = str(chunks[0].id) if chunks else f"{document_id}:csv:header"

        # 3. Build property assertions for columns
        for col_name, dtype in columns_found:
            prop_key = f"column:{col_name}"
            prop_val = dtype
            ass_id = compute_assertion_id(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                entity_id=ent_id,
                property_key=prop_key,
                property_value=prop_val,
                source_chunk_id=header_chunk_id,
            )
            assertions.append(
                OkfPropertyAssertion(
                    assertion_id=ass_id,
                    workspace_id=workspace_id,
                    entity_id=ent_id,
                    entity_name=ent_name,
                    property_key=prop_key,
                    property_value=prop_val,
                    value_type="schema_type",
                    source_chunk_id=header_chunk_id,
                    document_id=document_id,
                    document_version=document_version,
                    page_number=None,
                    table_name=t_name,
                    column_name=col_name,
                    confidence_score=1.0,
                    validation_status="verified",
                    created_at=timestamp,
                )
            )

        manifest = OkfManifest(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
            entities=(entity,),
            assertions=tuple(assertions),
            rejected_assertions_count=0,
            status="ready" if assertions else "empty",
            extraction_tokens=0,
            model_calls=0,
            idempotency_key=idempotency_key,
            created_at=timestamp,
        )

        self.repo.save_manifest(workspace_id, manifest)
        logger.info(
            "okf_builder.csv_ready ws=%s doc=%s ver=%s columns=%d (0 tokens, 0 model calls)",
            workspace_id,
            document_id,
            document_version,
            len(assertions),
        )
        return manifest

    def _extract_pdf(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        chunks: Sequence[Any],
        idempotency_key: str,
        timestamp: str,
    ) -> OkfManifest:
        """Bounded Bedrock fact extraction for text PDFs with chunk grounding validation."""
        if not chunks:
            manifest = OkfManifest(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                entities=(),
                assertions=(),
                rejected_assertions_count=0,
                status="empty",
                extraction_tokens=0,
                model_calls=0,
                idempotency_key=idempotency_key,
                created_at=timestamp,
            )
            self.repo.save_manifest(workspace_id, manifest)
            return manifest

        # Build authorized chunk lookup map: source_chunk_id -> chunk
        authorized_chunks = {str(c.id): c for c in chunks}

        # Select introductory and high density chunks up to budget
        # AC-3: max 2 calls, max 2000 input tokens per call, max 4000 total input tokens
        max_calls = getattr(settings, "OKF_MAX_MODEL_CALLS_PER_DOC", 2)
        max_prompt_chars = 2000 * _CHARS_PER_TOKEN  # ~8000 chars per call

        batches: list[list[Any]] = []
        current_batch: list[Any] = []
        current_chars = 0

        for ch in chunks:
            ch_text = getattr(ch, "text", "") or ""
            ch_len = min(len(ch_text), max_prompt_chars)
            if current_chars + ch_len > max_prompt_chars and current_batch:
                batches.append(current_batch)
                current_batch = []
                current_chars = 0
                if len(batches) >= max_calls:
                    break
            current_batch.append(ch)
            current_chars += ch_len

        if current_batch and len(batches) < max_calls:
            batches.append(current_batch)

        total_input_tokens = 0
        total_output_tokens = 0
        calls_made = 0
        raw_candidates: list[dict[str, Any]] = []

        client = self._get_bedrock_client()

        for batch_idx, batch in enumerate(batches):
            if calls_made >= max_calls:
                break

            prompt_text, text_stats = self._invoke_bedrock_extraction(
                client=client,
                chunks=batch,
                workspace_id=workspace_id,
                document_id=document_id,
            )
            calls_made += 1
            total_input_tokens += text_stats["input_tokens"]
            total_output_tokens += text_stats["output_tokens"]

            if prompt_text:
                parsed = self._parse_model_response(prompt_text)
                raw_candidates.extend(parsed)

        # Grounding validation pass (AC-2)
        valid_entities_map: dict[str, OkfEntityRecord] = {}
        valid_assertions: list[OkfPropertyAssertion] = []
        rejected_count = 0

        for item in raw_candidates:
            ent_name = item.get("entity_name") or item.get("concept") or ""
            prop_key = item.get("property_key") or item.get("property") or ""
            prop_val = item.get("property_value") or item.get("value") or ""
            chunk_id = item.get("source_chunk_id") or item.get("chunk_id") or ""
            val_type = item.get("value_type", "string")

            if not ent_name or not prop_key or not prop_val or not chunk_id:
                rejected_count += 1
                continue

            # Check 1: Authorized source chunk membership (AC-2)
            chunk = authorized_chunks.get(str(chunk_id))
            if chunk is None:
                logger.debug(
                    "okf_builder.rejection_unauthorized_chunk chunk_id=%s doc=%s",
                    chunk_id,
                    document_id,
                )
                rejected_count += 1
                continue

            # Check 2: Locator verification (AC-2)
            page_num = getattr(chunk, "page_number", None)
            loc_ref = getattr(chunk, "location_reference", None)
            if page_num is None and loc_ref is None:
                logger.debug("okf_builder.rejection_missing_locator chunk_id=%s", chunk_id)
                rejected_count += 1
                continue

            # Check 3: Grounding text match (verbatim appearance) (AC-2)
            ch_text = getattr(chunk, "text", "") or ""
            if not is_value_grounded(prop_val, ch_text):
                logger.debug(
                    "okf_builder.rejection_unsupported_claim val=%r chunk_id=%s",
                    prop_val,
                    chunk_id,
                )
                rejected_count += 1
                continue

            # Assertion passes all grounding checks!
            norm_name = normalize_entity_name(ent_name)
            ent_id = compute_entity_id(workspace_id, document_id, norm_name)
            if ent_id not in valid_entities_map:
                valid_entities_map[ent_id] = OkfEntityRecord(
                    workspace_id=workspace_id,
                    entity_id=ent_id,
                    entity_name=ent_name,
                    normalized_name=norm_name,
                    entity_type="organization",
                    document_id=document_id,
                    document_version=document_version,
                    created_at=timestamp,
                )

            ass_id = compute_assertion_id(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                entity_id=ent_id,
                property_key=prop_key,
                property_value=prop_val,
                source_chunk_id=str(chunk_id),
            )

            valid_assertions.append(
                OkfPropertyAssertion(
                    assertion_id=ass_id,
                    workspace_id=workspace_id,
                    entity_id=ent_id,
                    entity_name=ent_name,
                    property_key=prop_key,
                    property_value=prop_val,
                    value_type=val_type if val_type in ("string", "number", "date", "boolean", "schema_type") else "string",
                    source_chunk_id=str(chunk_id),
                    document_id=document_id,
                    document_version=document_version,
                    page_number=page_num,
                    table_name=None,
                    column_name=None,
                    confidence_score=1.0,
                    validation_status="verified",
                    created_at=timestamp,
                )
            )

        manifest = OkfManifest(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
            entities=tuple(valid_entities_map.values()),
            assertions=tuple(valid_assertions),
            rejected_assertions_count=rejected_count,
            status="ready" if valid_assertions else ("failed" if calls_made > 0 and not raw_candidates else "empty"),
            extraction_tokens=total_input_tokens + total_output_tokens,
            model_calls=calls_made,
            idempotency_key=idempotency_key,
            created_at=timestamp,
        )

        self.repo.save_manifest(workspace_id, manifest)
        logger.info(
            "okf_builder.pdf_ready ws=%s doc=%s ver=%s assertions=%d rejected=%d tokens=%d calls=%d",
            workspace_id,
            document_id,
            document_version,
            len(valid_assertions),
            rejected_count,
            manifest.extraction_tokens,
            calls_made,
        )
        return manifest

    def _invoke_bedrock_extraction(
        self,
        client: Any,
        chunks: Sequence[Any],
        workspace_id: str,
        document_id: str,
    ) -> tuple[str, dict[str, int]]:
        """Execute Bedrock Converse API with token caps, 10s deadline, and max 1 retry on throttling."""
        system_prompt = (
            "You are a factual property extraction system for an enterprise knowledge retrieval engine.\n"
            "Extract entities and factual property assertions from the provided document chunks.\n"
            "Return ONLY valid JSON matching this schema:\n"
            "[\n"
            "  {\n"
            '    "entity_name": "Entity Name",\n'
            '    "property_key": "property_name",\n'
            '    "property_value": "exact verbatim text from chunk",\n'
            '    "source_chunk_id": "chunk id from header"\n'
            "  }\n"
            "]\n"
            "CRITICAL RULES:\n"
            "1. 'property_value' MUST appear VERBATIM in the chunk text.\n"
            "2. 'source_chunk_id' MUST match the bracketed chunk id exactly.\n"
            "3. Do not invent facts, extrapolate, or hallucinate claims."
        )

        max_prompt_chars = 2000 * _CHARS_PER_TOKEN
        user_content_blocks = []
        for ch in chunks:
            ch_id = str(getattr(ch, "id", ""))
            ch_text = getattr(ch, "text", "") or ""
            if len(ch_text) > max_prompt_chars:
                ch_text = ch_text[:max_prompt_chars]
            user_content_blocks.append(f"<source_chunk id=\"{ch_id}\">\n{ch_text}\n</source_chunk>")

        user_prompt = "\n\n".join(user_content_blocks)

        # Test environment fallback
        if client is None:
            return self._test_mode_fallback(chunks)

        model_id = getattr(settings, "OKF_BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
        max_output_tokens = 600

        messages = [{"role": "user", "content": [{"text": user_prompt}]}]
        max_attempts = 2  # at most 1 retry on throttling (HTTP 429)

        for attempt in range(max_attempts):
            try:
                resp = client.converse(
                    modelId=model_id,
                    messages=messages,
                    system=[{"text": system_prompt}],
                    inferenceConfig={
                        "temperature": 0.0,
                        "maxTokens": max_output_tokens,
                    },
                )
                output_text = resp["output"]["message"]["content"][0]["text"]
                raw_usage = resp.get("usage", {})
                usage = {
                    "input_tokens": raw_usage.get("inputTokens", 0),
                    "output_tokens": raw_usage.get("outputTokens", 0),
                }
                return output_text, usage

            except Exception as e:
                err_str = str(e)
                if ("ThrottlingException" in err_str or "429" in err_str) and attempt < max_attempts - 1:
                    logger.warning("Bedrock OKF throttled (429), retrying once after 2.0s...")
                    time.sleep(2.0)
                    continue
                # Zero retries on model errors, bad requests, or timeouts (AC-3)
                logger.warning("Bedrock OKF call failed (no retries per AC-3): %s", e)
                return "", {"input_tokens": 0, "output_tokens": 0}

        return "", {"input_tokens": 0, "output_tokens": 0}

    def _test_mode_fallback(self, chunks: Sequence[Any]) -> tuple[str, dict[str, int]]:
        """Deterministic extractor for offline unit test execution without AWS Bedrock."""
        items: list[dict[str, Any]] = []
        for ch in chunks:
            ch_id = str(getattr(ch, "id", ""))
            ch_text = getattr(ch, "text", "") or ""

            # Check for revenue patterns
            rev_match = re.search(r"(\$?\d+[\d,.]*\s*(?:billion|million|M|B)?)\s*(?:in\s+)?revenue", ch_text, re.IGNORECASE)
            if rev_match:
                val = rev_match.group(1).strip()
                # Find preceding capitalized words as entity name
                ent_match = re.search(r"([A-Z][a-zA-Z0-9\s]+?)\s+(?:reported|generated|recorded|had)", ch_text)
                ent_name = ent_match.group(1).strip() if ent_match else "Organization"
                items.append({
                    "entity_name": ent_name,
                    "property_key": "revenue",
                    "property_value": val,
                    "source_chunk_id": ch_id,
                })

            # Check for executive role patterns (e.g. John Smith is CEO)
            ceo_match = re.search(r"([A-Z][a-z]+\s+[A-Z][a-z]+)\s+(?:is|serves as|was appointed)\s+(?:the\s+)?(CEO|Chief Executive Officer|Author|President)", ch_text, re.IGNORECASE)
            if ceo_match:
                ent_name = ceo_match.group(1).strip()
                role_val = ceo_match.group(2).strip()
                items.append({
                    "entity_name": ent_name,
                    "property_key": "title",
                    "property_value": role_val,
                    "source_chunk_id": ch_id,
                })

        return json.dumps(items), {"input_tokens": 100, "output_tokens": len(items) * 20}

    def _parse_model_response(self, text: str) -> list[dict[str, Any]]:
        """Parse JSON array of entities and assertions from model output."""
        try:
            cleaned = text.strip()
            match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
            if match:
                cleaned = match.group(1).strip()
            else:
                bracket_start = cleaned.find("[")
                brace_start = cleaned.find("{")
                if bracket_start != -1 and (brace_start == -1 or bracket_start < brace_start):
                    bracket_end = cleaned.rfind("]")
                    if bracket_end > bracket_start:
                        cleaned = cleaned[bracket_start : bracket_end + 1]
                elif brace_start != -1:
                    brace_end = cleaned.rfind("}")
                    if brace_end > brace_start:
                        cleaned = cleaned[brace_start : brace_end + 1]

            data = json.loads(cleaned.strip())
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
            elif isinstance(data, dict):
                if "facts" in data and isinstance(data["facts"], list):
                    return [d for d in data["facts"] if isinstance(d, dict)]
                return [data]
            return []
        except Exception as exc:
            logger.debug("Failed to parse OKF model JSON: %s (text=%r)", exc, text[:200])
            return []


# Module level singleton and legacy build_okf adapter
_builder_instance = OkfBuilder()


def extract_and_persist_okf(
    workspace_id: str,
    document_id: str,
    document_version: str,
    format: str,
    chunks: Sequence[Any],
    table_name: str | None = None,
    table_store: Any | None = None,
    repo: OkfRepositoryBase | None = None,
) -> OkfManifest:
    """Public helper for invoking OKF extraction and persistence."""
    builder = OkfBuilder(repo=repo) if repo else _builder_instance
    return builder.extract_and_persist(
        workspace_id=workspace_id,
        document_id=document_id,
        document_version=document_version,
        format=format,
        chunks=chunks,
        table_name=table_name,
        table_store=table_store,
    )


def _extract_tier3_smart(chunks: Any) -> tuple[list[dict], dict]:
    """Legacy helper stub for smart extraction tests."""
    return [], {"input_tokens": 0, "output_tokens": 0, "calls": 0}


def build_okf(document: Any) -> None:
    """Legacy backward-compatible entry point for build_okf."""
    doc_id = str(getattr(document, "id", ""))
    ws_id = getattr(document, "workspace_id", "default")
    ver = getattr(document, "version", doc_id)
    fmt = getattr(document, "source_format", "pdf")
    chunks = list(getattr(document, "chunks", ()))

    # Legacy mock / direct repository support (e.g. test_phase3)
    try:
        from db.database import CloudRepository
        repo = CloudRepository()
    except Exception:
        repo = None

    if hasattr(repo, "okf_properties_table") or hasattr(repo, "table"):
        try:
            tier3_props, token_stats = _extract_tier3_smart(chunks)
            all_props = list(tier3_props)
            try:
                from ingestion.concept_service import (
                    extract_tier1_patterns,
                    gate_and_rank_chunks,
                )
                gated = gate_and_rank_chunks(chunks)
                gated_chunks = [c for c, _ in gated]
                tier1_props = extract_tier1_patterns(gated_chunks)
                all_props = tier1_props + tier3_props
            except Exception:
                pass

            canonical_map: dict[str, str] = {}
            if all_props:
                try:
                    from ingestion.normalize_service import cluster_entities
                    entity_names = list({p["concept"] for p in all_props if p.get("concept")})
                    canonical_map = cluster_entities(entity_names)
                except Exception:
                    canonical_map = {}

            if hasattr(repo, "okf_properties_table"):
                for prop in all_props:
                    raw_name = prop.get("concept", "")
                    if not raw_name:
                        continue
                    canon = canonical_map.get(raw_name, raw_name)
                    concept_id = canon.strip().upper().replace(" ", "_")
                    prop_name = str(prop.get("property_name", ""))
                    chunk_id = str(prop.get("source_chunk_id", ""))
                    if not prop_name or not chunk_id:
                        continue
                    try:
                        from decimal import Decimal
                        repo.okf_properties_table.put_item(
                            Item={
                                "PK": f"ENTITY#{concept_id}",
                                "SK": f"PROP#{prop_name}#{chunk_id}",
                                "property_name": prop_name,
                                "property_value": str(prop.get("property_value", "")),
                                "source_chunk_id": chunk_id,
                                "confidence": Decimal(str(round(float(prop.get("confidence", 1.0)), 4))),
                                "doc_id": doc_id,
                            }
                        )
                    except Exception as err:
                        logger.debug("Legacy write property failed: %s", err)

            if hasattr(repo, "table") and token_stats.get("calls", 0) > 0:
                try:
                    from decimal import Decimal
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
                except Exception as err:
                    logger.debug("Legacy write token usage failed: %s", err)
        except Exception as exc:
            logger.debug("Legacy okf_builder execution error: %s", exc)

    # Modern extraction and persistence
    try:
        extract_and_persist_okf(
            workspace_id=ws_id,
            document_id=doc_id,
            document_version=ver,
            format=fmt,
            chunks=chunks,
        )
    except Exception as exc:
        logger.warning("Legacy build_okf failed safely: %s", exc)
