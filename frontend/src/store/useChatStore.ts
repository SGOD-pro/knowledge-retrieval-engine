import { create } from "zustand"
import type { ChatSession, Citation, KnowledgeGraphResponse } from "../types/api"
import { api, MOCK_GRAPH } from "../lib/api"

interface ChatState {
  sessions: ChatSession[]
  activeSessionId: string
  isQuerying: boolean
  activeCitation: Citation | null
  rightPaneMode: "document" | "graph"
  rightPaneOpen: boolean
  leftPaneOpen: boolean
  docPaneWidth: number
  graphData: KnowledgeGraphResponse | null
  zoomLevel: number
  currentPage: number
  totalPages: number
  searchFilter: string

  setSearchFilter: (filter: string) => void
  setActiveSessionId: (id: string) => void
  setActiveCitation: (citation: Citation | null) => void
  setRightPaneMode: (mode: "document" | "graph") => void
  setRightPaneOpen: (open: boolean) => void
  setLeftPaneOpen: (open: boolean) => void
  setDocPaneWidth: (width: number) => void
  setZoomLevel: (zoom: number | ((prev: number) => number)) => void
  setCurrentPage: (page: number) => void
  createNewChat: (workspaceId: string, title?: string) => string
  sendMessage: (workspaceId: string, text: string) => Promise<void>
  fetchGraph: (workspaceId: string) => Promise<void>
}

const INITIAL_SESSIONS: ChatSession[] = [
  {
    id: "session_1",
    workspaceId: "ws_001",
    title: "Data Scientist Pipeline",
    category: "Today",
    updatedAt: "10 mins ago",
    messages: [
      {
        id: "msg_1",
        sender: "user",
        text: "Summarize the machine learning experience for Candidate A, focusing specifically on their work with PyTorch and deployed models.",
        timestamp: "10:24 AM"
      },
      {
        id: "msg_2",
        sender: "assistant",
        text: "Candidate A has 4 years of applied machine learning experience [1]. At TechFlow Inc., they led the migration of their primary recommendation engine to KRE AI .\n\nThey successfully deployed three distinct predictive models into production environments serving over 10k requests/min. Notably, their implementation of a custom transformer architecture reduced inference latency by 22% [2].",
        timestamp: "10:25 AM",
        latency_ms: 1200,
        faithfulness: 98,
        retrieval_path: "Fast Match (1.2s)",
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
        ]
      }
    ]
  },
  {
    id: "session_2",
    workspaceId: "ws_001",
    title: "UI/UX Screenings",
    category: "Today",
    updatedAt: "2 hours ago",
    messages: [
      {
        id: "msg_2_1",
        sender: "user",
        text: "What are the core design systems and user research competencies listed in the design portfolios?",
        timestamp: "08:15 AM"
      }
    ]
  },
  {
    id: "session_3",
    workspaceId: "ws_001",
    title: "Q3 Backend Batch",
    category: "Previous 7 Days",
    updatedAt: "4 days ago",
    messages: [
      {
        id: "msg_3_1",
        sender: "user",
        text: "Compare backend systems architecture experience across candidates B and C.",
        timestamp: "Aug 10"
      }
    ]
  }
]

export const useChatStore = create<ChatState>((set, get) => ({
  sessions: INITIAL_SESSIONS,
  activeSessionId: "session_1",
  isQuerying: false,
  activeCitation: INITIAL_SESSIONS[0].messages[1]?.citations?.[0] || null,
  rightPaneMode: "document",
  rightPaneOpen: true,
  leftPaneOpen: true,
  docPaneWidth: 500,
  graphData: MOCK_GRAPH,
  zoomLevel: 100,
  currentPage: 1,
  totalPages: 12,
  searchFilter: "",

  setSearchFilter: (searchFilter) => set({ searchFilter }),

  setActiveSessionId: (activeSessionId) => {
    const session = get().sessions.find((s) => s.id === activeSessionId)
    const firstCitation = session?.messages.find((m) => m.citations && m.citations.length > 0)?.citations?.[0] || null
    set({
      activeSessionId,
      activeCitation: firstCitation
    })
  },

  setActiveCitation: (activeCitation) => {
    set({
      activeCitation,
      rightPaneOpen: true,
      currentPage: activeCitation?.page_number || 1
    })
  },

  setRightPaneMode: (rightPaneMode) => set({ rightPaneMode, rightPaneOpen: true }),

  setRightPaneOpen: (rightPaneOpen) => set({ rightPaneOpen }),

  setLeftPaneOpen: (leftPaneOpen) => set({ leftPaneOpen }),

  setDocPaneWidth: (docPaneWidth) => set({ docPaneWidth }),

  setZoomLevel: (zoom) =>
    set((state) => ({
      zoomLevel: typeof zoom === "function" ? zoom(state.zoomLevel) : zoom
    })),

  setCurrentPage: (currentPage) => set({ currentPage }),

  createNewChat: (workspaceId, title = "New Candidate Screening") => {
    const newSession: ChatSession = {
      id: "session_" + Date.now(),
      workspaceId,
      title,
      category: "Today",
      updatedAt: "Just now",
      messages: []
    }
    set({
      sessions: [newSession, ...get().sessions],
      activeSessionId: newSession.id,
      activeCitation: null
    })
    return newSession.id
  },

  sendMessage: async (workspaceId, text) => {
    const activeId = get().activeSessionId
    const userMsg = {
      id: "msg_" + Date.now(),
      sender: "user" as const,
      text,
      timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    }

    set((state) => ({
      isQuerying: true,
      sessions: state.sessions.map((s) =>
        s.id === activeId ? { ...s, messages: [...s.messages, userMsg] } : s
      )
    }))

    try {
      const response = await api.query({ workspace_id: workspaceId, query: text })
      const aiMsg = {
        id: "msg_ai_" + Date.now(),
        sender: "assistant" as const,
        text: response.answer,
        timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
        citations: response.citations,
        retrieval_path: response.retrieval_path === "fast" ? "Fast Match (1.2s)" : "Full Pipeline",
        confidence: response.confidence,
        latency_ms: response.latency_ms,
        faithfulness: response.faithfulness || 98
      }

      set((state) => ({
        isQuerying: false,
        activeCitation: response.citations?.[0] || state.activeCitation,
        sessions: state.sessions.map((s) =>
          s.id === activeId ? { ...s, messages: [...s.messages, aiMsg] } : s
        )
      }))
    } catch {
      set({ isQuerying: false })
    }
  },

  fetchGraph: async (workspaceId) => {
    try {
      const graph = await api.getKnowledgeGraph(workspaceId)
      set({ graphData: graph })
    } catch {
      set({ graphData: MOCK_GRAPH })
    }
  }
}))
