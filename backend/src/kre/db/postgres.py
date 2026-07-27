import json
import os
from uuid import UUID

import psycopg

from kre.models import Chunk, Document


_IN_MEMORY_DOCS: dict[str, Document] = {}
_IN_MEMORY_CHUNKS: dict[str, Chunk] = {}


class PostgresRepository:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.environ.get("DATABASE_URL", "postgresql://localhost:5432/kre")

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
                    embedding_str = f"[{','.join(str(x) for x in chunk.embedding)}]" if chunk.embedding else None
                    connection.execute(
                        """INSERT INTO chunks (
                            id, document_id, source_format, text, element_type, page_number,
                            section_path, bounding_box, location_reference, metadata, structural_weight, provider, embedding
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET text=EXCLUDED.text""",
                        (
                            chunk.id,
                            chunk.document_id,
                            chunk.source_format,
                            chunk.text,
                            chunk.element_type,
                            chunk.page_number,
                            json.dumps(chunk.section_path),
                            json.dumps(chunk.bounding_box) if chunk.bounding_box else None,
                            chunk.location_reference,
                            json.dumps(chunk.metadata) if chunk.metadata else None,
                            chunk.structural_weight,
                            chunk.provider,
                            embedding_str,
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
                                  section_path, bounding_box, location_reference, metadata, structural_weight, provider, embedding
                           FROM chunks WHERE document_id = ANY(%s) ORDER BY id""",
                        (uuids,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """SELECT id, document_id, source_format, text, element_type, page_number,
                                  section_path, bounding_box, location_reference, metadata, structural_weight, provider, embedding
                           FROM chunks ORDER BY id"""
                    ).fetchall()
            chunks = []
            for row in rows:
                emb = json.loads(row[12]) if isinstance(row[12], str) else row[12]
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
                        emb,
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
        provider: str = "dev",
        document_ids: list[str] | None = None,
        candidate_page_ids: list[int] | None = None,
        candidate_chunk_ids: list[str] | None = None,
        limit: int = 10,
    ) -> list[tuple[Chunk, float]]:
        """Vector search using pgvector cosine distance operator (<=>).

        Rule 30: provider matching enforced.
        Rule 5: PageIndex candidate scoping enforced.
        """
        try:
            embedding_str = f"[{','.join(str(x) for x in query_embedding)}]"
            query_sql = """
                SELECT id, document_id, source_format, text, element_type, page_number,
                       section_path, bounding_box, location_reference, metadata, structural_weight, provider,
                       embedding <=> %s::vector AS distance
                FROM chunks
                WHERE provider = %s
            """
            params = [embedding_str, provider]

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
            filtered = [c for c in chunks if c.provider == provider]

            if candidate_page_ids:
                page_set = set(candidate_page_ids)
                filtered = [c for c in filtered if c.page_number in page_set]

            if candidate_chunk_ids:
                chunk_set = set(candidate_chunk_ids)
                filtered = [c for c in filtered if c.id in chunk_set]

            results = []
            for chunk in filtered:
                if chunk.embedding:
                    vec = chunk.embedding
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

