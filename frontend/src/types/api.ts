// Types matching API Contract rev 6 in API.md

export interface User {
  id: string
  email: string
  name: string
  role: string
  avatar?: string
}

export interface AuthResponse {
  access_token: string
  token_type: string
  expires_in: number
  user: User
}

export interface LoginRequest {
  email: string
  password: string
  remember_me?: boolean
}

export interface Workspace {
  id: string
  name: string
  industry?: string
  description: string
  document_count: number
  last_active: string
  status: "active" | "archived" | "draft"
  icon_type?: "finance" | "legal" | "engineering" | "general"
  created_at?: string
}

export interface CreateWorkspaceRequest {
  name: string
  industry?: string
  description?: string
}

export interface DocumentItem {
  id: string
  filename: string
  format: "pdf" | "docx" | "csv" | "pptx" | string
  upload_date: string
  chunk_count: number
  status: "Ready" | "Processing" | "Failed" | string
  size?: string
}

export interface DocumentLibraryResponse {
  documents: DocumentItem[]
  total_documents: number
  current_page: number
  total_pages: number
}

export interface UploadedDocument {
  id: string
  filename: string
  format: string
  status: "processing" | "uploaded" | "failed" | string
}

export interface DocumentUploadResponse {
  uploaded_documents: UploadedDocument[]
}

export interface BoundingBox {
  l?: number
  t?: number
  r?: number
  b?: number
  x?: number
  y?: number
  width?: number
  height?: number
  page_number?: number
}

export interface Citation {
  id: number
  chunk_id: string
  document_id: string
  document_filename?: string
  source_format?: string
  text?: string
  snippet?: string
  page_number?: number | null
  bounding_box?: BoundingBox | null
  location_reference?: string
}

export interface QueryRequest {
  workspace_id?: string
  query: string
  document_ids?: string[]
  provider?: string
}

export interface LatencyBreakdown {
  route_query_ms?: number
  vector_ms?: number
  bm25_ms?: number
  reranker_ms?: number
  compressor_ms?: number
  fidelity_ms?: number
  llm_ms?: number
  total_ms?: number
  [key: string]: number | undefined
}

export interface QueryResponse {
  answer: string
  citations: Citation[]
  retrieval_path?: string | string[]
  confidence?: number
  confidence_score?: number
  latency_ms?: number
  latency_breakdown?: LatencyBreakdown
  fast_path?: boolean
  cached?: boolean
  faithfulness?: number
  document_ids?: string[]
}

export interface ChatMessage {
  id: string
  sender: "user" | "assistant"
  text: string
  timestamp: string
  citations?: Citation[]
  retrieval_path?: string
  confidence?: number
  latency_ms?: number
  faithfulness?: number
}

export interface ChatSession {
  id: string
  workspaceId: string
  title: string
  category: "Today" | "Previous 7 Days" | "Older"
  updatedAt: string
  messages: ChatMessage[]
}

export interface BenchmarkKPIItem {
  value: number
  unit: string
  target: number
  delta: string
  status: "passing" | "failing"
}

export interface LatencyDataPoint {
  timestamp: string
  latency_ms: number
}

export interface BenchmarkResponse {
  status: "PASSING ALL" | "FAILING TARGETS" | string
  version: string
  kpis: {
    p95_latency: BenchmarkKPIItem
    recall_5: BenchmarkKPIItem
    faithfulness: BenchmarkKPIItem
    llm_activation: BenchmarkKPIItem
  }
  latency_chart: {
    target_line: number
    data_points: LatencyDataPoint[]
  }
}

export interface KnowledgeGraphNode {
  id: string
  label: string
  type: "Person" | "Document" | "Company" | "Technology" | "Concept" | string
  properties?: Record<string, string | number>
}

export interface KnowledgeGraphEdge {
  source: string
  target: string
  label: string
  weight?: number
}

export interface KnowledgeGraphResponse {
  nodes: KnowledgeGraphNode[]
  edges: KnowledgeGraphEdge[]
}
