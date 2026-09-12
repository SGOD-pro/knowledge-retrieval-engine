import { create } from "zustand"
import type { ChatSession, Citation, KnowledgeGraphResponse, ChatMessage } from "../types/api"
import { api, type QueryStreamStageEvent } from "../lib/api"

interface ChatState {
  sessions: ChatSession[]
  activeSessionId: string
  isQuerying: boolean
  isLoadingSessions: boolean
  currentStage: QueryStreamStageEvent | null
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
  setActiveSessionId: (id: string, workspaceId?: string) => Promise<void>
  setActiveCitation: (citation: Citation | null) => void
  setRightPaneMode: (mode: "document" | "graph") => void
  setRightPaneOpen: (open: boolean) => void
  setLeftPaneOpen: (open: boolean) => void
  setDocPaneWidth: (width: number) => void
  setZoomLevel: (zoom: number | ((prev: number) => number)) => void
  setCurrentPage: (page: number) => void
  loadWorkspaceSessions: (workspaceId: string) => Promise<string>
  ensureSession: (workspaceId: string) => Promise<string>
  createNewChat: (workspaceId: string, title?: string) => Promise<string>
  deleteSession: (workspaceId: string, sessionId: string) => Promise<void>
  sendMessage: (workspaceId: string, text: string) => Promise<void>
  fetchGraph: (workspaceId: string) => Promise<void>
}

