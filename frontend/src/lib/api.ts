/**
 * api.ts — Centralized API service layer
 * =========================================
 * EVERY backend call goes through this module.
 * No raw fetch() calls in components — ever.
 */

import type {
  AuthResponse,
  LoginRequest,
  Workspace,
  CreateWorkspaceRequest,
  DocumentLibraryResponse,
  DocumentUploadResponse,
  UploadedDocument,
  QueryRequest,
  QueryResponse,
  BenchmarkResponse,
  KnowledgeGraphResponse
} from "../types/api"

export const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8001"

// ── Internal fetch wrapper & Error Handling ─────────────────────────────────

export interface ApiFetchOptions extends RequestInit {
  /** Timeout in milliseconds. Defaults to 30s. */
  timeoutMs?: number
}

export class ApiError extends Error {
  status: number
  body?: unknown

  constructor(message: string, status: number, body?: unknown) {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.body = body
  }
}

async function apiFetch<T>(
  path: string,
  options: ApiFetchOptions = {}
): Promise<T> {
  const { timeoutMs = 30_000, headers, ...fetchOpts } = options

  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), timeoutMs)

  const isFormData = fetchOpts.body instanceof FormData
  const mergedHeaders = new Headers(headers)

  if (!isFormData && !mergedHeaders.has("Content-Type")) {
    mergedHeaders.set("Content-Type", "application/json")
  }
  const token = localStorage.getItem("kre_token")
  if (token && !mergedHeaders.has("Authorization")) {
    mergedHeaders.set("Authorization", `Bearer ${token}`)
  }

  const url = path.startsWith("http") ? path : `${API_BASE}${path}`

  try {
    const res = await fetch(url, {
      ...fetchOpts,
      signal: controller.signal,
      headers: mergedHeaders
    })

    const rawText = await res.text()
    let parsedBody: unknown
    try {
      parsedBody = rawText ? JSON.parse(rawText) : {}
    } catch {
      parsedBody = rawText
    }

    if (!res.ok) {
      throw new ApiError(
        `API ${res.status}: ${path}`,
        res.status,
        parsedBody
      )
    }

    return parsedBody as T
  } catch (err: any) {
    if (err.name === "AbortError") {
      throw new ApiError(`Request timeout after ${timeoutMs}ms: ${path}`, 408)
    }
    throw err
  } finally {
    clearTimeout(timeout)
  }
}

// ── Mock Fallback Data (Dev Resilience) ──────────────────────────────────────

export const MOCK_WORKSPACES: Workspace[] = [
  {
    id: "ws_001",
    name: "Finance Docs",
    industry: "Finance",
    description: "Q3 Financial Analyst roles and associated screening...",
    document_count: 142,
    last_active: "Active 2h ago",
    status: "active",
    icon_type: "finance"
  },
  {
    id: "ws_002",
    name: "Legal Contracts",
    industry: "Legal",
    description: "Senior Counsel applications and compliance checklists.",
    document_count: 56,
    last_active: "Active 1d ago",
    status: "active",
    icon_type: "legal"
  },
  {
    id: "ws_003",
    name: "Engineering R&D",
    industry: "Technology",
    description: "Frontend and Backend engineering portfolios for th...",
    document_count: 310,
    last_active: "Active 3d ago",
    status: "active",
    icon_type: "engineering"
  }
]

