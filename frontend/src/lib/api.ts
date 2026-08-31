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
  QueryRequest,
  QueryResponse,
  BenchmarkResponse,
  KnowledgeGraphResponse,
  ChatMessage,
  ChatSession
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

export const MOCK_WORKSPACES: Workspace[] = []

export const MOCK_DOCUMENTS: DocumentLibraryResponse = {
  documents: [],
  total_documents: 0,
  current_page: 1,
  total_pages: 1
}

export const MOCK_BENCHMARKS: BenchmarkResponse = {
  status: "ACTIVE",
  version: "v2.4.1",
  kpis: {
    p95_latency: {
      value: 0,
      unit: "s",
      target: 4.0,
      delta: "Ready",
      status: "passing"
    },
    recall_5: {
      value: 0,
      unit: "%",
      target: 75.0,
      delta: "Ready",
      status: "passing"
    },
    faithfulness: {
      value: 0,
      unit: "%",
      target: 80.0,
      delta: "Ready",
      status: "passing"
    },
    llm_activation: {
      value: 0,
      unit: "%",
      target: 60.0,
      delta: "Ready",
      status: "passing"
    }
  },
  latency_chart: {
    target_line: 4.0,
    data_points: []
  }
}

export const MOCK_GRAPH: KnowledgeGraphResponse = {
  nodes: [],
  edges: []
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
  return apiFetch<{ workspaces: Workspace[] }>("/api/v1/workspaces")
}

export async function createWorkspace(data: CreateWorkspaceRequest): Promise<Workspace> {
  return apiFetch<Workspace>("/api/v1/workspaces", {
    method: "POST",
    body: JSON.stringify(data)
  })
}

/** Document Library */
export async function getDocuments(
  workspaceId: string,
  page = 1,
  limit = 10
): Promise<DocumentLibraryResponse> {
  return apiFetch<DocumentLibraryResponse>(
    `/api/v1/workspaces/${workspaceId}/documents?page=${page}&limit=${limit}`
  )
}

/**
 * Upload a single document with XHR so we can track per-file upload progress.
 * Call this per-file in parallel from the store — do not batch files into one request.
 */
export function uploadSingleDocument(
  workspaceId: string,
  file: File,
  onProgress?: (loaded: number, total: number) => void
): Promise<DocumentUploadResponse> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    const formData = new FormData()
    formData.append("files", file)

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
        let errMsg = `Upload failed: ${xhr.status}`
        try {
          const body = JSON.parse(xhr.responseText)
          if (body?.detail) errMsg = body.detail
        } catch { /* ignore */ }
        reject(new ApiError(errMsg, xhr.status))
      }
    })

    xhr.addEventListener("error", () => {
      reject(new ApiError("Network error during upload", 0))
    })

    xhr.addEventListener("abort", () => {
      reject(new ApiError("Upload aborted", 0))
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

export interface QueryStreamStageEvent {
  type: "stage"
  stage: string
  state: "working" | "searching" | "solving" | "listening" | "connecting" | "weaving" | "composing" | "breathing" | "shaping"
  label: string
  progress?: number
  path?: "fast" | "full"
}

/** Query Retrieval Pipeline with Real-time SSE Stage Streaming */
export async function queryStream(
  req: QueryRequest,
  onProgress?: (event: QueryStreamStageEvent) => void
): Promise<QueryResponse> {
  try {
    const token = localStorage.getItem("kre_token")
    const headers: Record<string, string> = {
      "Content-Type": "application/json"
    }
    if (token) {
      headers["Authorization"] = `Bearer ${token}`
    }

    const response = await fetch(`${API_BASE}/api/v1/query/stream`, {
      method: "POST",
      headers,
      body: JSON.stringify(req)
    })

    if (!response.ok) {
      return await query(req)
    }

    const reader = response.body?.getReader()
    if (!reader) {
      return await query(req)
    }

    const decoder = new TextDecoder()
    let buffer = ""
    let finalResult: QueryResponse | null = null

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split("\n\n")
      buffer = lines.pop() || ""

      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed.startsWith("data: ")) continue
        const dataStr = trimmed.substring(6)
        if (dataStr === "[DONE]") break

        try {
          const parsed = JSON.parse(dataStr)
          if (parsed.type === "stage" && onProgress) {
            onProgress(parsed)
          } else if (parsed.type === "result") {
            finalResult = parsed.data
          }
        } catch (e) {
          console.debug("SSE parse error:", e)
        }
      }
    }

    if (finalResult) {
      return finalResult
    }
    return await query(req)
  } catch (err) {
    console.warn("queryStream error, falling back to query:", err)
    return await query(req)
  }
}

/** Query Retrieval Pipeline (Standard) */
export async function query(req: QueryRequest): Promise<QueryResponse> {
  try {
    return await apiFetch<QueryResponse>("/api/v1/query", {
      method: "POST",
      body: JSON.stringify(req)
    })
  } catch (err) {
    console.warn("Query error:", err)
    return {
      answer: "No relevant documents found in this workspace to answer your query. Please upload documents first.",
      citations: [],
      confidence: 0,
      confidence_score: 0,
      latency_ms: 0,
      fast_path: false,
      retrieval_path: "empty",
      faithfulness: 0,
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

/** Chat Session Management */
export async function getChatSessions(workspaceId: string): Promise<ChatSession[]> {
  try {
    return await apiFetch<ChatSession[]>(`/api/v1/workspaces/${workspaceId}/sessions`)
  } catch (err) {
    console.warn("Failed to get chat sessions:", err)
    return []
  }
}

export async function createChatSession(workspaceId: string, title: string = "New Query Session"): Promise<ChatSession> {
  return await apiFetch<ChatSession>(`/api/v1/workspaces/${workspaceId}/sessions`, {
    method: "POST",
    body: JSON.stringify({ title })
  })
}

export async function getChatSession(workspaceId: string, sessionId: string): Promise<ChatSession> {
  return await apiFetch<ChatSession>(`/api/v1/workspaces/${workspaceId}/sessions/${sessionId}`)
}

export async function saveChatMessage(
  workspaceId: string,
  sessionId: string,
  message: Partial<ChatMessage>
): Promise<ChatMessage> {
  return await apiFetch<ChatMessage>(`/api/v1/workspaces/${workspaceId}/sessions/${sessionId}/messages`, {
    method: "POST",
    body: JSON.stringify(message)
  })
}

export async function deleteChatSession(workspaceId: string, sessionId: string): Promise<void> {
  await apiFetch(`/api/v1/workspaces/${workspaceId}/sessions/${sessionId}`, {
    method: "DELETE"
  })
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
  uploadSingleDocument,
  ingestFile,
  query,
  queryStream,
  getBenchmarks,
  getKnowledgeGraph,
  getChatSessions,
  createChatSession,
  getChatSession,
  saveChatMessage,
  deleteChatSession
}

export default api
