from dataclasses import dataclass, field
from typing import Optional

@dataclass
class Concept:
    id: str
    name: str
    type: str  # DocumentEntity, Date, Identifier, etc.
    document_ids: set[str] = field(default_factory=set)

@dataclass
class Property:
    concept_id: str
    property_name: str
    property_value: str
    source_chunk_id: str
    confidence: float

@dataclass
class Relation:
    from_concept_id: str
    to_concept_id: str
    relation_type: str
    relation_weight: float
    source_chunk_id: str
