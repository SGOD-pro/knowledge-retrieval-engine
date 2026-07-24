CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY, filename TEXT NOT NULL, source_format TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY, document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    source_format TEXT NOT NULL, text TEXT NOT NULL, element_type TEXT NOT NULL,
    page_number INTEGER, section_path JSONB NOT NULL DEFAULT '[]',
    bounding_box JSONB, location_reference TEXT, metadata JSONB,
    structural_weight DOUBLE PRECISION NOT NULL DEFAULT 0,
    embedding vector(2048)
);
CREATE INDEX IF NOT EXISTS chunks_document_id_idx ON chunks(document_id);
CREATE INDEX IF NOT EXISTS chunks_structural_idx ON chunks(structural_weight DESC);
