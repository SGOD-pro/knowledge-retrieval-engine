import numpy as np
from kre.ingestion.embed_service import embed_fast_local

def cosine_similarity(v1, v2):
    dot = np.dot(v1, v2)
    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)
    return dot / (norm1 * norm2)

if __name__ == "__main__":
    t1 = "refund policy"
    t2 = "return policy"
    t3 = "battery specifications"

    v1 = embed_fast_local(t1)
    v2 = embed_fast_local(t2)
    v3 = embed_fast_local(t3)

    sim_high = cosine_similarity(v1, v2)
    sim_low = cosine_similarity(v1, v3)

    print(f"Cosine similarity ('{t1}' vs '{t2}'): {sim_high:.4f} (Expected High)")
    print(f"Cosine similarity ('{t1}' vs '{t3}'): {sim_low:.4f} (Expected Low)")
    
    if sim_high > sim_low + 0.1:
        print("Sanity check PASSED: Model is producing meaningful embeddings.")
    else:
        print("Sanity check FAILED: Embeddings do not distinguish semantics properly.")
