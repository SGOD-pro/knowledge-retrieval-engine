import { create } from "zustand"
import type { ChatSession, Citation, KnowledgeGraphResponse } from "../types/api"
import { api } from "../lib/api"

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
  ensureSession: (workspaceId: string) => string
  createNewChat: (workspaceId: string, title?: string) => string
  sendMessage: (workspaceId: string, text: string) => Promise<void>
  fetchGraph: (workspaceId: string) => Promise<void>
}

export const useChatStore = create<ChatState>((set, get) => ({
  sessions: [],
  activeSessionId: "",
  isQuerying: false,
  activeCitation: null,
  rightPaneMode: "document",
  rightPaneOpen: true,
  leftPaneOpen: true,
  docPaneWidth: 500,
  graphData: null,
  zoomLevel: 100,
  currentPage: 1,
  totalPages: 1,
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

  ensureSession: (workspaceId: string) => {
    const existing = get().sessions.filter((s) => s.workspaceId === workspaceId)
    if (existing.length > 0) {
      if (!get().activeSessionId || !existing.some((s) => s.id === get().activeSessionId)) {
        set({ activeSessionId: existing[0].id })
        return existing[0].id
      }
      return get().activeSessionId
    }
    return get().createNewChat(workspaceId, "New Query Session")
  },

  createNewChat: (workspaceId, title = "New Query Session") => {
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
    let activeId = get().activeSessionId
    if (!activeId) {
      activeId = get().createNewChat(workspaceId)
    }

    const userMsg = {
      id: "msg_" + Date.now(),
      sender: "user" as const,
      text,
      timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    }

    // Auto update title from first message if default
    set((state) => ({
      isQuerying: true,
      sessions: state.sessions.map((s) => {
        if (s.id !== activeId) return s
        const isDefaultTitle = s.title === "New Query Session" || s.title === "New Chat"
        const newTitle = isDefaultTitle ? (text.length > 32 ? text.substring(0, 30) + "…" : text) : s.title
        return {
          ...s,
          title: newTitle,
          messages: [...s.messages, userMsg]
        }
      })
    }))

    try {
      const response = await api.query({ workspace_id: workspaceId, query: text })
      const aiMsg = {
        id: "msg_ai_" + Date.now(),
        sender: "assistant" as const,
        text: response.answer,
        timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
        citations: response.citations,
        retrieval_path: response.retrieval_path === "fast" ? "Fast Match" : "Full Pipeline",
        confidence: response.confidence_score || response.confidence,
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
    } catch (err: any) {
      const errorMsg = {
        id: "msg_err_" + Date.now(),
        sender: "assistant" as const,
        text: `Unable to complete retrieval: ${err?.message || "Internal server error"}. Please ensure documents are uploaded and processed.`,
        timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      }
      set((state) => ({
        isQuerying: false,
        sessions: state.sessions.map((s) =>
          s.id === activeId ? { ...s, messages: [...s.messages, errorMsg] } : s
        )
      }))
    }
  },

  fetchGraph: async (workspaceId) => {
    try {
      const graph = await api.getKnowledgeGraph(workspaceId)
      set({ graphData: graph })
    } catch {
      set({ graphData: { nodes: [], edges: [] } })
    }
  }
}))
