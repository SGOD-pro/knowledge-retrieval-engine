import os
import sys
from dotenv import load_dotenv; load_dotenv('.env')

# Re-ingest the DB directly by reading chunks and merging them!
from kre.db.postgres import PostgresRepository
from kre.ingestion.adapters.chunk_util import merge_and_split_chunks
from kre.ingestion.embed_service import embed_chunks_dual

def run_fix():
    repo = PostgresRepository()
    all_chunks = repo.get_all_chunks()
    print(f"Original chunks: {len(all_chunks)}")
    
    # Group by document
    docs = {}
    for c in all_chunks:
        docs.setdefault(c.document_id, []).append(c)
        
    fixed_chunks = []
    for doc_id, chunks in docs.items():
        # Sort chunks by element_id/index if possible, but they are generally ordered
        # We'll just trust the original DB order or sort by page/element
        # Here we just apply the merger to the document's chunks
        merged = merge_and_split_chunks(chunks)
        fixed_chunks.extend(merged)
        
    print(f"Merged chunks: {len(fixed_chunks)}")
    
    print("Re-embedding all chunks...")
    import time
    final_chunks = []
    batch_size = 50
    for i in range(0, len(fixed_chunks), batch_size):
        batch = fixed_chunks[i:i+batch_size]
        print(f"Embedding batch {i//batch_size + 1}/{(len(fixed_chunks)+batch_size-1)//batch_size}...")
        final_chunks.extend(embed_chunks_dual(batch, provider="prod"))
        time.sleep(1)
    
    import os
    import psycopg2
    
    print("Truncating old chunks...")
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE chunks")
    conn.commit()
    conn.close()
    
    # We can use save() which takes a Document, but we don't have the Document object easily.
    # We'll manually insert
    print("Inserting new chunks...")
    repo._insert_chunks(final_chunks)
    print("Done!")

if __name__ == "__main__":
    run_fix()
