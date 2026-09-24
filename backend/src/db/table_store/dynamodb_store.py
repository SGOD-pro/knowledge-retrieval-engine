"""DynamoDB table store implementation for production structured table storage.

Stores structured table schemas, rows, ingestion manifests, and checkpoints
in DynamoDB (`kre-table`) using versioned keys and atomic TransactWriteItems batches.
"""

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import logging
from typing import Any
import uuid

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from config import settings
from db.table_store.base import AggregateOp, Predicate, PredicateOp
from schemas.structured_table import (
    ColumnDefinition,
    HeaderTopology,
    InferredDtype,
    StructuredTable,
    TableCell,
    TableRow,
    TableSchema,
)

logger = logging.getLogger(__name__)


def _schema_to_json(schema: TableSchema) -> str:
    cols = []
    for c in schema.columns:
        cols.append({
            "col_index": c.col_index,
            "name": c.name,
            "path_hierarchy": list(c.path_hierarchy),
            "inferred_dtype": c.inferred_dtype.value,
            "unit": c.unit,
            "sample_values": list(c.sample_values),
            "null_ratio": c.null_ratio,
        })
    data = {
        "columns": cols,
        "header_rows": list(schema.header_rows),
        "header_topology": schema.header_topology.value,
        "confidence": schema.confidence,
    }
    return json.dumps(data)


def _json_to_schema(s: str) -> TableSchema:
    data = json.loads(s)
    cols = []
    for c in data.get("columns", []):
        cols.append(ColumnDefinition(
            col_index=c["col_index"],
            name=c["name"],
            path_hierarchy=tuple(c.get("path_hierarchy", ())),
            inferred_dtype=InferredDtype(c.get("inferred_dtype", "string")),
            unit=c.get("unit"),
            sample_values=tuple(c.get("sample_values", ())),
            null_ratio=c.get("null_ratio", 0.0),
        ))
    return TableSchema(
        columns=tuple(cols),
        header_rows=tuple(data.get("header_rows", (0,))),
        header_topology=HeaderTopology(data.get("header_topology", "single_row")),
        confidence=data.get("confidence", 1.0),
    )


def _cells_to_json(cells: tuple[TableCell, ...] | list[TableCell]) -> str:
    raw_list = []
    for c in cells:
        val = c.normalized_value
        if isinstance(val, Decimal):
            norm_val = str(val)
        else:
            norm_val = val
        raw_list.append({
            "cell_id": c.cell_id,
            "row_index": c.row_index,
            "col_index": c.col_index,
            "coordinate": c.coordinate,
            "raw_value": str(c.raw_value) if c.raw_value is not None else None,
            "normalized_value": norm_val,
            "inferred_dtype": c.inferred_dtype.value if hasattr(c.inferred_dtype, "value") else str(c.inferred_dtype),
            "unit": c.unit,
        })
    return json.dumps(raw_list)


def _json_to_cells(s: str) -> tuple[TableCell, ...]:
    raw_list = json.loads(s)
    cells = []
    for d in raw_list:
        norm = d.get("normalized_value")
        if norm is not None and d.get("inferred_dtype") in ("decimal", "integer", "currency", "percentage"):
            try:
                norm = Decimal(str(norm))
            except Exception:
                pass
        cells.append(TableCell(
            cell_id=d.get("cell_id", ""),
            row_index=d.get("row_index", 0),
            col_index=d.get("col_index", 0),
            coordinate=d.get("coordinate", ""),
            raw_value=d.get("raw_value"),
            normalized_value=norm,
            inferred_dtype=InferredDtype(d.get("inferred_dtype", "string")),
            unit=d.get("unit"),
        ))
    return tuple(cells)