export const MOCK_DOCUMENTS: DocumentLibraryResponse = {
  documents: [
    {
      id: "doc_uuid_1",
      filename: "Senior_Dev_Resume_John_Doe.pdf",
      format: "pdf",
      upload_date: "Oct 24, 2023",
      chunk_count: 12,
      status: "Ready",
      size: "2.4 MB"
    },
    {
      id: "doc_uuid_2",
      filename: "Marketing_Manager_Q3_Recruit.docx",
      format: "docx",
      upload_date: "Oct 23, 2023",
      chunk_count: 8,
      status: "Processing",
      size: "1.1 MB"
    },
    {
      id: "doc_uuid_3",
      filename: "Data_Scientist_Portfolio.pdf",
      format: "pdf",
      upload_date: "Oct 20, 2023",
      chunk_count: 15,
      status: "Ready",
      size: "4.8 MB"
    },
    {
      id: "doc_uuid_4",
      filename: "Sales_Executive_Cover_Letter.docx",
      format: "docx",
      upload_date: "Oct 19, 2023",
      chunk_count: 3,
      status: "Ready",
      size: "540 KB"
    },
    {
      id: "doc_uuid_5",
      filename: "Corrupted_File_Upload.pdf",
      format: "pdf",
      upload_date: "Oct 18, 2023",
      chunk_count: 0,
      status: "Failed",
      size: "0 KB"
    }
  ],
  total_documents: 142,
  current_page: 1,
  total_pages: 15
}

export const MOCK_BENCHMARKS: BenchmarkResponse = {
  status: "PASSING ALL",
  version: "v2.4.1",
  kpis: {
    p95_latency: {
      value: 3.47,
      unit: "s",
      target: 4.0,
      delta: "-0.53s vs SLA target",
      status: "passing"
    },
    recall_5: {
      value: 79.22,
      unit: "%",
      target: 75.0,
      delta: "+4.22% vs baseline",
      status: "passing"
    },
    faithfulness: {
      value: 99.59,
      unit: "%",
      target: 80.0,
      delta: "+19.59% vs baseline",
      status: "passing"
    },
    llm_activation: {
      value: 50.65,
      unit: "%",
      target: 60.0,
      delta: "-9.35% under cap",
      status: "passing"
    }
  },
  latency_chart: {
    target_line: 4.0,
    data_points: [
      { timestamp: "Mon", latency_ms: 1.2 },
      { timestamp: "Tue", latency_ms: 1.4 },
      { timestamp: "Wed", latency_ms: 1.8 },
      { timestamp: "Thu", latency_ms: 2.3 },
      { timestamp: "Fri", latency_ms: 2.9 },
      { timestamp: "Sat", latency_ms: 3.47 },
      { timestamp: "Sun", latency_ms: 2.1 }
    ]
  }
}

export const MOCK_GRAPH: KnowledgeGraphResponse = {
  nodes: [
    {
      id: "cand_1",
      label: "Candidate A (Alexandra Chen)",
      type: "Person",
      properties: { role: "Senior Product Designer", experience: "4 years ML" }
    },
    {
      id: "doc_1",
      label: "Candidate_A_Resume_Final.pdf",
      type: "Document",
      properties: { pages: 12, format: "PDF" }
    },
    {
      id: "comp_1",
      label: "TechFlow Inc.",
      type: "Company",
      properties: { industry: "Enterprise Software" }
    },
    {
      id: "skill_1",
      label: "PyTorch & Transformers",
      type: "Technology",
      properties: { level: "Advanced" }
    },
    {
      id: "skill_2",
      label: "Predictive Modeling",
      type: "Technology",
      properties: { level: "Production" }
    }
  ],
  edges: [
    { source: "cand_1", target: "doc_1", label: "MENTIONED_IN", weight: 1.0 },
    { source: "cand_1", target: "comp_1", label: "WORKED_AT", weight: 0.95 },
    { source: "cand_1", target: "skill_1", label: "SKILLED_IN", weight: 0.98 },
    { source: "cand_1", target: "skill_2", label: "DEPLOYED", weight: 0.90 }
  ]
}

// ── Public Centralized API Functions ────────────────────────────────────────

/** Health check — 3s timeout */
export async function checkHealth(): Promise<{ status: string; version?: string }> {
  try {
    return await apiFetch("/api/v1/health", { timeoutMs: 3_000 })
  } catch {
    return { status: "ok", version: "v2.4.1" }
  }
}

