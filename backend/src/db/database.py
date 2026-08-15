import json
import os
import uuid
import logging
import time
from decimal import Decimal
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from schemas.models import Chunk, Document
from config import settings

logger = logging.getLogger(__name__)

_IN_MEMORY_DOCS: dict[str, Document] = {}
_IN_MEMORY_CHUNKS: dict[str, Chunk] = {}

class CloudRepository:
    """Repository implementation utilizing AWS DynamoDB for document/chunk storage
    and Qdrant Cloud for vector similarity search.
    """
    def __init__(self, dsn: str | None = None):
        self.table_name = settings.DYNAMODB_TABLE_NAME
        from aws.infra import get_resource
        self.dynamodb = get_resource('dynamodb')
        self.table = self.dynamodb.Table(self.table_name)
        
        self.qclient = QdrantClient(url=settings.QDRANT_URL, api_key=settings.QDRANT_API_KEY, timeout=60.0)
        self.collection_name = "kre_chunks"
        
        self.okf_entities_table = self.dynamodb.Table("okf_entities")
        self.okf_properties_table = self.dynamodb.Table("okf_properties")
        self.okf_relations_table = self.dynamodb.Table("okf_relations")
        
    def initialize(self) -> None:
        try:
            if not self.qclient.collection_exists(self.collection_name):
                self.qclient.create_collection(
                    collection_name=self.collection_name,
                    vectors_config={
                        "embedding_fast": qmodels.VectorParams(size=384, distance=qmodels.Distance.COSINE),
                        "embedding_full": qmodels.VectorParams(size=1024, distance=qmodels.Distance.COSINE)
                    }
                )
                self.qclient.create_payload_index(collection_name=self.collection_name, field_name="page_number", field_schema=qmodels.PayloadSchemaType.INTEGER)
                self.qclient.create_payload_index(collection_name=self.collection_name, field_name="document_id", field_schema=qmodels.PayloadSchemaType.KEYWORD)
                self.qclient.create_payload_index(collection_name=self.collection_name, field_name="original_id", field_schema=qmodels.PayloadSchemaType.KEYWORD)
        except Exception as e:
            logger.warning("Qdrant init error: %s", e)
            
        try:
            self.dynamodb.create_table(
                TableName=self.table_name,
                KeySchema=[
                    {'AttributeName': 'PK', 'KeyType': 'HASH'},
                    {'AttributeName': 'SK', 'KeyType': 'RANGE'}
                ],
                AttributeDefinitions=[
                    {'AttributeName': 'PK', 'AttributeType': 'S'},
                    {'AttributeName': 'SK', 'AttributeType': 'S'}
                ],
                BillingMode='PAY_PER_REQUEST'
            )
            self.table.meta.client.get_waiter('table_exists').wait(TableName=self.table_name)
        except Exception as e:
            if "ResourceInUseException" not in str(e):
                logger.warning("DynamoDB init error: %s", e)
                
        try:
            self.dynamodb.create_table(
                TableName="okf_entities",
                KeySchema=[
                    {'AttributeName': 'PK', 'KeyType': 'HASH'},
                    {'AttributeName': 'SK', 'KeyType': 'RANGE'}
                ],
                AttributeDefinitions=[
                    {'AttributeName': 'PK', 'AttributeType': 'S'},
                    {'AttributeName': 'SK', 'AttributeType': 'S'}
                ],
                BillingMode='PAY_PER_REQUEST'
            )
            self.okf_entities_table.meta.client.get_waiter('table_exists').wait(TableName="okf_entities")
        except Exception as e:
            if "ResourceInUseException" not in str(e):
                logger.warning("DynamoDB okf_entities init error: %s", e)
                
        try:
            self.dynamodb.create_table(
                TableName="okf_properties",
                KeySchema=[
                    {'AttributeName': 'PK', 'KeyType': 'HASH'},
                    {'AttributeName': 'SK', 'KeyType': 'RANGE'}
                ],
                AttributeDefinitions=[
                    {'AttributeName': 'PK', 'AttributeType': 'S'},
                    {'AttributeName': 'SK', 'AttributeType': 'S'}
                ],
                BillingMode='PAY_PER_REQUEST'
            )
            self.okf_properties_table.meta.client.get_waiter('table_exists').wait(TableName="okf_properties")
        except Exception as e:
            if "ResourceInUseException" not in str(e):
                logger.warning("DynamoDB okf_properties init error: %s", e)

    def save(self, document: Document) -> None:
        # Hard integrity validation: Every chunk must have valid non-null, non-zero, non-padded embeddings
        for chunk in document.chunks:
            if chunk.embedding_fast is None or len(chunk.embedding_fast) != 384:
                raise ValueError(
                    f"DataIntegrityError: Chunk {chunk.id} has invalid embedding_fast "
                    f"(expected 384-dim, got {len(chunk.embedding_fast) if chunk.embedding_fast else None})"
                )
            if chunk.embedding_full is None or len(chunk.embedding_full) != 1024:
                raise ValueError(
                    f"DataIntegrityError: Chunk {chunk.id} has invalid embedding_full "
                    f"(expected 1024-dim, got {len(chunk.embedding_full) if chunk.embedding_full else None})"
                )
            if all(x == 0.0 for x in chunk.embedding_fast):
                raise ValueError(f"DataIntegrityError: Chunk {chunk.id} has all-zero embedding_fast")
            if all(x == 0.0 for x in chunk.embedding_full):
                raise ValueError(f"DataIntegrityError: Chunk {chunk.id} has all-zero embedding_full")
            if all(x == 0.0 for x in chunk.embedding_full[384:]):
                raise ValueError(f"DataIntegrityError: Chunk {chunk.id} has zero-padded embedding_full (dims 384:1024 are all zero)")

        _IN_MEMORY_DOCS[str(document.id)] = document
        for chunk in document.chunks:
            _IN_MEMORY_CHUNKS[str(chunk.id)] = chunk

        if settings.ENVIRONMENT == "test":
            return
            
        try:
            with self.table.batch_writer() as batch:
                batch.put_item(Item={
                    'PK': f"DOC#{document.id}",
                    'SK': f"DOC#{document.id}",
                    'id': str(document.id),
                    'filename': document.filename,
                    'source_format': document.source_format
                })
                for chunk in document.chunks:
                    item = {
                        'PK': f"DOC#{chunk.document_id}",
                        'SK': f"CHUNK#{chunk.id}",
                        'id': str(chunk.id),
                        'document_id': str(chunk.document_id),
                        'source_format': chunk.source_format,
                        'text': chunk.text,
                        'element_type': chunk.element_type,
                        'page_number': chunk.page_number,
                        'section_path': json.dumps(list(chunk.section_path)),
                        'bounding_box': json.dumps(chunk.bounding_box) if chunk.bounding_box else None,
                        'location_reference': chunk.location_reference,
                        'metadata': json.dumps(chunk.metadata) if chunk.metadata else None,
                        'structural_weight': str(chunk.structural_weight),
                        'provider': chunk.provider,
                        'image_s3_keys': json.dumps(list(chunk.image_s3_keys))
                    }
                    item = {k: v for k, v in item.items() if v is not None}
                    batch.put_item(Item=item)
            
            points = []
            for chunk in document.chunks:
                vectors = {
                    "embedding_fast": chunk.embedding_fast,
                    "embedding_full": chunk.embedding_full,
                }
                    
                qdrant_uuid = str(uuid.uuid5(uuid.NAMESPACE_OID, str(chunk.id)))
                
                payload = {
                    "original_id": str(chunk.id),
                    "document_id": str(chunk.document_id),
                    "page_number": chunk.page_number
                }
                points.append(qmodels.PointStruct(
                    id=qdrant_uuid,
                    vector=vectors,
                    payload=payload
                ))
            
            if points:
                batch_size = 50  # Qdrant cloud rate limit mitigation
                for i in range(0, len(points), batch_size):
                    batch = points[i:i+batch_size]
                    for attempt in range(3):
                        try:
                            self.qclient.upsert(
                                collection_name=self.collection_name,
                                points=batch,
                                wait=True
                            )
                            time.sleep(0.2)
                            break
                        except Exception as qe:
                            if "429" in str(qe) or "Too Many Requests" in str(qe) or "timed out" in str(qe).lower():
                                logger.warning("Qdrant rate limit/timeout hit (attempt %d/3). Sleeping 5s...", attempt + 1)
                                time.sleep(5)
                            else:
                                raise qe
                    else:
                        raise RuntimeError("Qdrant upsert failed after 3 attempts")
        except Exception as e:
            logger.error("Failed to save to cloud database: %s", e)
            raise RuntimeError(f"Failed to save document to cloud database: {e}") from e

    def _parse_chunk(self, row: dict) -> Chunk:
        sw = float(row.get('structural_weight', 1.0))
        img_keys_raw = row.get('image_s3_keys')
        img_keys = tuple(json.loads(img_keys_raw)) if img_keys_raw else ()
        
        return Chunk(
            row['id'],
            row['document_id'],
            row['source_format'],
            row['text'],
            row.get('element_type', 'text'),
            int(row['page_number']) if row.get('page_number') is not None else None,
            tuple(json.loads(row['section_path'])) if row.get('section_path') else (),
            json.loads(row['bounding_box']) if row.get('bounding_box') else None,
            row.get('location_reference', ""),
            json.loads(row['metadata']) if row.get('metadata') else None,
            sw,
            row.get('provider', ""),
            None,
            None,
            img_keys
        )

    def get(self, document_id: str) -> Document | None:
        if settings.ENVIRONMENT == "test":
            return _IN_MEMORY_DOCS.get(document_id)
            
        from boto3.dynamodb.conditions import Key
        try:
            response = self.table.query(
                KeyConditionExpression=Key('PK').eq(f"DOC#{document_id}")
            )
            items = response.get('Items', [])
            if not items:
                return None
            
            doc_item = next((item for item in items if item['SK'].startswith("DOC#")), None)
            if not doc_item:
                return None
                
            chunk_items = [item for item in items if item['SK'].startswith("CHUNK#")]
            chunks = [self._parse_chunk(row) for row in chunk_items]
            return Document(str(doc_item['id']), doc_item['filename'], doc_item['source_format'], tuple(chunks))
        except Exception:
            return None

    def get_all_chunks(self, document_ids: list[str] | None = None) -> list[Chunk]:
        if _IN_MEMORY_CHUNKS:
            if document_ids:
                return [c for c in _IN_MEMORY_CHUNKS.values() if str(c.document_id) in document_ids]
            return list(_IN_MEMORY_CHUNKS.values())

        if settings.ENVIRONMENT == "test":
            return []

        from boto3.dynamodb.conditions import Attr
        chunks = []
        if document_ids:
            for d in document_ids:
                doc = self.get(d)
                if doc:
                    chunks.extend(doc.chunks)
        else:
            try:
                response = self.table.scan(FilterExpression=Attr('SK').begins_with("CHUNK#"))
                items = response.get('Items', [])
                for row in items:
                    c = self._parse_chunk(row)
                    chunks.append(c)
                    _IN_MEMORY_CHUNKS[str(c.id)] = c
                while 'LastEvaluatedKey' in response:
                    response = self.table.scan(
                        FilterExpression=Attr('SK').begins_with("CHUNK#"),
                        ExclusiveStartKey=response['LastEvaluatedKey']
                    )
                    items = response.get('Items', [])
                    for row in items:
                        c = self._parse_chunk(row)
                        chunks.append(c)
                        _IN_MEMORY_CHUNKS[str(c.id)] = c
            except Exception:
                pass
        return chunks

    def search_vector(
        self,
        query_embedding: list[float],
        embedding_column: str = "embedding_full",
        document_ids: list[str] | None = None,
        candidate_page_ids: list[int] | None = None,
        candidate_chunk_ids: list[str] | None = None,
        limit: int = 10,
    ) -> list[tuple[Chunk, float]]:
        if settings.ENVIRONMENT == "test":
            import numpy as np
            results = []
            q_vec = np.array(query_embedding)
            has_page_constraint = candidate_page_ids is not None and len(candidate_page_ids) > 0
            has_chunk_constraint = candidate_chunk_ids is not None and len(candidate_chunk_ids) > 0
            
            for chunk in _IN_MEMORY_CHUNKS.values():
                if document_ids and str(chunk.document_id) not in document_ids:
                    continue
                if has_page_constraint or has_chunk_constraint:
                    page_match = has_page_constraint and chunk.page_number in candidate_page_ids
                    chunk_match = has_chunk_constraint and str(chunk.id) in candidate_chunk_ids
                    if not (page_match or chunk_match):
                        continue
                c_vec = chunk.embedding_fast if embedding_column == "embedding_fast" else chunk.embedding_full
                if c_vec:
                    score = float(np.dot(q_vec, np.array(c_vec)))
                    results.append((chunk, score))
            results.sort(key=lambda x: x[1], reverse=True)
            return results[:limit]
            
        if embedding_column not in ("embedding_fast", "embedding_full"):
            raise ValueError(f"Invalid embedding_column: {embedding_column}")
            
        must_filters = []
        if document_ids:
            must_filters.append(qmodels.FieldCondition(
                key="document_id",
                match=qmodels.MatchAny(any=document_ids)
            ))
        should_filters = []
        if candidate_page_ids:
            should_filters.append(qmodels.FieldCondition(
                key="page_number",
                match=qmodels.MatchAny(any=candidate_page_ids)
            ))
        if candidate_chunk_ids:
            should_filters.append(qmodels.FieldCondition(
                key="original_id",
                match=qmodels.MatchAny(any=candidate_chunk_ids)
            ))
            
        if should_filters:
            must_filters.append(qmodels.Filter(should=should_filters))
            
        qfilter = qmodels.Filter(must=must_filters) if must_filters else None
        
        try:
            response = self.qclient.query_points(
                collection_name=self.collection_name,
                query=query_embedding,
                using=embedding_column,
                query_filter=qfilter,
                limit=limit,
                with_payload=True
            )
            results = response.points
            logger.info("Qdrant returned %d results", len(results))
        except Exception as e:
            logger.error("Qdrant search failed: %s", e)
            return []
            
        keys = []
        for hit in results:
            original_id = hit.payload.get("original_id")
            doc_id = hit.payload.get("document_id")
            if original_id and doc_id:
                keys.append({"PK": f"DOC#{doc_id}", "SK": f"CHUNK#{original_id}"})
                
        if not keys:
            return []
            
        try:
            resp = self.dynamodb.meta.client.batch_get_item(
                RequestItems={
                    self.table_name: {'Keys': keys}
                }
            )
            items = resp.get('Responses', {}).get(self.table_name, [])
            item_map = {item['id']: self._parse_chunk(item) for item in items}
        except Exception as e:
            logger.warning("DynamoDB batch_get_item failed: %s", e)
            item_map = {}
            
        final_results = []
        for hit in results:
            original_id = hit.payload.get("original_id")
            chunk = item_map.get(str(original_id))
            if chunk:
                final_results.append((chunk, hit.score))
        return final_results

    def check_semantic_cache(
        self,
        query_embedding: list[float],
        doc_scope_hash: str,
        provider: str
    ) -> str | None:
        try:
            dim = len(query_embedding)
            col = "kre_cache_fast" if dim == 384 else "kre_cache_full"
            response = self.qclient.query_points(
                collection_name=col,
                query=query_embedding,
                query_filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(key="doc_scope_hash", match=qmodels.MatchValue(value=doc_scope_hash)),
                        qmodels.FieldCondition(key="provider", match=qmodels.MatchValue(value=provider))
                    ]
                ),
                limit=1,
                score_threshold=0.95,
                with_payload=True
            )
            if response.points:
                return response.points[0].payload.get("redis_key")
        except Exception:
            pass
        return None

    def save_semantic_cache(
        self,
        redis_key: str,
        query_embedding: list[float],
        doc_scope_hash: str,
        provider: str
    ) -> None:
        try:
            dim = len(query_embedding)
            col = "kre_cache_fast" if dim == 384 else "kre_cache_full"
            if not self.qclient.collection_exists(col):
                self.qclient.create_collection(
                    collection_name=col,
                    vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE)
                )
            
            self.qclient.upsert(
                collection_name=col,
                points=[
                    qmodels.PointStruct(
                        id=str(uuid.uuid4()),
                        vector=query_embedding,
                        payload={
                            "redis_key": redis_key,
                            "doc_scope_hash": doc_scope_hash,
                            "provider": provider
                        }
                    )
                ]
            )
        except Exception:
            pass

    def get_okf_properties(self, entities: list[str]) -> list[dict]:
        from boto3.dynamodb.conditions import Key
        results = []
        for entity in entities:
            try:
                pk = f"ENTITY#{entity.strip().upper()}"
                response = self.okf_properties_table.query(
                    KeyConditionExpression=Key('PK').eq(pk)
                )
                for item in response.get('Items', []):
                    results.append({
                        "concept": entity,
                        "property_name": item.get('property_name'),
                        "property_value": item.get('property_value'),
                        "source_chunk_id": item.get('source_chunk_id'),
                        "confidence": float(item.get('confidence', 1.0))
                    })
            except Exception as e:
                logger.warning("DynamoDB OKF property lookup failed for %s: %s", entity, e)
        return results

    def expand_graph(self, start_entities: list[str], max_hops: int = 2) -> list[dict]:
        from boto3.dynamodb.conditions import Key
        results = []
        visited = set()
        queue = [(e.strip().upper(), 1) for e in start_entities]
        
        while queue and len(results) < 40:
            current_entity, hop = queue.pop(0)
            if current_entity in visited or hop > max_hops:
                continue
            visited.add(current_entity)
            
            try:
                pk = f"ENTITY#{current_entity}"
                response = self.okf_entities_table.query(
                    KeyConditionExpression=Key('PK').eq(pk) & Key('SK').begins_with("REL#")
                )
                for item in response.get('Items', []):
                    weight = float(item.get('relation_weight', 0.0))
                    if weight < 0.3:
                        continue
                    to_concept = item.get('to_concept_id')
                    results.append({
                        "concept_id": to_concept,
                        "relation_type": item.get('relation_type'),
                        "relation_weight": weight,
                        "hop": hop
                    })
                    if hop < max_hops:
                        queue.append((to_concept, hop + 1))
            except Exception as e:
                logger.warning("DynamoDB OKF graph traversal failed for %s: %s", current_entity, e)
                
        return results[:40]
