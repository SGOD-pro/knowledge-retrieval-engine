"""Schema Table Evidence Provider querying TableStore directly using resolved schemas."""

import logging
from typing import Any

from db.table_store.base import Predicate, TableStore
from schemas.evidence import (
    EvidenceConfidence,
    EvidenceEnvelope,
    EvidenceType,
    StructuredRowEvidence,
)
from services.retrieval.evidence_providers.base import (
    CriticalStorageFailure,
    EvidenceProvider,
)
from services.retrieval.schema_resolver import resolve_schema_concept

logger = logging.getLogger(__name__)


class SchemaTableEvidenceProvider:
    provider_name = "schema_table"

    def __init__(self, table_store: TableStore) -> None:
        self.table_store = table_store

    def retrieve(
        self,
        query: str,
        workspace_id: str,
        top_k: int = 20,
        **kwargs: Any,
    ) -> list[EvidenceEnvelope]:
        if not workspace_id:
            raise CriticalStorageFailure("workspace_id must be provided for schema table retrieval")

        table_id = kwargs.get("table_id")
        if not table_id:
            return []

        try:
            table = self.table_store.get_table(table_id, workspace_id)
        except Exception as exc:
            raise CriticalStorageFailure(f"TableStore retrieval failed: {exc}") from exc

        if not table:
            return []

        # Resolve query concept to table schema
        concept = kwargs.get("concept", query)
        res = resolve_schema_concept(concept, table.schema)
        if not res.bindings:
            return []

        binding = res.bindings[0]
        predicates = kwargs.get("predicates", [])
        rows = self.table_store.query_rows(table_id, workspace_id, predicates=predicates, limit=top_k)

        headers = tuple(c.name for c in table.schema.columns)
        envelopes: list[EvidenceEnvelope] = []

        for r in rows:
            values = tuple(
                (c.normalized_value if c.normalized_value is not None else c.raw_value)
                for c in r.cells
            )
            payload = StructuredRowEvidence(
                table_id=table.table_id,
                row_id=r.row_id,
                row_index=r.row_index,
                headers=headers,
                values=values,
                sheet_name=table.sheet_name,
            )
            kv_pairs = [f"{h}: {v}" for h, v in zip(headers, values) if v is not None]
            citation = f"Table {table.table_id} Row {r.row_index} [{', '.join(kv_pairs[:5])}]"

            env = EvidenceEnvelope(
                evidence_id=f"tab:{table.table_id}:{r.row_id}",
                evidence_type=EvidenceType.STRUCTURED_ROW,
                document_id=table.document_id,
                location={"table_id": table.table_id, "row_index": r.row_index},
                provider=self.provider_name,
                provider_score=binding.confidence,
                payload=payload,
                confidence=EvidenceConfidence(
                    retrieval_confidence=1.0,
                    schema_binding_confidence=binding.confidence,
                ),
                citation_text=citation,
                workspace_id=workspace_id,
            )
            envelopes.append(env)

        return envelopes
