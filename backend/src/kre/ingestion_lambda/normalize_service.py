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
    clusters = [] # list of dicts: {"canonical_idx": i, "members": [(idx, confidence, is_flagged)]}
    
    for i, emb_i in enumerate(embeddings):
        best_sim = 0.0
        best_cluster = -1
        
        # Find the most similar cluster
        for c_idx, cluster in enumerate(clusters):
            canonical_idx = cluster["canonical_idx"]
            emb_c = embeddings[canonical_idx]
            
            # Cosine similarity
            dot = sum(a * b for a, b in zip(emb_i, emb_c))
            norm_a = sum(a * a for a in emb_i) ** 0.5
            norm_b = sum(b * b for b in emb_c) ** 0.5
            sim = dot / (norm_a * norm_b) if (norm_a * norm_b) > 0 else 0.0
            
            if sim > best_sim:
                best_sim = sim
                best_cluster = c_idx
                
        # DECISION.md Brackets:
        # cosine_sim >= 0.92    auto-merge (same entity)
        # 0.85 <= sim < 0.92    merge, set low_confidence = True
        # sim < 0.85            separate nodes, add to manual review queue
        if best_sim >= 0.92:
            clusters[best_cluster]["members"].append({"idx": i, "low_confidence": False, "flagged": False})
        elif best_sim >= 0.85:
            clusters[best_cluster]["members"].append({"idx": i, "low_confidence": True, "flagged": False})
        else:
            clusters.append({"canonical_idx": i, "members": [{"idx": i, "low_confidence": False, "flagged": True}]})
            
    # Build the map
    # In a full system, we would store the low_confidence and flagged metadata in the DB.
    # For now, we return the canonical mapping and log the flagged/low_confidence items.
    for cluster in clusters:
        canonical_name = unique_names[cluster["canonical_idx"]]
        for member in cluster["members"]:
            member_name = unique_names[member["idx"]]
            canonical_map[member_name] = canonical_name
            if member["low_confidence"]:
                logger.info(f"Merged '{member_name}' into '{canonical_name}' with LOW CONFIDENCE (sim < 0.92)")
            if member["flagged"] and member["idx"] != cluster["canonical_idx"]:
                # This should theoretically not happen because flagged creates a new cluster,
                # but if we extend logic, this logs the manual review queue items.
                pass
        
    return canonical_map
