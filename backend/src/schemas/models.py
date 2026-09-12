from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Chunk:
    id: str
    document_id: str
    source_format: str
    text: str
    element_type: str
    page_number: int | None = None
    section_path: tuple[str, ...] = ()
    bounding_box: dict[str, float] | None = None
    location_reference: str | None = None
    metadata: dict[str, Any] | None = None
    structural_weight: float = 0.0
    similarity_score: float | None = None
    reranker_score: float | None = None
    provider: str = "dev"
    embedding_fast: list[float] | None = None
    embedding_full: list[float] | None = None
    # S3 keys for images extracted alongside this chunk (e.g. figures in a PDF).
    # Tuple (not list) to stay consistent with section_path and keep Chunk hashable.
    image_s3_keys: tuple[str, ...] = ()
    workspace_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["section_path"] = list(self.section_path)
        value["image_s3_keys"] = list(self.image_s3_keys)
        return value


@dataclass(frozen=True)
class Document:
    id: str
    filename: str
    source_format: str
    chunks: tuple[Chunk, ...]
    workspace_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "source_format": self.source_format,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "workspace_id": self.workspace_id,
        }


from pydantic import BaseModel, Field


# --- Auth Models ---
class LoginRequest(BaseModel):
    email: str
    password: str
    remember_me: bool | None = False


class User(BaseModel):
    id: str
    email: str
    name: str
    role: str
    avatar: str | None = None


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 3600
    user: User


# --- Workspace Models ---
class CreateWorkspaceRequest(BaseModel):
    name: str
    industry: str | None = "General"
    description: str = ""


class Workspace(BaseModel):
    id: str
    name: str
    industry: str | None = "General"
    description: str = ""
    document_count: int = 0
    last_active: str = "Active just now"
    status: str = "active"
    icon_type: str | None = "general"
    created_at: str | None = None

    def __getitem__(self, item: str):
        return getattr(self, item)



# --- Document Models ---
class DocumentItem(BaseModel):
    id: str
    filename: str
    format: str
    upload_date: str
    chunk_count: int
    status: str = "Ready"
    size: str | None = None


class DocumentLibraryResponse(BaseModel):
    documents: list[DocumentItem]
    total_documents: int
    current_page: int = 1
    total_pages: int = 1


class UploadedDocument(BaseModel):
    id: str
    filename: str
    format: str
    status: str = "processing"


class DocumentUploadResponse(BaseModel):
    uploaded_documents: list[UploadedDocument]


# --- Query Models ---
class BoundingBox(BaseModel):
    l: float
    t: float
    r: float
    b: float


class Citation(BaseModel):
    id: int
    chunk_id: str
    document_id: str
    document_filename: str
    source_format: str
    text: str
    page_number: int | None = None
    bounding_box: dict[str, float] | None = None
    location_reference: str = ""


class QueryRequest(BaseModel):
    query: str
    workspace_id: str
    document_ids: list[str] | None = None
    provider: str | None = None


class QueryResponse(BaseModel):
    answer: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_path: str = "full"
    confidence: float = 0.0
    latency_ms: float = 0.0
    faithfulness: float | None = None
    citation_utilization_rate: float | None = None
    token_usage: dict[str, Any] | None = None
    cached: bool = False
    document_ids: list[str] = Field(default_factory=list)
    latency_breakdown: dict[str, Any] | None = None


# --- System Benchmark Models ---
class BenchmarkKPIItem(BaseModel):
    value: float
    unit: str
    target: float
    delta: str
    status: str


class LatencyDataPoint(BaseModel):
    timestamp: str
    latency_ms: float


class BenchmarkResponse(BaseModel):
    status: str = "PASSING ALL"
    version: str = "v2.4.1"
    kpis: dict[str, BenchmarkKPIItem]
    latency_chart: dict[str, Any]


# --- Knowledge Graph Models ---
class KnowledgeGraphNode(BaseModel):
    id: str
    label: str
    type: str = "Concept"
    properties: dict[str, Any] | None = None


class KnowledgeGraphEdge(BaseModel):
    source: str
    target: str
    label: str
    weight: float | None = 1.0


class KnowledgeGraphResponse(BaseModel):
    nodes: list[KnowledgeGraphNode]
    edges: list[KnowledgeGraphEdge]


# --- Chat Persistence Models ---
class ChatMessageSchema(BaseModel):
    id: str
    sender: str  # "user" | "assistant"
    text: str
    timestamp: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_path: str | None = None
    confidence: float | None = None
    latency_ms: float | None = None
    faithfulness: float | None = None


class ChatSessionSchema(BaseModel):
    id: str
    workspace_id: str
    title: str
    category: str = "Today"
    updated_at: str
    messages: list[ChatMessageSchema] = Field(default_factory=list)


class CreateChatSessionRequest(BaseModel):
    title: str | None = "New Query Session"


class SaveChatMessageRequest(BaseModel):
    id: str | None = None
    sender: str  # "user" | "assistant"
    text: str
    timestamp: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_path: str | None = None
    confidence: float | None = None
    latency_ms: float | None = None
    faithfulness: float | None = None