export const useChatStore = create<ChatState>((set, get) => ({
  sessions: [],
  activeSessionId: "",
  isQuerying: false,
  isLoadingSessions: false,
  currentStage: null,
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

  setActiveSessionId: async (activeSessionId, workspaceId) => {
    const wsId = workspaceId || get().sessions.find((s) => s.id === activeSessionId)?.workspaceId
    set({ activeSessionId })

    if (wsId) {
      try {
        const fullSession = await api.getChatSession(wsId, activeSessionId)
        if (fullSession && fullSession.messages) {
          set((state) => ({
            sessions: state.sessions.map((s) =>
              s.id === activeSessionId
                ? {
                    ...s,
                    title: fullSession.title || s.title,
                    messages: fullSession.messages.map((m: any) => ({
                      ...m,
                      id: m.id || `msg_${Date.now()}`
                    }))
                  }
                : s
            )
          }))
          const firstCitation = fullSession.messages.find(
            (m: any) => m.citations && m.citations.length > 0
          )?.citations?.[0] || null
          if (firstCitation) {
            set({ activeCitation: firstCitation })
          }
        }
      } catch (err) {
        console.warn("Failed to load session messages:", err)
      }
    }
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

  loadWorkspaceSessions: async (workspaceId: string) => {
    set({ isLoadingSessions: true })
    try {
      const backendSessions = await api.getChatSessions(workspaceId)
      if (backendSessions && backendSessions.length > 0) {
        const formatted: ChatSession[] = backendSessions.map((s: any) => ({
          id: s.id,
          workspaceId: s.workspace_id || workspaceId,
          title: s.title || "New Query Session",
          category: (s.category || "Today") as any,
          updatedAt: s.updated_at || "Just now",
          messages: (s.messages || []).map((m: any) => ({
            id: m.id,
            sender: m.sender,
            text: m.text,
            timestamp: m.timestamp,
            citations: m.citations,
            retrieval_path: m.retrieval_path,
            confidence: m.confidence,
            latency_ms: m.latency_ms,
            faithfulness: m.faithfulness
          }))
        }))

        const firstId = formatted[0].id
        set({
          sessions: formatted,
          activeSessionId: firstId,
          isLoadingSessions: false
        })

        // Fetch full message list for first session
        await get().setActiveSessionId(firstId, workspaceId)
        return firstId
      }
    } catch (err) {
      console.warn("Error loading workspace sessions from backend:", err)
    }

    // If none exist, create one
    set({ isLoadingSessions: false })
    return await get().createNewChat(workspaceId, "New Query Session")
  },

  ensureSession: async (workspaceId: string) => {
    const existing = get().sessions.filter((s) => s.workspaceId === workspaceId)
    if (existing.length > 0) {
      const activeId = get().activeSessionId
      if (!activeId || !existing.some((s) => s.id === activeId)) {
        const targetId = existing[0].id
        await get().setActiveSessionId(targetId, workspaceId)
        return targetId
      }
      return activeId
    }
    return await get().loadWorkspaceSessions(workspaceId)
  },

  createNewChat: async (workspaceId, title = "New Query Session") => {
    try {
      const newBackendSession = await api.createChatSession(workspaceId, title)
      const newSession: ChatSession = {
        id: newBackendSession.id || `session_${Date.now()}`,
        workspaceId,
        title: newBackendSession.title || title,
        category: "Today",
        updatedAt: "Just now",
        messages: []
      }

      set({
        sessions: [newSession, ...get().sessions.filter((s) => s.id !== newSession.id)],
        activeSessionId: newSession.id,
        activeCitation: null
      })
      return newSession.id
    } catch (err) {
      console.warn("Failed to create session on backend, using local fallback:", err)
      const localId = `session_${Date.now()}`
      const localSession: ChatSession = {
        id: localId,
        workspaceId,
        title,
        category: "Today",
        updatedAt: "Just now",
        messages: []
      }
      set({
        sessions: [localSession, ...get().sessions],
        activeSessionId: localId,
        activeCitation: null
      })
      return localId
    }
  },

  deleteSession: async (workspaceId: string, sessionId: string) => {
    try {
      await api.deleteChatSession(workspaceId, sessionId)
    } catch (err) {
      console.warn("Failed to delete session on backend:", err)
    }

    const remaining = get().sessions.filter((s) => s.id !== sessionId)
    set({ sessions: remaining })

    if (get().activeSessionId === sessionId) {
      const nextWsSession = remaining.find((s) => s.workspaceId === workspaceId)
      if (nextWsSession) {
        await get().setActiveSessionId(nextWsSession.id, workspaceId)
      } else {
        await get().createNewChat(workspaceId, "New Query Session")
      }
    }
  },

  sendMessage: async (workspaceId, text) => {
    let activeId = get().activeSessionId
    if (!activeId) {
      activeId = await get().createNewChat(workspaceId)
    }

    const userMsg: ChatMessage = {
      id: "msg_" + Date.now(),
      sender: "user",
      text,
      timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    }

    // Auto update title from first message if default
    set((state) => ({
      isQuerying: true,
      currentStage: {
        type: "stage",
        stage: "listening",
        state: "listening",
        label: "Analyzing query & planning retrieval strategy...",
        progress: 15
      },
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

    // Persist user message to backend
    api.saveChatMessage(workspaceId, activeId, userMsg).catch((err) => {
      console.warn("Failed to save user message to backend:", err)
    })

    try {
      const response = await api.queryStream(
        { workspace_id: workspaceId, query: text },
        (stage) => set({ currentStage: stage })
      )
      const aiMsg: ChatMessage = {
        id: "msg_ai_" + Date.now(),
        sender: "assistant",
        text: response.answer,
        timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
        citations: response.citations,
        retrieval_path: response.retrieval_path === "fast" ? "Fast Match" : "Full Pipeline",
        confidence: response.confidence_score || response.confidence,
        latency_ms: response.latency_ms,
        faithfulness: response.faithfulness ?? null
      }

      set((state) => ({
        isQuerying: false,
        currentStage: null,
        activeCitation: response.citations?.[0] || state.activeCitation,
        sessions: state.sessions.map((s) =>
          s.id === activeId ? { ...s, messages: [...s.messages, aiMsg] } : s
        )
      }))

      // Persist assistant message to backend
      api.saveChatMessage(workspaceId, activeId, aiMsg).catch((err) => {
        console.warn("Failed to save assistant message to backend:", err)
      })
    } catch (err: any) {
      const errorMsg: ChatMessage = {
        id: "msg_err_" + Date.now(),
        sender: "assistant",
        text: `Unable to complete retrieval: ${err?.message || "Internal server error"}. Please ensure documents are uploaded and processed.`,
        timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      }
      set((state) => ({
        isQuerying: false,
        currentStage: null,
        sessions: state.sessions.map((s) =>
          s.id === activeId ? { ...s, messages: [...s.messages, errorMsg] } : s
        )
      }))

      api.saveChatMessage(workspaceId, activeId, errorMsg).catch(() => {})
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