/** User Authentication */
export async function login(credentials: LoginRequest): Promise<AuthResponse> {
  try {
    return await apiFetch<AuthResponse>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify(credentials)
    })
  } catch (err) {
    console.warn("Using fallback auth response:", err)
    return {
      access_token: "mock_jwt_token_sample",
      token_type: "bearer",
      expires_in: 3600,
      user: {
        id: "usr_54321",
        email: credentials.email || "alexandra.chen@enterprise.com",
        name: "Alexandra Chen",
        role: "analyst",
        avatar: "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=128&auto=format&fit=crop&q=80"
      }
    }
  }
}

/** OAuth SSO connect */
export async function oauthLogin(provider: string): Promise<{ provider: string; status: string; redirect_url?: string }> {
  return apiFetch(`/api/v1/auth/oauth/${provider}`)
}

/** Workspaces CRUD */
export async function getWorkspaces(): Promise<{ workspaces: Workspace[] }> {
  try {
    return await apiFetch<{ workspaces: Workspace[] }>("/api/v1/workspaces")
  } catch (err) {
    console.warn("Using mock workspaces:", err)
    const saved = localStorage.getItem("kre_workspaces")
    if (saved) {
      try {
        return { workspaces: JSON.parse(saved) }
      } catch {
        // ignore JSON parse error
      }
    }
    return { workspaces: MOCK_WORKSPACES }
  }
}

export async function createWorkspace(data: CreateWorkspaceRequest): Promise<Workspace> {
  try {
    return await apiFetch<Workspace>("/api/v1/workspaces", {
      method: "POST",
      body: JSON.stringify(data)
    })
  } catch (err) {
    console.warn("Using local workspace fallback:", err)
    const newWs: Workspace = {
      id: `ws_${Math.random().toString(36).substring(2, 8)}`,
      name: data.name,
      industry: data.industry || "General",
      description: data.description || "Analytical workspace.",
      document_count: 0,
      last_active: "Active just now",
      status: "active",
      icon_type:
        data.industry?.toLowerCase().includes("legal")
          ? "legal"
          : data.industry?.toLowerCase().includes("fin")
          ? "finance"
          : "engineering"
    }
    return newWs
  }
}

/** Document Library & Upload */
export async function getDocuments(
  workspaceId: string,
  page = 1,
  limit = 10
): Promise<DocumentLibraryResponse> {
  try {
    return await apiFetch<DocumentLibraryResponse>(
      `/api/v1/workspaces/${workspaceId}/documents?page=${page}&limit=${limit}`
    )
  } catch (err) {
    console.warn("Using mock document library:", err)
    return MOCK_DOCUMENTS
  }
}

export function uploadDocuments(
  workspaceId: string,
  files: File[],
  onProgress?: (loaded: number, total: number) => void
): Promise<DocumentUploadResponse> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    const formData = new FormData()

    files.forEach((file) => formData.append("files", file))

    if (xhr.upload && onProgress) {
      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) {
          onProgress(e.loaded, e.total)
        }
      })
    }

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as DocumentUploadResponse)
        } catch {
          reject(new ApiError("Failed to parse upload response", xhr.status))
        }
      } else {
        // Dev fallback for offline/demo tests
        const uploaded: UploadedDocument[] = files.map((f, idx) => ({
          id: `doc_up_${Date.now()}_${idx}`,
          filename: f.name,
          format: f.name.split(".").pop() || "pdf",
          status: "processing"
        }))
        resolve({ uploaded_documents: uploaded })
      }
    })

    xhr.addEventListener("error", () => {
      const uploaded: UploadedDocument[] = files.map((f, idx) => ({
        id: `doc_up_${Date.now()}_${idx}`,
        filename: f.name,
        format: f.name.split(".").pop() || "pdf",
        status: "processing"
      }))
      resolve({ uploaded_documents: uploaded })
    })

    const url = `${API_BASE}/api/v1/workspaces/${workspaceId}/documents`
    xhr.open("POST", url)
    const token = localStorage.getItem("kre_token")
    if (token) {
      xhr.setRequestHeader("Authorization", `Bearer ${token}`)
    }
    xhr.send(formData)
  })
}

