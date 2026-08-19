import { create } from "zustand"
import type { DocumentItem } from "../types/api"
import { api } from "../lib/api"

export interface FileUploadState {
  filename: string
  progress: number  // 0–100
  status: "pending" | "uploading" | "done" | "error"
  error?: string
}

interface DocumentState {
  documents: DocumentItem[]
  totalDocuments: number
  currentPage: number
  totalPages: number
  isLoading: boolean
  isUploading: boolean
  /** Per-file upload state keyed by filename (or unique key) */
  fileUploads: FileUploadState[]
  error: string | null
  fetchDocuments: (workspaceId: string, page?: number) => Promise<void>
  uploadFiles: (workspaceId: string, files: File[]) => Promise<boolean>
  setPage: (page: number) => void
}

export const useDocumentStore = create<DocumentState>((set) => ({
  documents: [],
  totalDocuments: 0,
  currentPage: 1,
  totalPages: 1,
  isLoading: false,
  isUploading: false,
  fileUploads: [],
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
    if (files.length === 0) return false

    // Initialise per-file upload state
    const initialUploads: FileUploadState[] = files.map((f) => ({
      filename: f.name,
      progress: 0,
      status: "pending"
    }))
    set({ isUploading: true, fileUploads: initialUploads, error: null })

    const updateFile = (index: number, patch: Partial<FileUploadState>) => {
      set((state) => {
        const next = [...state.fileUploads]
        next[index] = { ...next[index], ...patch }
        return { fileUploads: next }
      })
    }

    // Upload every file in parallel
    const results = await Promise.allSettled(
      files.map((file, index) => {
        updateFile(index, { status: "uploading", progress: 0 })

        return api.uploadSingleDocument(workspaceId, file, (loaded, total) => {
          const pct = total > 0 ? Math.round((loaded / total) * 100) : 0
          updateFile(index, { progress: pct })
        }).then((res) => {
          updateFile(index, { status: "done", progress: 100 })
          return res
        }).catch((err: any) => {
          updateFile(index, { status: "error", error: err?.message || "Upload failed" })
          throw err
        })
      })
    )

    // Collect successfully uploaded docs and prepend to the document list
    const newDocs: DocumentItem[] = []
    results.forEach((result) => {
      if (result.status === "fulfilled") {
        result.value.uploaded_documents.forEach((ud) => {
          newDocs.push({
            id: ud.id,
            filename: ud.filename,
            format: ud.format,
            upload_date: "Just now",
            chunk_count: 0,
            status: "Processing",
            size: "Uploaded"
          })
        })
      }
    })

    const anySucceeded = newDocs.length > 0
    if (anySucceeded) {
      set((state) => ({
        documents: [...newDocs, ...state.documents],
        totalDocuments: state.totalDocuments + newDocs.length
      }))
    }

    const anyFailed = results.some((r) => r.status === "rejected")
    set({ isUploading: false })

    return anySucceeded && !anyFailed
  }
}))
