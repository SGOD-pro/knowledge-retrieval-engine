import { useState, useEffect } from "react"
import { useNavigate, useParams } from "react-router-dom"
import {
  FileText,
  FileUp,
  Bell,
  CheckCircle2,
  RefreshCw,
  AlertCircle,
  ChevronLeft,
  ChevronRight,
  ChevronDown,
  MessageSquare
} from "lucide-react"
import { useWorkspaceStore } from "../store/useWorkspaceStore"
import { useDocumentStore } from "../store/useDocumentStore"
import { TableRowSkeleton } from "../components/common/ShimmerSkeleton"
import { Button } from "../components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger
} from "../components/ui/dropdown-menu"
import { Dialog, DialogContent, DialogTitle } from "../components/ui/dialog"
import { UploadPage } from "./UploadPage"
import type { DocumentItem } from "../types/api"

export function LibraryPage() {
  const navigate = useNavigate()
  const { workspaceId } = useParams<{ workspaceId: string }>()
  const { activeWorkspace, workspaces, setActiveWorkspace } = useWorkspaceStore()
  const {
    documents,
    totalDocuments,
    currentPage,
    totalPages,
    setPage,
    fetchDocuments,
    isLoading
  } = useDocumentStore()

  const [uploadModalOpen, setUploadModalOpen] = useState(false)

  // Sync route workspaceId with store
  useEffect(() => {
    if (workspaceId && activeWorkspace?.id !== workspaceId) {
      const match = workspaces.find((w) => w.id === workspaceId)
      if (match) {
        setActiveWorkspace(match)
      }
    }
  }, [workspaceId, activeWorkspace, workspaces, setActiveWorkspace])

  const currentWsId = workspaceId || activeWorkspace?.id || ""

  useEffect(() => {
    if (currentWsId) {
      fetchDocuments(currentWsId, currentPage)
    }
  }, [currentWsId, currentPage, fetchDocuments])

  // Polling: Auto-refresh library every 2.5s while any document is in processing state
  useEffect(() => {
    if (!currentWsId) return
    const hasProcessing = documents.some((d) => {
      const s = String(d.status || "").toLowerCase()
      return s === "processing" || s === "indexing" || s === "pending"
    })
    if (!hasProcessing) return

    const interval = setInterval(() => {
      fetchDocuments(currentWsId, currentPage)
    }, 2500)

    return () => clearInterval(interval)
  }, [currentWsId, currentPage, documents, fetchDocuments])

  const getFormatBadge = (format: string) => {
    const fmt = format.toUpperCase()
    if (fmt === "PDF") {
      return (
        <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-[#fdeae4] dark:bg-[#3d231b] text-[#c96442] dark:text-[#ffb59d] tracking-wider">
          PDF
        </span>
      )
    }
    return (
      <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-[#ede9de] dark:bg-[#242628] text-foreground/80 tracking-wider">
        {fmt}
      </span>
    )
  }

  const getStatusBadge = (status: DocumentItem["status"]) => {
    const s = String(status || "").toLowerCase()
    if (s === "ready") {
      return (
        <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-[#e2f3ee] dark:bg-[#1a3832] text-[#006768] dark:text-[#6cd7d8]">
          <CheckCircle2 className="h-3.5 w-3.5" />
          <span>Ready</span>
        </span>
      )
    }
    if (s === "processing" || s === "indexing" || s === "pending") {
      return (
        <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-[#ede9de] dark:bg-[#282a2c] text-foreground/80">
          <RefreshCw className="h-3.5 w-3.5 animate-spin" />
          <span>Processing</span>
        </span>
      )
    }
    return (
      <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-[#ffdad6] dark:bg-[#4a1818] text-[#ba1a1a] dark:text-[#ffb4ab]">
        <AlertCircle className="h-3.5 w-3.5" />
        <span>Failed</span>
      </span>
    )
  }

  return (
    <div className="p-8 lg:p-12 max-w-7xl mx-auto space-y-6 animate-in fade-in-50 duration-200">
      {/* Top Breadcrumb & Notification */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs font-semibold text-muted-foreground uppercase tracking-wider">
          <span className="text-[#c96442]">Active Workspace</span>
          <span>/</span>
          <DropdownMenu>
            <DropdownMenuTrigger className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-[#ede9de] dark:bg-[#282a2c] text-foreground font-medium text-xs hover:bg-accent transition-colors cursor-pointer outline-none capitalize">
              <span>{activeWorkspace?.name || "Workspace Alpha"}</span>
              <ChevronDown className="h-3 w-3 opacity-60" />
            </DropdownMenuTrigger>
            <DropdownMenuContent className="bg-popover border-border">
              {workspaces.map((ws) => (
                <DropdownMenuItem
                  key={ws.id}
                  onClick={() => {
                    setActiveWorkspace(ws)
                    navigate(`/library/${ws.id}`)
                  }}
                  className="cursor-pointer"
                >
                  {ws.name}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>

        <button
          type="button"
          className="p-2.5 rounded-full hover:bg-accent text-foreground transition-colors cursor-pointer"
          title="Notifications"
        >
          <Bell className="h-5 w-5" />
        </button>
      </div>

      {/* Main Header & Upload Action */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-2">
        <div className="space-y-1">
          <h1 className="font-headline text-3xl sm:text-4xl font-bold tracking-tight text-foreground">
            Document Library
          </h1>
          <p className="text-sm text-muted-foreground font-sans">
            Manage and review all uploaded candidate materials.
          </p>
        </div>

        <div className="flex items-center gap-3">
          {documents.length > 0 && (
            <Button
              onClick={() => navigate(`/workspaces/${currentWsId}/chat`)}
              className="h-10 px-4 bg-[#c96442] hover:bg-[#b05730] text-white font-semibold rounded-xl text-xs flex items-center gap-2 shadow-xs cursor-pointer transition-colors"
            >
              <MessageSquare className="h-4 w-4" />
              <span>Go to Chat</span>
            </Button>
          )}
          <Button
            onClick={() => setUploadModalOpen(true)}
            className="h-10 px-5 bg-[#ede9de] dark:bg-[#282a2c] hover:bg-[#e2ded2] dark:hover:bg-[#343638] text-foreground font-semibold rounded-xl text-xs flex items-center gap-2 border border-border/60 shadow-xs cursor-pointer transition-colors"
          >
            <FileUp className="h-4 w-4 text-[#c96442]" />
            <span>Upload Files</span>
          </Button>
        </div>
      </div>

      {/* Documents Table */}
      <div className="rounded-2xl border border-border/80 bg-card overflow-hidden shadow-xs">
        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="border-b border-border/70 bg-card/40 text-[11px] font-bold text-muted-foreground uppercase tracking-wider">
                <th className="py-4 px-6">Filename</th>
                <th className="py-4 px-4">Format</th>
                <th className="py-4 px-4">Upload Date</th>
                <th className="py-4 px-4 text-center">Chunks</th>
                <th className="py-4 px-6 text-right">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40 text-xs text-foreground font-sans">
              {isLoading ? (
                <>
                  <tr>
                    <td colSpan={5} className="p-0">
                      <TableRowSkeleton />
                    </td>
                  </tr>
                  <tr>
                    <td colSpan={5} className="p-0">
                      <TableRowSkeleton />
                    </td>
                  </tr>
                  <tr>
                    <td colSpan={5} className="p-0">
                      <TableRowSkeleton />
                    </td>
                  </tr>
                </>
              ) : documents.length === 0 ? (
                <tr>
                  <td colSpan={5} className="py-16 text-center text-muted-foreground">
                    <FileText className="h-10 w-10 mx-auto mb-3 opacity-30 text-muted-foreground" />
                    <p className="text-sm font-semibold text-foreground">No documents uploaded yet</p>
                    <p className="text-xs text-muted-foreground mt-1 max-w-sm mx-auto">
                      Upload PDF, DOCX, or PPTX documents to this workspace to start indexing and testing the retrieval engine.
                    </p>
                  </td>
                </tr>
              ) : (
                documents.map((doc) => (
                  <tr
                    key={doc.id}
                    className="hover:bg-accent/40 transition-colors group cursor-pointer"
                    onClick={() => navigate(`/library/${currentWsId}/document/${doc.id}`)}
                  >
                    {/* Filename */}
                    <td className="py-4 px-6 font-medium">
                      <div className="flex items-center gap-3">
                        <FileText className="h-4 w-4 text-[#c96442] shrink-0" />
                        <span className="group-hover:text-primary transition-colors line-clamp-1">
                          {doc.filename}
                        </span>
                      </div>
                    </td>

                    {/* Format */}
                    <td className="py-4 px-4">{getFormatBadge(doc.format)}</td>

                    {/* Upload Date */}
                    <td className="py-4 px-4 text-muted-foreground font-normal">
                      {doc.upload_date}
                    </td>

                    {/* Chunks */}
                    <td className="py-4 px-4 text-center text-muted-foreground font-normal">
                      {doc.chunk_count > 0 ? doc.chunk_count : "--"}
                    </td>

                    {/* Status */}
                    <td className="py-4 px-6 text-right">
                      {getStatusBadge(doc.status)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Footer Pagination */}
        <div className="py-4 px-6 border-t border-border/60 flex items-center justify-between text-xs text-muted-foreground">
          <span>
            Showing 1-{documents.length} of {totalDocuments} documents
          </span>
          <div className="flex items-center gap-1">
            <button
              type="button"
              disabled={currentPage <= 1}
              onClick={() => setPage(Math.max(1, currentPage - 1))}
              className="p-1.5 rounded-lg hover:bg-accent disabled:opacity-30 disabled:hover:bg-transparent cursor-pointer transition-colors"
            >
              <ChevronLeft className="h-4 w-4" />
            </button>
            <button
              type="button"
              disabled={currentPage >= totalPages}
              onClick={() => setPage(Math.min(totalPages, currentPage + 1))}
              className="p-1.5 rounded-lg hover:bg-accent disabled:opacity-30 disabled:hover:bg-transparent cursor-pointer transition-colors"
            >
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        </div>
      </div>

      {/* Upload Modal Dialog */}
      <Dialog open={uploadModalOpen} onOpenChange={setUploadModalOpen}>
        <DialogContent className="max-w-2xl p-8 bg-card border border-border/90 rounded-3xl shadow-2xl">
          <DialogTitle className="sr-only">Upload Documents</DialogTitle>
          <UploadPage onClose={() => setUploadModalOpen(false)} isModal={true} />
        </DialogContent>
      </Dialog>
    </div>
  )
}
