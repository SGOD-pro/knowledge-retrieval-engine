import { create } from "zustand"
import type { DocumentItem } from "../types/api"
import { api, MOCK_DOCUMENTS } from "../lib/api"

interface DocumentState {
  documents: DocumentItem[]
  totalDocuments: number
  currentPage: number
  totalPages: number
  isLoading: boolean
  isUploading: boolean
  uploadProgress: number
  error: string | null
  fetchDocuments: (workspaceId: string, page?: number) => Promise<void>
  uploadFiles: (workspaceId: string, files: File[]) => Promise<boolean>
  setPage: (page: number) => void
}

export const useDocumentStore = create<DocumentState>((set, get) => ({
  documents: MOCK_DOCUMENTS.documents,
  totalDocuments: MOCK_DOCUMENTS.total_documents,
  currentPage: 1,
  totalPages: MOCK_DOCUMENTS.total_pages,
  isLoading: false,
  isUploading: false,
  uploadProgress: 0,
  error: null,

  setPage: (page) => set({ currentPage: page }),

  fetchDocuments: async (workspaceId, page = 1) => {
    set({ isLoading: true, error: null })
    try {
      const res = await api.getDocuments(workspaceId, page)
      set({
        documents: res.documents,
        totalDocuments: res.total_documents,
        currentPage: res.current_page,
        totalPages: res.total_pages,
        isLoading: false
      })
    } catch (err: any) {
      set({ error: err.message, isLoading: false })
    }
  },

  uploadFiles: async (workspaceId, files) => {
    set({ isUploading: true, uploadProgress: 10, error: null })
    try {
      const interval = setInterval(() => {
        set((state) => ({
          uploadProgress: Math.min(state.uploadProgress + 25, 90)
        }))
      }, 200)

      const res = await api.uploadDocuments(workspaceId, files)
      clearInterval(interval)

      const newDocs: DocumentItem[] = res.uploaded_documents.map((ud) => ({
        id: ud.id,
        filename: ud.filename,
        format: ud.format,
        upload_date: "Just now",
        chunk_count: 0,
        status: "Processing",
        size: "Uploaded"
      }))

      set({
        documents: [...newDocs, ...get().documents],
        totalDocuments: get().totalDocuments + newDocs.length,
        isUploading: false,
        uploadProgress: 100
      })
      return true
    } catch (err: any) {
      set({ error: err.message, isUploading: false, uploadProgress: 0 })
      return false
    }
  }
}))
