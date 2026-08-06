import os
import json
import logging
import re
from kre.shared.models import Chunk
from kre.providers.provider_client import get_active_provider, enforce_rate_limit
from kre.shared.bedrock_models import get_concept_model

logger = logging.getLogger(__name__)

def extract_properties_nova_micro(chunks: list[Chunk], provider: str | None = None) -> list[dict]:
    """
    Tier 3 extraction using Bedrock.
    Processes chunks in batch mode (up to 20 at a time) to stay under budget.
    Expected output: [{concept, property_name, property_value, source_chunk_id, confidence}]
    """
    active = provider or get_active_provider()
    model_id = get_concept_model()
    enforce_rate_limit(model_id)
    results = []
    
    # Process in batches of 20
    batch_size = 20
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i+batch_size]
        
        batch_text = "\n---\n".join([f"[{c.chunk_id}]: {c.text}" for c in batch])
        system_prompt = (
            "You are a strict knowledge extraction system. Extract properties from the provided text chunks.\n"
            "Return a JSON array of objects with the exact schema: "
            '{"concept": "...", "property_name": "...", "property_value": "...", "source_chunk_id": "...", "confidence": 0.0-1.0}'
        )
        user_prompt = f"Extract properties from these chunks:\n{batch_text}"
        
        try:
            from kre.shared.aws import get_client
            client = get_client("bedrock-runtime")
            response = client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": user_prompt}]}],
                system=[{"text": system_prompt}],
                inferenceConfig={"temperature": 0.0}
            )
            output_text = response["output"]["message"]["content"][0]["text"]
            
            # Strip markdown
            if output_text.startswith("```json"):
                output_text = output_text[7:]
            elif output_text.startswith("```"):
                output_text = output_text[3:]
            if output_text.endswith("```"):
                output_text = output_text[:-3]
            output_text = output_text.strip()
            
            extracted = json.loads(output_text)
            if isinstance(extracted, list):
                results.extend(extracted)
        except Exception as e:
            logger.error("Extraction failed: %s", str(e))
                
    return results

def extract_tier1_patterns(chunks: list[Chunk]) -> list[dict]:
    """
    Tier 1 extraction using strict Regex for dates, percentages, and identifiers.
    """
    results = []
    # Dates: yyyy-mm-dd or mm/dd/yyyy
    date_pattern = re.compile(r'\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\b')
    # Percentages: numbers followed by %
    pct_pattern = re.compile(r'\b(\d+(?:\.\d+)?)%\b')
    # Identifiers: Alphanumeric ending with uppercase/numbers
    id_pattern = re.compile(r'\b([A-Z0-9]{5,})\b')
    
    for c in chunks:
        for match in date_pattern.finditer(c.text):
            results.append({
                "concept": "DocumentEntity",
                "property_name": "Date",
                "property_value": match.group(1),
                "source_chunk_id": c.id,
                "confidence": 1.0
            })
        for match in pct_pattern.finditer(c.text):
            results.append({
                "concept": "DocumentEntity",
                "property_name": "Percentage",
                "property_value": match.group(1) + "%",
                "source_chunk_id": c.id,
                "confidence": 1.0
            })
        for match in id_pattern.finditer(c.text):
            results.append({
                "concept": "DocumentEntity",
                "property_name": "Identifier",
                "property_value": match.group(1),
                "source_chunk_id": c.id,
                "confidence": 1.0,
                "extraction_tier": "tier1_regex"
            })
    return results

def extract_tier1_5_relations(chunks: list[Chunk], known_concepts: list[str]) -> list[dict]:
    """
    Tier 1.5 SVO Relation Extraction.
    Matches predefined verb families (CAUSES, DEPENDS_ON, AFFECTS) between known concepts.
    Stored with relation_weight=0.6, low_confidence=True, extraction_tier='tier1.5_regex'
    """
    results = []
    if not known_concepts:
        return results

    # Sort known concepts by length descending to match longest phrases first
    sorted_concepts = sorted(known_concepts, key=len, reverse=True)
    # Escape concepts for regex safety
    escaped_concepts = [re.escape(c) for c in sorted_concepts]
    entities_pattern = r'(' + r'|'.join(escaped_concepts) + r')'

    # Define verb families
    families = {
        "CAUSES": r'(?:causes|leads to|results in)',
        "DEPENDS_ON": r'(?:depends on|relies on|requires)',
        "AFFECTS": r'(?:affects|impacts|influences)'
    }

    for rel_type, verb_pattern in families.items():
        # Entity1 + verb + Entity2 (with some optional words in between, up to 5 words)
        # e.g., "ConceptA significantly impacts the new ConceptB"
        pattern = re.compile(
            entities_pattern + r'(?:\s+(?:\w+\s+){0,3})' + verb_pattern + r'(?:\s+(?:\w+\s+){0,3})' + entities_pattern,
            re.IGNORECASE
        )
        for c in chunks:
            for match in pattern.finditer(c.text):
                entity1 = match.group(1)
                entity2 = match.group(2)
                if entity1.lower() != entity2.lower():
                    # We found a relation!
                    results.append({
                        "from_concept": entity1,
                        "to_concept": entity2,
                        "relation_type": rel_type,
                        "relation_weight": 0.6,
                        "source_chunk_id": c.id,
                        "low_confidence": True,
                        "extraction_tier": "tier1.5_regex"
                    })
    return results
