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
  KnowledgeGraphResponse
} from "../types/api"

const BASE_URL = "/api/v1"

function getAuthHeader(): Record<string, string> {
  const token = localStorage.getItem("kre_token")
  return token ? { Authorization: `Bearer ${token}` } : {}
}

// Initial Mock Data mirroring API.md and UI screenshots
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
  status: "FAILING TARGETS",
  version: "v2.4.1",
  kpis: {
    p95_latency: {
      value: 1.8,
      unit: "s",
      target: 1.4,
      delta: "+0.4s vs target",
      status: "failing"
    },
    recall_5: {
      value: 94,
      unit: "%",
      target: 85,
      delta: "+2% vs target",
      status: "passing"
    },
    faithfulness: {
      value: 98,
      unit: "%",
      target: 100,
      delta: "On target",
      status: "passing"
    },
    llm_activation: {
      value: 12,
      unit: "%",
      target: 15,
      delta: "-3% vs baseline (cache hit)",
      status: "passing"
    }
  },
  latency_chart: {
    target_line: 1.4,
    data_points: [
      { timestamp: "Mon", latency_ms: 0.6 },
      { timestamp: "Tue", latency_ms: 0.55 },
      { timestamp: "Wed", latency_ms: 0.9 },
      { timestamp: "Thu", latency_ms: 1.2 },
      { timestamp: "Fri", latency_ms: 1.6 },
      { timestamp: "Sat", latency_ms: 2.1 },
      { timestamp: "Sun", latency_ms: 1.9 }
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
      type: "Document"
    },
    {
      id: "comp_1",
      label: "TechFlow Inc.",
      type: "Company"
    },
    {
      id: "skill_1",
      label: "PyTorch & Transformers",
      type: "Technology"
    },
    {
      id: "skill_2",
      label: "Predictive Modeling",
      type: "Technology"
    }
  ],
  edges: [
    {
      source: "cand_1",
      target: "doc_1",
      label: "MENTIONED_IN",
      weight: 1.0
    },
    {
      source: "cand_1",
      target: "comp_1",
      label: "WORKED_AT",
      weight: 0.95
    },
    {
      source: "cand_1",
      target: "skill_1",
      label: "SKILLED_IN",
      weight: 0.98
    },
    {
      source: "cand_1",
      target: "skill_2",
      label: "DEPLOYED",
      weight: 0.9
    }
  ]
}

