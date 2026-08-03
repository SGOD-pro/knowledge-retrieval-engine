import json
import os
from uuid import UUID

import psycopg

from kre.shared.models import Chunk, Document


_IN_MEMORY_DOCS: dict[str, Document] = {}
_IN_MEMORY_CHUNKS: dict[str, Chunk] = {}


class PostgresRepository:
    def __init__(self, dsn: str | None = None):
        env = os.environ.get("ENVIRONMENT", "dev")
        if dsn:
            self.dsn = dsn
        elif env == "prod":
            self.dsn = os.environ["DATABASE_URL"]  # Must be set in prod
        else:
            self.dsn = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kre")

    def _connect(self):
        return psycopg.connect(self.dsn)

    def initialize(self) -> None:
        schema = open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8").read()
        try:
            with self._connect() as connection:
                connection.execute(schema)
        except Exception:
            # Postgres not available; operate in in-memory mode
            pass

    def save(self, document: Document) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO documents (id, filename, source_format) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
                    (document.id, document.filename, document.source_format),
                )
                for chunk in document.chunks:
                    emb_fast_str = f"[{','.join(str(x) for x in chunk.embedding_fast)}]" if chunk.embedding_fast else None
                    emb_full_str = f"[{','.join(str(x) for x in chunk.embedding_full)}]" if chunk.embedding_full else None
                    connection.execute(
                        """INSERT INTO chunks (
                            id, document_id, source_format, text, element_type, page_number,
                            section_path, bounding_box, location_reference, metadata,
                            structural_weight, provider, embedding_fast, embedding_full,
                            image_s3_keys
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (id) DO UPDATE SET
                            text=EXCLUDED.text,
                            embedding_fast=EXCLUDED.embedding_fast,
                            embedding_full=EXCLUDED.embedding_full,
                            image_s3_keys=EXCLUDED.image_s3_keys""",
                        (
                            chunk.id,
                            chunk.document_id,
                            chunk.source_format,
                            chunk.text,
                            chunk.element_type,
                            chunk.page_number,
                            json.dumps(list(chunk.section_path)),
                            json.dumps(chunk.bounding_box) if chunk.bounding_box else None,
                            chunk.location_reference,
                            json.dumps(chunk.metadata) if chunk.metadata else None,
                            chunk.structural_weight,
                            chunk.provider,
                            emb_fast_str,
                            emb_full_str,
                            json.dumps(list(chunk.image_s3_keys)),
                        ),
                    )
                return
        except Exception:
            pass

        # In-memory fallback
        _IN_MEMORY_DOCS[str(document.id)] = document
        for c in document.chunks:
            _IN_MEMORY_CHUNKS[c.id] = c

    def get(self, document_id: str) -> Document | None:
        try:
            with self._connect() as connection:
                doc = connection.execute(
                    "SELECT id, filename, source_format FROM documents WHERE id = %s",
                    (UUID(document_id),),
                ).fetchone()
                if not doc:
                    return None
                rows = connection.execute(
                    """SELECT id, document_id, source_format, text, element_type, page_number,
                              section_path, bounding_box, location_reference, metadata, structural_weight, provider
                       FROM chunks WHERE document_id = %s ORDER BY id""",
                    (UUID(document_id),),
                ).fetchall()
            chunks = tuple(
                Chunk(
                    row[0],
                    str(row[1]),
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    tuple(row[6] or []),
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    row[11],
                )
                for row in rows
            )
            return Document(str(doc[0]), doc[1], doc[2], chunks)
        except Exception:
            return _IN_MEMORY_DOCS.get(str(document_id))

    def get_all_chunks(self, document_ids: list[str] | None = None) -> list[Chunk]:
        try:
            with self._connect() as connection:
                if document_ids:
                    uuids = [UUID(d) for d in document_ids]
                    rows = connection.execute(
                        """SELECT id, document_id, source_format, text, element_type, page_number,
                                  section_path, bounding_box, location_reference, metadata,
                                  structural_weight, provider, embedding_fast, embedding_full,
                                  image_s3_keys
                           FROM chunks WHERE document_id = ANY(%s) ORDER BY id""",
                        (uuids,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """SELECT id, document_id, source_format, text, element_type, page_number,
                                  section_path, bounding_box, location_reference, metadata,
                                  structural_weight, provider, embedding_fast, embedding_full,
                                  image_s3_keys
                           FROM chunks ORDER BY id"""
                    ).fetchall()
            chunks = []
            for row in rows:
                emb_fast = json.loads(row[12]) if isinstance(row[12], str) else row[12]
                emb_full = json.loads(row[13]) if isinstance(row[13], str) else row[13]
                raw_img_keys = row[14]
                if isinstance(raw_img_keys, str):
                    img_keys: tuple[str, ...] = tuple(json.loads(raw_img_keys))
                elif raw_img_keys:
                    img_keys = tuple(raw_img_keys)
                else:
                    img_keys = ()
                chunks.append(
                    Chunk(
                        row[0],
                        str(row[1]),
                        row[2],
                        row[3],
                        row[4],
                        row[5],
                        tuple(row[6] or []),
                        row[7],
                        row[8],
                        row[9],
                        row[10],
                        row[11],
                        emb_fast,
                        emb_full,
                        img_keys,
                    )
                )
            return chunks
        except Exception:
            chunks = list(_IN_MEMORY_CHUNKS.values())
            if document_ids:
                doc_set = set(str(d) for d in document_ids)
                chunks = [c for c in chunks if str(c.document_id) in doc_set]
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
        """Vector search using pgvector cosine distance operator (<=>).

        Rule 19: fast-path queries target embedding_fast, full-path queries target embedding_full.
        Rule 30: No query ever compares across both columns.
        Rule 5: PageIndex candidate scoping enforced.
        """
        if embedding_column not in ("embedding_fast", "embedding_full"):
            raise ValueError(f"Invalid embedding_column: {embedding_column}. Must be 'embedding_fast' or 'embedding_full'.")

        try:
            embedding_str = f"[{','.join(str(x) for x in query_embedding)}]"
            query_sql = f"""
                SELECT id, document_id, source_format, text, element_type, page_number,
                       section_path, bounding_box, location_reference, metadata, structural_weight, provider,
                       {embedding_column} <=> %s::vector AS distance
                FROM chunks
                WHERE {embedding_column} IS NOT NULL
            """
            params: list = [embedding_str]

            if document_ids:
                query_sql += " AND document_id = ANY(%s)"
                params.append([UUID(d) for d in document_ids])

            if candidate_page_ids:
                query_sql += " AND page_number = ANY(%s)"
                params.append(candidate_page_ids)

            if candidate_chunk_ids:
                query_sql += " AND id = ANY(%s)"
                params.append(candidate_chunk_ids)

            query_sql += " ORDER BY distance ASC LIMIT %s"
            params.append(limit)

            with self._connect() as connection:
                rows = connection.execute(query_sql, params).fetchall()

            results = []
            for row in rows:
                chunk = Chunk(
                    row[0],
                    str(row[1]),
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    tuple(row[6] or []),
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    row[11],
                )
                distance = float(row[12])
                similarity = max(0.0, 1.0 - distance)
                results.append((chunk, similarity))
            return results
        except Exception:
            # Fallback cosine distance calculation over in-memory chunks
            chunks = self.get_all_chunks(document_ids)

            if candidate_page_ids:
                page_set = set(candidate_page_ids)
                chunks = [c for c in chunks if c.page_number in page_set]

            if candidate_chunk_ids:
                chunk_set = set(candidate_chunk_ids)
                chunks = [c for c in chunks if c.id in chunk_set]

            results = []
            for chunk in chunks:
                vec = chunk.embedding_fast if embedding_column == "embedding_fast" else chunk.embedding_full
                if vec:
                    # Compute cosine similarity
                    dot = sum(a * b for a, b in zip(query_embedding, vec))
                    norm_a = sum(a * a for a in query_embedding) ** 0.5
                    norm_b = sum(b * b for b in vec) ** 0.5
                    sim = dot / (norm_a * norm_b) if (norm_a * norm_b) > 0 else 0.0
                else:
                    sim = 0.0
                results.append((chunk, float(sim)))

            results.sort(key=lambda x: x[1], reverse=True)
            return results[:limit]

    def check_semantic_cache(
        self,
        query_embedding: list[float],
        doc_scope_hash: str,
        provider: str
    ) -> str | None:
        """Layer 1 Cache: pgvector semantic similarity check.
        Uses <=> (cosine distance) operator. A distance <= 0.05 is equivalent to similarity >= 0.95.
        Returns the redis_key if a match is found, otherwise None.
        """
        try:
            embedding_str = f"[{','.join(str(x) for x in query_embedding)}]"
            query_sql = """
                SELECT redis_key
                FROM cache_entries
                WHERE doc_scope_hash = %s AND provider = %s
                  AND query_embedding <=> %s::vector <= 0.05
                ORDER BY query_embedding <=> %s::vector ASC
                LIMIT 1
            """
            with self._connect() as connection:
                row = connection.execute(query_sql, (doc_scope_hash, provider, embedding_str, embedding_str)).fetchone()
                if row:
                    return row[0]
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
        """Save a new entry to the semantic cache index."""
        try:
            embedding_str = f"[{','.join(str(x) for x in query_embedding)}]"
            query_sql = """
                INSERT INTO cache_entries (redis_key, query_embedding, doc_scope_hash, provider)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (redis_key) DO NOTHING
            """
            with self._connect() as connection:
                connection.execute(query_sql, (redis_key, embedding_str, doc_scope_hash, provider))
        except Exception:
            pass
