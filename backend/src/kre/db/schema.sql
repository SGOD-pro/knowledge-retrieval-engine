CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY,
    filename TEXT NOT NULL,
    source_format TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    source_format TEXT NOT NULL,
    text TEXT NOT NULL,
    element_type TEXT NOT NULL,
    page_number INTEGER,
    section_path JSONB NOT NULL DEFAULT '[]',
    bounding_box JSONB,
    location_reference TEXT,
    metadata JSONB,
    structural_weight DOUBLE PRECISION NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT 'dev',
    embedding vector(1024)
);

CREATE INDEX IF NOT EXISTS chunks_document_id_idx ON chunks(document_id);
CREATE INDEX IF NOT EXISTS chunks_structural_idx ON chunks(structural_weight DESC);
CREATE INDEX IF NOT EXISTS chunks_provider_idx ON chunks(provider);
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS cache_entries (
    redis_key TEXT PRIMARY KEY,
    query_embedding vector(1024),
    doc_scope_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