export const api = {
  // 1. Auth
  login: async (credentials: LoginRequest): Promise<AuthResponse> => {
    try {
      const res = await fetch(`${BASE_URL}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(credentials)
      })
      if (res.ok) {
        return await res.json()
      }
    } catch {
      // Backend not running, use mock response
    }
    // Mock user login
    await new Promise((r) => setTimeout(r, 600))
    return {
      access_token: "mock_jwt_token_" + Date.now(),
      token_type: "bearer",
      expires_in: 3600,
      user: {
        id: "usr_54321",
        email: credentials.email || "alexandra.chen@enterprise.com",
        name: "Alexandra Chen",
        role: "Senior Designer"
      }
    }
  },

  // 2. Workspaces
  getWorkspaces: async (): Promise<{ workspaces: Workspace[] }> => {
    try {
      const res = await fetch(`${BASE_URL}/workspaces`, {
        headers: { ...getAuthHeader() }
      })
      if (res.ok) return await res.json()
    } catch {
      // fallback
    }
    await new Promise((r) => setTimeout(r, 400))
    const stored = localStorage.getItem("kre_workspaces")
    if (stored) {
      try {
        return { workspaces: JSON.parse(stored) }
      } catch {}
    }
    return { workspaces: MOCK_WORKSPACES }
  },

  createWorkspace: async (data: CreateWorkspaceRequest): Promise<Workspace> => {
    try {
      const res = await fetch(`${BASE_URL}/workspaces`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getAuthHeader() },
        body: JSON.stringify(data)
      })
      if (res.ok) return await res.json()
    } catch {
      // fallback
    }
    await new Promise((r) => setTimeout(r, 500))
    const newWs: Workspace = {
      id: "ws_" + Date.now().toString(36),
      name: data.name,
      industry: data.industry,
      description: data.description,
      document_count: 0,
      last_active: "Active just now",
      status: "active",
      icon_type: data.industry.toLowerCase().includes("tech")
        ? "engineering"
        : data.industry.toLowerCase().includes("legal")
        ? "legal"
        : "general",
      created_at: new Date().toISOString()
    }
    return newWs
  },

  // 3. Documents
  getDocuments: async (
    workspaceId: string,
    page: number = 1,
    limit: number = 10
  ): Promise<DocumentLibraryResponse> => {
    try {
      const res = await fetch(
        `${BASE_URL}/workspaces/${workspaceId}/documents?page=${page}&limit=${limit}`,
        { headers: { ...getAuthHeader() } }
      )
      if (res.ok) return await res.json()
    } catch {
      // fallback
    }
    await new Promise((r) => setTimeout(r, 450))
    return MOCK_DOCUMENTS
  },

  uploadDocuments: async (
    workspaceId: string,
    files: File[]
  ): Promise<DocumentUploadResponse> => {
    try {
      const formData = new FormData()
      files.forEach((f) => formData.append("files", f))

      const res = await fetch(`${BASE_URL}/workspaces/${workspaceId}/documents`, {
        method: "POST",
        headers: { ...getAuthHeader() },
        body: formData
      })
      if (res.ok) return await res.json()
    } catch {
      // fallback
    }
    await new Promise((r) => setTimeout(r, 800))
    return {
      uploaded_documents: files.map((f, i) => ({
        id: `doc_uuid_${Date.now()}_${i}`,
        filename: f.name,
        format: f.name.split(".").pop() || "pdf",
        status: "processing"
      }))
    }
  },

  // 4. Query Pipeline
  query: async (req: QueryRequest): Promise<QueryResponse> => {
    try {
      const res = await fetch(`${BASE_URL}/query`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getAuthHeader() },
        body: JSON.stringify(req)
      })
      if (res.ok) return await res.json()
    } catch {
      // fallback
    }
    await new Promise((r) => setTimeout(r, 1200))
    return {
      answer:
        "Candidate A has 4 years of applied machine learning experience [1]. At TechFlow Inc., they led the migration of their primary recommendation engine to KRE AI .\n\nThey successfully deployed three distinct predictive models into production environments serving over 10k requests/min. Notably, their implementation of a custom transformer architecture reduced inference latency by 22% [2].",
      citations: [
        {
          id: 1,
          chunk_id: "chunk_uuid_1",
          document_id: "doc_uuid_1",
          document_filename: "Candidate_A_Resume_Final.pdf",
          source_format: "pdf",
          text: "Senior Product Designer | InnovateTech Solutions (2019–Present) - 4 years applied ML experience.",
          page_number: 1,
          bounding_box: { l: 80, t: 260, r: 880, b: 320 },
          location_reference: "Page 1, Para 1"
        },
        {
          id: 2,
          chunk_id: "chunk_uuid_2",
          document_id: "doc_uuid_1",
          document_filename: "Candidate_A_Resume_Final.pdf",
          source_format: "pdf",
          text: "Deployed three distinct predictive models into production serving over 10k requests/min. Custom transformer latency reduction by 22%.",
          page_number: 1,
          bounding_box: { l: 80, t: 370, r: 940, b: 440 },
          location_reference: "Page 1, Para 2"
        }
      ],
      retrieval_path: "fast",
      confidence: 0.98,
      latency_ms: 1200,
      faithfulness: 98
    }
  },

  // 6. System Benchmarks
  getBenchmarks: async (): Promise<BenchmarkResponse> => {
    try {
      const res = await fetch(`${BASE_URL}/system/benchmarks`, {
        headers: { ...getAuthHeader() }
      })
      if (res.ok) return await res.json()
    } catch {
      // fallback
    }
    await new Promise((r) => setTimeout(r, 400))
    return MOCK_BENCHMARKS
  },

  // 7. Knowledge Graph
  getKnowledgeGraph: async (workspaceId: string): Promise<KnowledgeGraphResponse> => {
    try {
      const res = await fetch(`${BASE_URL}/workspaces/${workspaceId}/graph`, {
        headers: { ...getAuthHeader() }
      })
      if (res.ok) return await res.json()
    } catch {
      // fallback
    }
    await new Promise((r) => setTimeout(r, 500))
    return MOCK_GRAPH
  }
}
