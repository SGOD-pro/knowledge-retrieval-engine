from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

class ConceptType(str, Enum):
    PRODUCT = "PRODUCT"
    PERSON = "PERSON"
    ORGANIZATION = "ORGANIZATION"
    METRIC = "METRIC"
    POLICY = "POLICY"
    PROCESS = "PROCESS"
    DATE_PERIOD = "DATE_PERIOD"
    LOCATION = "LOCATION"
    ISSUE = "ISSUE"
    REGULATION = "REGULATION"
    TERM = "TERM"

@dataclass
class Concept:
    id: str
    name: str
    type: ConceptType
    document_ids: set[str] = field(default_factory=set)

@dataclass
class Property:
    concept_id: str
    property_name: str
    property_value: str
    value_type: str
    source_chunk_id: str
    confidence: float
    extraction_tier: str

@dataclass
class Relation:
    from_concept_id: str
    to_concept_id: str
    relation_type: str
    relation_weight: float
    source_chunk_id: str
    extraction_tier: str
