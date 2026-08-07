import json
import os
import uuid
import boto3
from decimal import Decimal
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from kre.shared.models import Chunk, Document

_IN_MEMORY_DOCS: dict[str, Document] = {}
_IN_MEMORY_CHUNKS: dict[str, Chunk] = {}

class PostgresRepository:
    def __init__(self, dsn: str | None = None):
        # We ignore dsn and use dynamo/qdrant
        self.table_name = os.environ.get("DYNAMODB_TABLE_NAME", "kre-table")
        from kre.shared.aws import get_resource
        self.dynamodb = get_resource('dynamodb')
        self.table = self.dynamodb.Table(self.table_name)
        
        self.qdrant_url = os.environ.get("QDRANT_URL", "http://localhost:6333")
        self.qdrant_api_key = os.environ.get("QDRANT_API_KEY")
        self.qclient = QdrantClient(url=self.qdrant_url, api_key=self.qdrant_api_key)
        self.collection_name = "kre_chunks"
        
    def _connect(self):
        import contextlib
        @contextlib.contextmanager
        def dummy_connect():
            class DummyConnection:
                def execute(self, *args, **kwargs):
                    class DummyCursor:
                        def fetchall(self): return []
                    return DummyCursor()
            yield DummyConnection()
        return dummy_connect()
        
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
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Qdrant init error: %s", e)
            
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
                import logging
                logging.getLogger(__name__).warning("DynamoDB init error: %s", e)

    def save(self, document: Document) -> None:
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
                vectors = {}
                if chunk.embedding_fast:
                    vectors["embedding_fast"] = chunk.embedding_fast
                if chunk.embedding_full:
                    vectors["embedding_full"] = chunk.embedding_full
                
                if not vectors:
                    continue
                    
                import uuid
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
                self.qclient.upsert(
                    collection_name=self.collection_name,
                    points=points
                )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Failed to save to cloud: %s", e)

    def _parse_chunk(self, row: dict) -> Chunk:
        sw = float(row.get('structural_weight', 1.0))
        img_keys_raw = row.get('image_s3_keys')
        img_keys = tuple(json.loads(img_keys_raw)) if img_keys_raw else ()
        
        return Chunk(
            row['id'],
            row['document_id'],
            row['source_format'],
            row['text'],
            row['element_type'],
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
                chunks.extend([self._parse_chunk(row) for row in items])
                while 'LastEvaluatedKey' in response:
                    response = self.table.scan(
                        FilterExpression=Attr('SK').begins_with("CHUNK#"),
                        ExclusiveStartKey=response['LastEvaluatedKey']
                    )
                    items = response.get('Items', [])
                    chunks.extend([self._parse_chunk(row) for row in items])
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
        if embedding_column not in ("embedding_fast", "embedding_full"):
            raise ValueError(f"Invalid embedding_column: {embedding_column}")
            
        must_filters = []
        if document_ids:
            must_filters.append(qmodels.FieldCondition(
                key="document_id",
                match=qmodels.MatchAny(any=document_ids)
            ))
        if candidate_page_ids:
            must_filters.append(qmodels.FieldCondition(
                key="page_number",
                match=qmodels.MatchAny(any=candidate_page_ids)
            ))
        if candidate_chunk_ids:
            must_filters.append(qmodels.FieldCondition(
                key="original_id",
                match=qmodels.MatchAny(any=candidate_chunk_ids)
            ))
            
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
            import logging
            logging.getLogger(__name__).info(f"Qdrant returned {len(results)} results")
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Qdrant search failed: {e}")
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
            import logging
            logging.getLogger(__name__).warning("DynamoDB batch_get_item failed: %s", e)
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
