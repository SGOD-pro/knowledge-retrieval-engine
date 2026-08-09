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
    embedding_fast vector(384),
    embedding_full vector(1024),
    -- S3 keys of images co-extracted with this chunk; never exposed as public URLs.
    -- Presigned URLs are generated at query time by image_url_provider.
    image_s3_keys JSONB NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS chunks_document_id_idx ON chunks(document_id);
CREATE INDEX IF NOT EXISTS chunks_structural_idx ON chunks(structural_weight DESC);
CREATE INDEX IF NOT EXISTS chunks_provider_idx ON chunks(provider);
CREATE INDEX IF NOT EXISTS chunks_embedding_fast_hnsw_idx ON chunks USING hnsw (embedding_fast vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_embedding_full_hnsw_idx ON chunks USING hnsw (embedding_full vector_cosine_ops);

CREATE TABLE IF NOT EXISTS cache_entries (
    redis_key TEXT PRIMARY KEY,
    query_embedding vector(1024),
    doc_scope_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS concepts (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS properties (
    id UUID PRIMARY KEY,
    concept_id UUID NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    property_name TEXT NOT NULL,
    property_value TEXT NOT NULL,
    source_chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0
);
