import { create } from "zustand"
import type { Workspace, CreateWorkspaceRequest } from "../types/api"
import { api, MOCK_WORKSPACES } from "../lib/api"

interface WorkspaceState {
  workspaces: Workspace[]
  activeWorkspace: Workspace | null
  searchQuery: string
  isLoading: boolean
  error: string | null
  setSearchQuery: (query: string) => void
  setActiveWorkspace: (workspace: Workspace | null) => void
  fetchWorkspaces: () => Promise<void>
  createWorkspace: (data: CreateWorkspaceRequest) => Promise<Workspace | null>
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  workspaces: MOCK_WORKSPACES,
  activeWorkspace: MOCK_WORKSPACES[0],
  searchQuery: "",
  isLoading: false,
  error: null,

  setSearchQuery: (searchQuery) => set({ searchQuery }),

  setActiveWorkspace: (activeWorkspace) => set({ activeWorkspace }),

  fetchWorkspaces: async () => {
    set({ isLoading: true, error: null })
    try {
      const res = await api.getWorkspaces()
      set({
        workspaces: res.workspaces,
        activeWorkspace: get().activeWorkspace || res.workspaces[0] || null,
        isLoading: false
      })
    } catch (err: any) {
      set({ error: err.message, isLoading: false })
    }
  },

  createWorkspace: async (data) => {
    set({ isLoading: true, error: null })
    try {
      const newWs = await api.createWorkspace(data)
      const updated = [newWs, ...get().workspaces]
      localStorage.setItem("kre_workspaces", JSON.stringify(updated))
      set({
        workspaces: updated,
        activeWorkspace: newWs,
        isLoading: false
      })
      return newWs
    } catch (err: any) {
      set({ error: err.message, isLoading: false })
      return null
    }
  }
}))