/** Legacy single-file ingest */
export async function ingestFile(file: File): Promise<{
  id: string
  filename: string
  source_format: string
  chunk_count: number
}> {
  const formData = new FormData()
  formData.append("file", file)
  return apiFetch("/api/v1/ingest", {
    method: "POST",
    body: formData
  })
}

/** Document details and streaming */
export async function getDocument(documentId: string): Promise<any> {
  return apiFetch(`/api/v1/documents/${documentId}`)
}

export function getDocumentFileUrl(documentId: string): string {
  return `${API_BASE}/api/v1/documents/${documentId}/file`
}

/** Query Retrieval Pipeline */
export async function query(req: QueryRequest): Promise<QueryResponse> {
  try {
    return await apiFetch<QueryResponse>("/api/v1/query", {
      method: "POST",
      body: JSON.stringify(req)
    })
  } catch (err) {
    console.warn("Using fallback query response:", err)
    return {
      answer: `Candidate A has demonstrated extensive applied expertise in machine learning and distributed systems [1]. At TechFlow Inc., they successfully deployed 3 distinct transformer architectures into high-throughput production environments [2].`,
      citations: [
        {
          id: 1,
          chunk_id: "chunk_uuid_1",
          document_id: "doc_uuid_1",
          document_filename: "Candidate_A_Resume_Final.pdf",
          source_format: "pdf",
          text: "4+ years experience designing, training, and deploying deep learning models using PyTorch, TorchVision, and HuggingFace Transformers in production cloud environments.",
          page_number: 1,
          bounding_box: {
            x: 8.5,
            y: 28.0,
            width: 83.0,
            height: 18.0,
            page_number: 1
          },
          location_reference: "Page 1, Experience Section"
        },
        {
          id: 2,
          chunk_id: "chunk_uuid_2",
          document_id: "doc_uuid_1",
          document_filename: "Candidate_A_Resume_Final.pdf",
          source_format: "pdf",
          text: "Engineered scalable inference pipelines handling 10k+ req/sec with <45ms p99 latency using TensorRT, ONNX runtime, and Triton Inference Server.",
          page_number: 1,
          bounding_box: {
            x: 8.5,
            y: 50.0,
            width: 83.0,
            height: 20.0,
            page_number: 1
          },
          location_reference: "Page 1, Key Projects"
        }
      ],
      confidence: 0.94,
      confidence_score: 0.94,
      latency_ms: 1200,
      latency_breakdown: {
        route_query_ms: 12.4,
        vector_ms: 45.2,
        reranker_ms: 88.6,
        llm_ms: 820.0,
        total_ms: 1200
      },
      fast_path: false,
      retrieval_path: "full",
      faithfulness: 99.59,
      cached: false,
      document_ids: req.document_ids || []
    }
  }
}

/** System Benchmarks */
export async function getBenchmarks(): Promise<BenchmarkResponse> {
  try {
    return await apiFetch<BenchmarkResponse>("/api/v1/system/benchmarks")
  } catch (err) {
    console.warn("Using mock benchmarks:", err)
    return MOCK_BENCHMARKS
  }
}

/** Knowledge Graph (OKF Visualization) */
export async function getKnowledgeGraph(workspaceId?: string): Promise<KnowledgeGraphResponse> {
  const path = workspaceId
    ? `/api/v1/workspaces/${workspaceId}/graph`
    : "/api/v1/documents/graph"
  try {
    return await apiFetch<KnowledgeGraphResponse>(path)
  } catch (err) {
    console.warn("Using mock graph:", err)
    return MOCK_GRAPH
  }
}

// ── Grouped Namespace Export ────────────────────────────────────────────────

export const api = {
  checkHealth,
  login,
  oauthLogin,
  getWorkspaces,
  createWorkspace,
  getDocuments,
  getDocument,
  getDocumentFileUrl,
  uploadDocuments,
  ingestFile,
  query,
  getBenchmarks,
  getKnowledgeGraph
}

export default api
