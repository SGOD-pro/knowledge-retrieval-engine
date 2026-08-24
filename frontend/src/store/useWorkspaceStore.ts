import { create } from "zustand"
import type { Workspace, CreateWorkspaceRequest } from "../types/api"
import { api } from "../lib/api"

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
  deleteWorkspace: (workspaceId: string) => Promise<boolean>
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  workspaces: [],
  activeWorkspace: null,
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
      // Add to Zustand state only after backend confirms creation
      set((state) => ({
        workspaces: [newWs, ...state.workspaces],
        activeWorkspace: newWs,
        isLoading: false
      }))
      return newWs
    } catch (err: any) {
      set({ error: err.message, isLoading: false })
      return null
    }
  },

  deleteWorkspace: async (workspaceId: string) => {
    try {
      await api.deleteWorkspace(workspaceId)
      set((state) => {
        const nextWorkspaces = state.workspaces.filter((w) => w.id !== workspaceId)
        const nextActive =
          state.activeWorkspace?.id === workspaceId
            ? nextWorkspaces[0] || null
            : state.activeWorkspace
        return {
          workspaces: nextWorkspaces,
          activeWorkspace: nextActive
        }
      })
      return true
    } catch (err: any) {
      set({ error: err.message })
      return false
    }
  }
}))