class DynamoDBTableStore:
    """Production TableStore implementation backed by DynamoDB (kre-table).

    Key Patterns:
      Schema Item:
        PK = TABLE#{workspace_id}#{table_id}#V#{version}    SK = SCHEMA
      Row Items:
        PK = TABLE#{workspace_id}#{table_id}#V#{version}    SK = ROW#{row_index:010d}
      Checkpoint Item:
        PK = INGESTION#{workspace_id}#{document_id}#V#{version}  SK = CHECKPOINT
      Active Version Pointer:
        PK = TABLE#{workspace_id}#{table_id}                SK = ACTIVE_VERSION
      Manifest Items:
        PK = INGESTION#{workspace_id}#{document_id}         SK = MANIFEST#V#{version}
        PK = INGESTION#{workspace_id}#{document_id}         SK = MANIFEST
      Workspace Catalogue:
        PK = WORKSPACE#{workspace_id}                       SK = TABLE#{table_id}
    """

    def __init__(
        self,
        table_name: str | None = None,
        client: Any = None,
        resource: Any = None,
    ) -> None:
        self.table_name = table_name or settings.DYNAMODB_TABLE_NAME
        self._client = client
        self._resource = resource

    @property
    def client(self):
        if self._client is None:
            from aws.infra import get_client
            self._client = get_client("dynamodb")
        return self._client

    @property
    def resource(self):
        if self._resource is None:
            from aws.infra import get_resource
            self._resource = get_resource("dynamodb")
        return self._resource

    @property
    def table(self):
        return self.resource.Table(self.table_name)

    # -------------------------------------------------------------------------
    # Schema and Versioning
    # -------------------------------------------------------------------------

    def store_schema(
        self,
        table_id: str,
        workspace_id: str,
        schema: TableSchema,
        document_id: str,
        document_version: str,
        parser_version: str = "1.0.0",
        schema_version: str = "",
    ) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must not be empty")

        now = datetime.now(timezone.utc).isoformat()
        pk = f"TABLE#{workspace_id}#{table_id}#V#{document_version}"
        col_names = [c.name for c in schema.columns]

        item = {
            "PK": pk,
            "SK": "SCHEMA",
            "table_id": table_id,
            "workspace_id": workspace_id,
            "document_id": document_id,
            "document_version": document_version,
            "schema_json": _schema_to_json(schema),
            "col_count": len(schema.columns),
            "col_names": col_names,
            "parser_version": parser_version,
            "schema_version": schema_version,
            "ingested_at_utc": now,
        }
        self.table.put_item(Item=item)
        logger.info("DynamoDBTableStore.store_schema table=%s doc_ver=%s cols=%d",
                    table_id, document_version, len(col_names))

    def write_row_batch_with_checkpoint(
        self,
        table_id: str,
        workspace_id: str,
        document_id: str,
        version: str,
        rows: list[TableRow],
        checkpoint_batch: int,
        source_rows: int,
        attempted_rows: int,
        persisted_rows: int,
        rejected_rows: int,
        parser_version: str,
        schema_version: str,
        content_hash: str,
    ) -> None:
        """Write a batch of rows and update ingestion checkpoint atomically.

        DynamoDB TransactWriteItems supports up to 100 actions and 4MB per call.
        We batch at most 99 rows + 1 checkpoint update per transaction.
        Checkpoints advance ONLY when the transaction succeeds!
        """
        if not workspace_id:
            raise ValueError("workspace_id must not be empty")

        now = datetime.now(timezone.utc).isoformat()
        table_pk = f"TABLE#{workspace_id}#{table_id}#V#{version}"
        cp_pk = f"INGESTION#{workspace_id}#{document_id}#V#{version}"

        # If batch has more than 99 rows, chunk into slices of 99
        chunk_size = 99
        for start_idx in range(0, max(1, len(rows)), chunk_size):
            chunk_rows = rows[start_idx: start_idx + chunk_size]
            transact_items = []

            for r in chunk_rows:
                sk = f"ROW#{r.row_index:010d}"
                row_item = {
                    "PK": {"S": table_pk},
                    "SK": {"S": sk},
                    "row_id": {"S": r.row_id},
                    "row_index": {"N": str(r.row_index)},
                    "cells_json": {"S": _cells_to_json(r.cells)},
                    "is_header": {"BOOL": r.is_header},
                    "is_subtotal": {"BOOL": r.is_subtotal},
                    "is_empty": {"BOOL": r.is_empty},
                }
                transact_items.append({
                    "Put": {
                        "TableName": self.table_name,
                        "Item": row_item,
                    }
                })

            # Checkpoint action (advances only with transaction commit)
            cp_item = {
                "PK": {"S": cp_pk},
                "SK": {"S": "CHECKPOINT"},
                "table_id": {"S": table_id},
                "document_id": {"S": document_id},
                "workspace_id": {"S": workspace_id},
                "version": {"S": version},
                "checkpoint_batch": {"N": str(checkpoint_batch)},
                "source_rows": {"N": str(source_rows)},
                "attempted_rows": {"N": str(attempted_rows)},
                "persisted_rows": {"N": str(persisted_rows)},
                "rejected_rows": {"N": str(rejected_rows)},
                "parser_version": {"S": parser_version},
                "schema_version": {"S": schema_version},
                "content_hash": {"S": content_hash},
                "updated_at_utc": {"S": now},
            }
            transact_items.append({
                "Put": {
                    "TableName": self.table_name,
                    "Item": cp_item,
                }
            })

            try:
                self.client.transact_write_items(TransactItems=transact_items)
            except ClientError as e:
                logger.error("DynamoDBTableStore transact_write_items failed: %s", e)
                raise

    def publish_active_version(
        self,
        table_id: str,
        workspace_id: str,
        document_id: str,
        version: str,
        manifest: dict,
    ) -> None:
        """Publish active version pointer, catalogue item, and immutable manifest after validation."""
        if not workspace_id:
            raise ValueError("workspace_id must not be empty")

        now = datetime.now(timezone.utc).isoformat()

        # 1. Active Version Pointer: PK = TABLE#{workspace_id}#{table_id}, SK = ACTIVE_VERSION
        active_item = {
            "PK": f"TABLE#{workspace_id}#{table_id}",
            "SK": "ACTIVE_VERSION",
            "active_version": version,
            "table_id": table_id,
            "document_id": document_id,
            "workspace_id": workspace_id,
            "coverage_complete": bool(manifest.get("coverage_complete", False)),
            "processing_finished": bool(manifest.get("processing_finished", False)),
            "source_rows": int(manifest.get("source_rows", 0)),
            "persisted_rows": int(manifest.get("persisted_rows", 0)),
            "rejected_rows": int(manifest.get("rejected_rows", 0)),
            "published_at_utc": now,
        }
        self.table.put_item(Item=active_item)

        # 2. Immutable Manifest: PK = INGESTION#{workspace_id}#{document_id}, SK = MANIFEST#V#{version}
        # and active manifest pointer SK = MANIFEST
        serialized_manifest = dict(manifest)
        serialized_manifest["updated_at_utc"] = now

        man_v_item = {
            "PK": f"INGESTION#{workspace_id}#{document_id}",
            "SK": f"MANIFEST#V#{version}",
            **serialized_manifest,
        }
        self.table.put_item(Item=man_v_item)

        man_latest_item = {
            "PK": f"INGESTION#{workspace_id}#{document_id}",
            "SK": "MANIFEST",
            **serialized_manifest,
        }
        self.table.put_item(Item=man_latest_item)

        # 3. Workspace Catalogue: PK = WORKSPACE#{workspace_id}, SK = TABLE#{table_id}
        cat_item = {
            "PK": f"WORKSPACE#{workspace_id}",
            "SK": f"TABLE#{table_id}",
            "table_id": table_id,
            "document_id": document_id,
            "active_version": version,
            "coverage_complete": bool(manifest.get("coverage_complete", False)),
            "updated_at_utc": now,
        }
        self.table.put_item(Item=cat_item)

        logger.info("DynamoDBTableStore published active version table=%s ver=%s coverage_complete=%s",
                    table_id, version, manifest.get("coverage_complete"))

    def get_active_version(self, table_id: str, workspace_id: str) -> str | None:
        if not workspace_id:
            return None
        pk = f"TABLE#{workspace_id}#{table_id}"
        resp = self.table.get_item(Key={"PK": pk, "SK": "ACTIVE_VERSION"})
        item = resp.get("Item")
        if item:
            return item.get("active_version")
        return None

    def list_tables(self, workspace_id: str) -> list[str]:
        """List all active tables for workspace via catalogue lookup — zero table scans."""
        if not workspace_id:
            return []
        pk = f"WORKSPACE#{workspace_id}"
        resp = self.table.query(
            KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("TABLE#")
        )
        items = resp.get("Items", [])
        tables = [item["table_id"] for item in items if "table_id" in item]
        return sorted(tables)

    def get_ingestion_manifest(
        self,
        document_id: str,
        workspace_id: str,
        version: str | None = None,
    ) -> dict | None:
        if not workspace_id or not document_id:
            return None
        pk = f"INGESTION#{workspace_id}#{document_id}"
        sk = f"MANIFEST#V#{version}" if version else "MANIFEST"
        resp = self.table.get_item(Key={"PK": pk, "SK": sk})
        item = resp.get("Item")
        if not item:
            return None
        # Clean DynamoDB types
        res = dict(item)
        res.pop("PK", None)
        res.pop("SK", None)
        return res

    def get_checkpoint(
        self,
        document_id: str,
        workspace_id: str,
        version: str,
    ) -> dict | None:
        if not workspace_id or not document_id:
            return None
        pk = f"INGESTION#{workspace_id}#{document_id}#V#{version}"
        resp = self.table.get_item(Key={"PK": pk, "SK": "CHECKPOINT"})
        item = resp.get("Item")
        if not item:
            return None
        res = dict(item)
        res.pop("PK", None)
        res.pop("SK", None)
        return res

    def has_coverage_complete_table(self, table_id: str, workspace_id: str) -> bool:
        if not workspace_id:
            return False
        pk = f"TABLE#{workspace_id}#{table_id}"
        resp = self.table.get_item(Key={"PK": pk, "SK": "ACTIVE_VERSION"})
        item = resp.get("Item")
        if item:
            return bool(item.get("coverage_complete", False))
        return False

    def get_table(
        self,
        table_id: str,
        workspace_id: str,
        version: str | None = None,
    ) -> StructuredTable | None:
        if not workspace_id:
            return None
        ver = version or self.get_active_version(table_id, workspace_id)
        if not ver:
            return None

        pk = f"TABLE#{workspace_id}#{table_id}#V#{ver}"
        resp = self.table.get_item(Key={"PK": pk, "SK": "SCHEMA"})
        item = resp.get("Item")
        if not item or "schema_json" not in item:
            return None

        schema = _json_to_schema(item["schema_json"])
        doc_id = item.get("document_id", "")
        # Note: does NOT load all rows into memory; returns metadata with empty rows
        # Use query_rows or execute_single_pass_query to access rows
        return StructuredTable(
            table_id=table_id,
            document_id=doc_id,
            sheet_name=None,
            page_number=None,
            schema=schema,
            rows=(),
            row_count=int(item.get("col_count", 0)),
            col_count=len(schema.columns),
        )

    # -------------------------------------------------------------------------
    # Single-Pass Paginated Query Execution
    # -------------------------------------------------------------------------

    def _matches_predicate(self, cells: tuple[TableCell, ...], pred: Predicate) -> bool:
        if pred.column_index < 0 or pred.column_index >= len(cells):
            return False
        cell = cells[pred.column_index]
        cell_val = cell.normalized_value if cell.normalized_value is not None else cell.raw_value
        target_val = pred.value

        # Normalize string comparison
        if isinstance(cell_val, str) and isinstance(target_val, str):
            c_str, t_str = cell_val.strip().lower(), target_val.strip().lower()
            if pred.op == PredicateOp.EQ:
                return c_str == t_str
            if pred.op == PredicateOp.NEQ:
                return c_str != t_str
            if pred.op == PredicateOp.CONTAINS:
                return t_str in c_str

        # Numeric comparison
        try:
            if pred.op in (PredicateOp.EQ, PredicateOp.NEQ, PredicateOp.GT, PredicateOp.LT, PredicateOp.GTE, PredicateOp.LTE):
                try:
                    num_cell = Decimal(str(cell_val).replace(",", "").strip())
                    num_target = Decimal(str(target_val).replace(",", "").strip())
                    if pred.op == PredicateOp.EQ:
                        return num_cell == num_target
                    if pred.op == PredicateOp.NEQ:
                        return num_cell != num_target
                    if pred.op == PredicateOp.GT:
                        return num_cell > num_target
                    if pred.op == PredicateOp.LT:
                        return num_cell < num_target
                    if pred.op == PredicateOp.GTE:
                        return num_cell >= num_target
                    if pred.op == PredicateOp.LTE:
                        return num_cell <= num_target
                except Exception:
                    pass

            if pred.op == PredicateOp.EQ:
                return cell_val == target_val
            if pred.op == PredicateOp.NEQ:
                return cell_val != target_val
            if pred.op == PredicateOp.GT:
                return cell_val > target_val
            if pred.op == PredicateOp.LT:
                return cell_val < target_val
            if pred.op == PredicateOp.GTE:
                return cell_val >= target_val
            if pred.op == PredicateOp.LTE:
                return cell_val <= target_val
            if pred.op == PredicateOp.IN:
                return cell_val in target_val
            if pred.op == PredicateOp.CONTAINS:
                return str(target_val).lower() in str(cell_val).lower()
        except Exception:
            return False
        return False

    def execute_single_pass_query(
        self,
        table_id: str,
        workspace_id: str,
        column_index: int | None,
        op: AggregateOp | None,
        predicates: list[Predicate] | None = None,
        limit: int | None = None,
        version: str | None = None,
    ) -> tuple[Decimal | None, int, list[str], list[int], str]:
        """Single-pass paginated query over DynamoDB rows.

        Computes count, aggregate accumulator, row_ids, row_indices, and digest
        in ONE pass over paginated items, correctly handling the 1MB page boundary.
        """
        ver = version or self.get_active_version(table_id, workspace_id)
        if not ver:
            return None, 0, [], [], hashlib.sha256(b"").hexdigest()[:16]

        table_pk = f"TABLE#{workspace_id}#{table_id}#V#{ver}"

        matched_ids: list[str] = []
        matched_indices: list[int] = []
        numeric_values: list[Decimal] = []
        total_count = 0

        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(table_pk) & Key("SK").begins_with("ROW#"),
        }

        while True:
            resp = self.table.query(**query_kwargs)
            items = resp.get("Items", [])

            for item in items:
                if item.get("is_header"):
                    continue

                cells = _json_to_cells(item.get("cells_json", "[]"))
                if predicates:
                    if not all(self._matches_predicate(cells, p) for p in predicates):
                        continue

                r_id = item.get("row_id", "")
                r_idx = int(item.get("row_index", 0))

                total_count += 1
                matched_ids.append(r_id)
                matched_indices.append(r_idx)

                if column_index is not None and column_index < len(cells):
                    cell = cells[column_index]
                    val = cell.normalized_value
                    if val is not None:
                        if isinstance(val, Decimal):
                            numeric_values.append(val)
                        else:
                            try:
                                clean_str = str(val).replace(",", "").strip()
                                numeric_values.append(Decimal(clean_str))
                            except Exception:
                                pass

                if limit is not None and total_count >= limit:
                    break

            if limit is not None and total_count >= limit:
                break

            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key

        digest = hashlib.sha256(",".join(sorted(matched_ids)).encode()).hexdigest()[:16]

        agg_val: Decimal | None = None
        if op == AggregateOp.COUNT:
            agg_val = Decimal(total_count)
        elif op and numeric_values:
            if op == AggregateOp.SUM:
                agg_val = sum(numeric_values)
            elif op == AggregateOp.AVG:
                agg_val = sum(numeric_values) / Decimal(len(numeric_values))
            elif op == AggregateOp.MIN:
                agg_val = min(numeric_values)
            elif op == AggregateOp.MAX:
                agg_val = max(numeric_values)

        return agg_val, total_count, matched_ids, matched_indices, digest

    def query_rows(
        self,
        table_id: str,
        workspace_id: str,
        predicates: list[Predicate] | None = None,
        limit: int | None = None,
        version: str | None = None,
    ) -> list[TableRow]:
        ver = version or self.get_active_version(table_id, workspace_id)
        if not ver:
            return []

        table_pk = f"TABLE#{workspace_id}#{table_id}#V#{ver}"
        results: list[TableRow] = []

        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(table_pk) & Key("SK").begins_with("ROW#"),
        }

        while True:
            resp = self.table.query(**query_kwargs)
            for item in resp.get("Items", []):
                if item.get("is_header"):
                    continue
                cells = _json_to_cells(item.get("cells_json", "[]"))
                if predicates and not all(self._matches_predicate(cells, p) for p in predicates):
                    continue

                results.append(TableRow(
                    row_id=item.get("row_id", ""),
                    row_index=int(item.get("row_index", 0)),
                    cells=cells,
                    is_header=item.get("is_header", False),
                    is_subtotal=item.get("is_subtotal", False),
                    is_empty=item.get("is_empty", False),
                ))
                if limit is not None and len(results) >= limit:
                    break

            if limit is not None and len(results) >= limit:
                break

            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key

        return results

    def aggregate(
        self,
        table_id: str,
        workspace_id: str,
        column_index: int,
        op: AggregateOp,
        predicates: list[Predicate] | None = None,
        version: str | None = None,
    ) -> Decimal | None:
        agg_val, _, _, _, _ = self.execute_single_pass_query(
            table_id=table_id,
            workspace_id=workspace_id,
            column_index=column_index,
            op=op,
            predicates=predicates,
            limit=None,
            version=version,
        )
        return agg_val

    def invalidate_document_version(
        self,
        document_id: str,
        workspace_id: str,
        version: str | None = None,
    ) -> int:
        """Prune rows, schemas, checkpoints, and manifests for document version."""
        deleted_count = 0
        # If version not specified, find all versions from manifests
        versions_to_delete = []
        if version:
            versions_to_delete.append(version)
        else:
            pk = f"INGESTION#{workspace_id}#{document_id}"
            resp = self.table.query(
                KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("MANIFEST#V#")
            )
            for it in resp.get("Items", []):
                sk = it["SK"]
                v = sk.replace("MANIFEST#V#", "")
                if v:
                    versions_to_delete.append(v)

        # For each version, delete rows and schema
        for v in versions_to_delete:
            # Find table_id from manifest
            man = self.get_ingestion_manifest(document_id, workspace_id, v)
            table_id = man.get("table_id") if man else None
            if table_id:
                table_pk = f"TABLE#{workspace_id}#{table_id}#V#{v}"
                # Query rows
                q_kwargs: dict[str, Any] = {"KeyConditionExpression": Key("PK").eq(table_pk)}
                while True:
                    r_resp = self.table.query(**q_kwargs)
                    for r_item in r_resp.get("Items", []):
                        self.table.delete_item(Key={"PK": r_item["PK"], "SK": r_item["SK"]})
                        deleted_count += 1
                    lk = r_resp.get("LastEvaluatedKey")
                    if not lk:
                        break
                    q_kwargs["ExclusiveStartKey"] = lk

                # Clear active pointer if this was active
                if self.get_active_version(table_id, workspace_id) == v:
                    self.table.delete_item(Key={"PK": f"TABLE#{workspace_id}#{table_id}", "SK": "ACTIVE_VERSION"})
                    self.table.delete_item(Key={"PK": f"WORKSPACE#{workspace_id}", "SK": f"TABLE#{table_id}"})

            # Delete checkpoint
            cp_pk = f"INGESTION#{workspace_id}#{document_id}#V#{v}"
            self.table.delete_item(Key={"PK": cp_pk, "SK": "CHECKPOINT"})
            # Delete version manifest
            self.table.delete_item(Key={"PK": f"INGESTION#{workspace_id}#{document_id}", "SK": f"MANIFEST#V#{v}"})

        if not version:
            self.table.delete_item(Key={"PK": f"INGESTION#{workspace_id}#{document_id}", "SK": "MANIFEST"})

        return deleted_count

    def store_table(self, table: StructuredTable, workspace_id: str) -> None:
        """Legacy compatibility store_table writing unversioned / default version."""
        v = str(uuid.uuid4())[:8]
        doc_id = table.document_id or f"doc_{table.table_id}"
        self.store_schema(table.table_id, workspace_id, table.schema, doc_id, v)
        self.write_row_batch_with_checkpoint(
            table_id=table.table_id,
            workspace_id=workspace_id,
            document_id=doc_id,
            version=v,
            rows=list(table.rows),
            checkpoint_batch=1,
            source_rows=len(table.rows),
            attempted_rows=len(table.rows),
            persisted_rows=len(table.rows),
            rejected_rows=0,
            parser_version="1.0.0",
            schema_version="legacy",
            content_hash=f"hash_{v}",
        )
        self.publish_active_version(
            table_id=table.table_id,
            workspace_id=workspace_id,
            document_id=doc_id,
            version=v,
            manifest={
                "table_id": table.table_id,
                "document_id": doc_id,
                "workspace_id": workspace_id,
                "document_version": v,
                "source_rows": len(table.rows),
                "persisted_rows": len(table.rows),
                "rejected_rows": 0,
                "coverage_complete": True,
                "processing_finished": True,
            },
        )

    def delete_table(self, table_id: str, workspace_id: str) -> bool:
        ver = self.get_active_version(table_id, workspace_id)
        if not ver:
            return False
        # Get document_id
        pk = f"TABLE#{workspace_id}#{table_id}"
        resp = self.table.get_item(Key={"PK": pk, "SK": "ACTIVE_VERSION"})
        doc_id = resp.get("Item", {}).get("document_id", "")
        self.invalidate_document_version(doc_id, workspace_id, ver)
        return True
