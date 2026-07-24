import json
import os
from uuid import UUID

import psycopg

from kre.models import Document


class PostgresRepository:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.environ["DATABASE_URL"]

    def initialize(self) -> None:
        schema = open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8").read()
        with psycopg.connect(self.dsn) as connection:
            connection.execute(schema)

    def save(self, document: Document) -> None:
        with psycopg.connect(self.dsn) as connection:
            connection.execute("INSERT INTO documents (id, filename, source_format) VALUES (%s, %s, %s)", (document.id, document.filename, document.source_format))
            for chunk in document.chunks:
                connection.execute("""INSERT INTO chunks (id, document_id, source_format, text, element_type, page_number, section_path, bounding_box, location_reference, metadata, structural_weight) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (chunk.id, chunk.document_id, chunk.source_format, chunk.text, chunk.element_type, chunk.page_number, json.dumps(chunk.section_path), json.dumps(chunk.bounding_box) if chunk.bounding_box else None, chunk.location_reference, json.dumps(chunk.metadata) if chunk.metadata else None, chunk.structural_weight))

    def get(self, document_id: str) -> Document | None:
        with psycopg.connect(self.dsn) as connection:
            doc = connection.execute("SELECT id, filename, source_format FROM documents WHERE id = %s", (UUID(document_id),)).fetchone()
            if not doc:
                return None
            rows = connection.execute("SELECT id, document_id, source_format, text, element_type, page_number, section_path, bounding_box, location_reference, metadata, structural_weight FROM chunks WHERE document_id = %s ORDER BY id", (UUID(document_id),)).fetchall()
        from kre.models import Chunk
        chunks = tuple(Chunk(row[0], str(row[1]), row[2], row[3], row[4], row[5], tuple(row[6] or []), row[7], row[8], row[9], row[10]) for row in rows)
        return Document(str(doc[0]), doc[1], doc[2], chunks)
