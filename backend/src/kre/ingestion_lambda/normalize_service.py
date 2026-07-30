import logging
from kre.providers.embedding_provider import embed_batch

logger = logging.getLogger(__name__)

def cluster_entities(entity_names: list[str], threshold: float = 0.92, provider: str | None = None) -> dict[str, str]:
    """
    Cluster extracted entity names to merge synonyms using embeddings.
    Returns a mapping of {original_name: canonical_name}.
    """
    if not entity_names:
        return {}
        
    unique_names = list(set(entity_names))
    # Generate embeddings
    embeddings = embed_batch(unique_names, provider=provider)
    
    canonical_map = {}
    clusters = [] # list of lists of canonical indices
    
    for i, emb_i in enumerate(embeddings):
        matched_cluster = -1
        # Try to find a matching cluster
        for c_idx, cluster in enumerate(clusters):
            # Compare with the canonical element of the cluster (first item)
            canonical_idx = cluster[0]
            emb_c = embeddings[canonical_idx]
            
            # Cosine similarity
            dot = sum(a * b for a, b in zip(emb_i, emb_c))
            norm_a = sum(a * a for a in emb_i) ** 0.5
            norm_b = sum(b * b for b in emb_c) ** 0.5
            sim = dot / (norm_a * norm_b) if (norm_a * norm_b) > 0 else 0.0
            
            if sim >= threshold:
                matched_cluster = c_idx
                break
                
        if matched_cluster >= 0:
            clusters[matched_cluster].append(i)
        else:
            clusters.append([i])
            
    # Build the map
    for cluster in clusters:
        canonical_name = unique_names[cluster[0]]
        for idx in cluster:
            canonical_map[unique_names[idx]] = canonical_name
            
    return canonical_map
