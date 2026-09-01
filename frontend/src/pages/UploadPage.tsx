import { useState, useRef, useEffect } from "react"
import { useParams, useNavigate } from "react-router-dom"
import {
  UploadCloud,
  Info,
  Loader2,
  ChevronDown,
  FileText,
  CheckCircle2,
  RefreshCw,
  AlertCircle,
  XCircle,
  MessageSquare
} from "lucide-react"
import { useWorkspaceStore } from "../store/useWorkspaceStore"
import { useDocumentStore } from "../store/useDocumentStore"
import type { FileUploadState } from "../store/useDocumentStore"
import { Button } from "../components/ui/button"
import { Progress } from "../components/ui/progress"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger
} from "../components/ui/dropdown-menu"
import { toast } from "sonner"
import type { DocumentItem } from "../types/api"

export function UploadPage({
  onClose,
  isModal = false
}: {
  onClose?: () => void
  isModal?: boolean
}) {
  const { workspaceId } = useParams<{ workspaceId: string }>()
  const { activeWorkspace, workspaces, setActiveWorkspace, fetchWorkspaces } = useWorkspaceStore()
  const { documents, fetchDocuments, uploadFiles, isUploading, fileUploads } = useDocumentStore()
  const navigate = useNavigate()
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // Sync route workspaceId with store
  useEffect(() => {
    if (workspaceId && activeWorkspace?.id !== workspaceId) {
      const match = workspaces.find((w) => w.id === workspaceId)
      if (match) {
        setActiveWorkspace(match)
      }
    }
  }, [workspaceId, activeWorkspace?.id, workspaces, setActiveWorkspace])

  const currentWsId = workspaceId || activeWorkspace?.id || ""

  useEffect(() => {
    if (currentWsId) {
      fetchDocuments(currentWsId)
    }
  }, [currentWsId, fetchDocuments])

  const handleFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return
    const fileArray = Array.from(files)

    toast.info(`Uploading ${fileArray.length} document${fileArray.length > 1 ? "s" : ""}...`)
    const success = await uploadFiles(currentWsId, fileArray)

    if (success) {
      toast.success("All documents uploaded and queued for processing!")
      // Refresh both documents AND workspace list so doc_count updates on workspace card
      await Promise.all([fetchDocuments(currentWsId), fetchWorkspaces()])
      if (onClose) {
        onClose()
      }
    } else {
      const failedCount = fileUploads.filter((f) => f.status === "error").length
      if (failedCount > 0) {
        toast.error(`${failedCount} file(s) failed to upload`)
      }
    }
  }

  const getFormatBadge = (format: string) => {
    const fmt = format.toUpperCase()
    if (fmt === "PDF") {
      return (
        <span className="px-2 py-0.5 rounded text-[9px] font-bold bg-[#fdeae4] dark:bg-[#3d231b] text-[#c96442] dark:text-[#ffb59d] tracking-wider">
          PDF
        </span>
      )
    }
    return (
      <span className="px-2 py-0.5 rounded text-[9px] font-bold bg-[#ede9de] dark:bg-[#242628] text-foreground/80 tracking-wider">
        {fmt}
      </span>
    )
  }

  const getStatusBadge = (status: DocumentItem["status"]) => {
    if (status === "Ready") {
      return (
        <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-[#e2f3ee] dark:bg-[#1a3832] text-[#006768] dark:text-[#6cd7d8]">
          <CheckCircle2 className="h-3 w-3" />
          <span>Ready</span>
        </span>
      )
    }
    if (status === "Processing") {
      return (
        <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-[#ede9de] dark:bg-[#242628] text-foreground/80">
          <RefreshCw className="h-3 w-3 animate-spin text-[#c96442]" />
          <span>Processing</span>
        </span>
      )
    }
    return (
      <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-[#ffdad6] dark:bg-[#4a1818] text-[#ba1a1a] dark:text-[#ffb4ab]">
        <AlertCircle className="h-3 w-3" />
        <span>Failed</span>
      </span>
    )
  }

  /** Render per-file upload status icon */
  const getFileStatusIcon = (fu: FileUploadState) => {
    if (fu.status === "done") {
      return <CheckCircle2 className="h-4 w-4 text-[#006768] dark:text-[#6cd7d8] shrink-0" />
    }
    if (fu.status === "error") {
      return <XCircle className="h-4 w-4 text-[#ba1a1a] dark:text-[#ffb4ab] shrink-0" />
    }
    if (fu.status === "uploading") {
      return <Loader2 className="h-4 w-4 text-[#c96442] animate-spin shrink-0" />
    }
    return <RefreshCw className="h-4 w-4 text-muted-foreground/50 shrink-0" />
  }

  return (
    <div
      className={
        isModal
          ? "w-full space-y-5 animate-in fade-in-50 duration-150 max-h-[82vh] overflow-y-auto pr-1"
          : "p-8 lg:p-12 max-w-4xl mx-auto space-y-8 animate-in fade-in-50 duration-200"
      }
    >
      {/* Breadcrumb / Workspace Switcher only if not in modal */}
      {!isModal && (
        <div className="flex items-center gap-2 text-xs font-semibold text-muted-foreground">
          <span className="font-headline font-bold text-sm tracking-wider text-[#c96442]">
            KRE
          </span>
          <span>|</span>
          <DropdownMenu>
            <DropdownMenuTrigger className="flex items-center gap-1 text-foreground hover:text-primary transition-colors cursor-pointer outline-none">
              <span>{activeWorkspace?.name || "Select Workspace"}</span>
              <ChevronDown className="h-3.5 w-3.5 opacity-60" />
            </DropdownMenuTrigger>
            <DropdownMenuContent className="bg-popover border-border">
              {workspaces.map((ws) => (
                <DropdownMenuItem
                  key={ws.id}
                  onClick={() => setActiveWorkspace(ws)}
                  className="cursor-pointer"
                >
                  {ws.name}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      )}

      {/* Header */}
      <div className="text-center space-y-1.5 max-w-2xl mx-auto">
        <h1 className="font-headline text-2xl sm:text-3xl font-bold tracking-tight text-foreground">
          Add Knowledge to Workspace
        </h1>
        <p className="text-xs sm:text-sm text-muted-foreground font-sans leading-relaxed">
          Upload documents to build your retrieval corpus. KRE AI will automatically extract and index content.
        </p>
      </div>

      {/* Drag and Drop Zone */}
      <div
        onDragOver={(e) => {
          e.preventDefault()
          setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragOver(false)
          handleFiles(e.dataTransfer.files)
        }}
        className={`rounded-3xl border-2 border-dashed transition-all p-6 sm:p-10 flex flex-col items-center justify-center text-center bg-[#fffbf9]/60 dark:bg-[#1a1c1e] ${
          dragOver
            ? "border-[#c96442] bg-[#c96442]/5 scale-[1.01]"
            : "border-[#e5bfb3] dark:border-[#4d2a20] hover:border-[#c96442]"
        }`}
      >
        <input
          type="file"
          ref={fileInputRef}
          multiple
          accept=".pdf,.docx,.csv,.pptx"
          className="hidden"
          onChange={(e) => handleFiles(e.target.files)}
        />

        {/* Upload Icon */}
        <div className="h-12 w-12 rounded-full bg-[#fdeae4] dark:bg-[#282a2c] text-[#c96442] dark:text-[#ffb59d] flex items-center justify-center mb-3.5 shadow-xs">
          <UploadCloud className="h-6 w-6 stroke-[2]" />
        </div>

        {/* Title */}
        <h3 className="font-headline text-lg sm:text-xl font-bold text-foreground mb-0.5">
          Drag and drop files here
        </h3>
        <p className="text-xs text-muted-foreground mb-4 font-sans">
          or click to browse your computer
        </p>

        {/* Select Button */}
        <Button
          type="button"
          disabled={isUploading}
          onClick={() => fileInputRef.current?.click()}
          className="h-9 px-6 bg-[#c96442] hover:bg-[#b05730] text-white font-medium rounded-xl text-xs shadow-xs mb-4 cursor-pointer"
        >
          {isUploading ? (
            <div className="flex items-center gap-2">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span>Uploading...</span>
            </div>
          ) : (
            "Select Files"
          )}
        </Button>

        {/* Format Chips */}
        <div className="flex items-center gap-2">
          {["PDF", "DOCX", "CSV", "PPTX"].map((fmt) => (
            <span
              key={fmt}
              className="px-2.5 py-1 rounded-md text-[10px] font-bold bg-[#ede9de] dark:bg-[#242628] text-foreground/80 tracking-wider"
            >
              {fmt}
            </span>
          ))}
        </div>
      </div>

      {/* Per-File Upload Progress — shown while uploading */}
      {isUploading && fileUploads.length > 0 && (
        <div className="rounded-2xl border border-border/80 bg-card shadow-xs overflow-hidden max-w-2xl mx-auto divide-y divide-border/50">
          <div className="px-4 py-3 flex items-center justify-between">
            <h3 className="font-headline font-bold text-xs text-foreground">
              Uploading {fileUploads.length} file{fileUploads.length > 1 ? "s" : ""}
            </h3>
            <span className="text-[11px] text-muted-foreground font-sans">
              {fileUploads.filter((f) => f.status === "done").length}/{fileUploads.length} complete
            </span>
          </div>
          {fileUploads.map((fu, idx) => (
            <div key={`${fu.filename}-${idx}`} className="px-4 py-3 space-y-1.5">
              <div className="flex items-center gap-2.5">
                {getFileStatusIcon(fu)}
                <span className="text-xs font-medium text-foreground truncate flex-1 min-w-0">
                  {fu.filename}
                </span>
                <span
                  className={`text-[11px] font-semibold shrink-0 ${
                    fu.status === "done"
                      ? "text-[#006768] dark:text-[#6cd7d8]"
                      : fu.status === "error"
                      ? "text-[#ba1a1a] dark:text-[#ffb4ab]"
                      : "text-muted-foreground"
                  }`}
                >
                  {fu.status === "done"
                    ? "Done"
                    : fu.status === "error"
                    ? fu.error || "Failed"
                    : fu.status === "uploading"
                    ? `${fu.progress}%`
                    : "Pending"}
                </span>
              </div>
              {(fu.status === "uploading" || fu.status === "pending") && (
                <Progress
                  value={fu.progress}
                  className="h-1"
                />
              )}
              {fu.status === "done" && (
                <Progress value={100} className="h-1 [&>div]:bg-[#006768]" />
              )}
              {fu.status === "error" && (
                <Progress value={100} className="h-1 [&>div]:bg-[#ba1a1a]" />
              )}
            </div>
          ))}
        </div>
      )}

      {/* Info Callout */}
      <div className="rounded-2xl border border-[#f7e4df] dark:border-[#2d2f31] bg-[#fff1ed] dark:bg-[#181a1c] p-4 flex items-start gap-3.5 shadow-xs max-w-2xl mx-auto">
        <div className="p-1 rounded-full text-[#006768] dark:text-[#6cd7d8] shrink-0">
          <Info className="h-4 w-4" />
        </div>
        <div className="space-y-0.5">
          <h4 className="text-xs font-bold text-foreground">
            KRE AI Processing
          </h4>
          <p className="text-[11px] text-muted-foreground leading-relaxed">
            Uploaded files are securely processed and vectorized for high-speed semantic retrieval. Complex tables and images within documents may require additional processing time.
          </p>
        </div>
      </div>

      {/* Uploaded Documents List with Live Status */}
      {documents.length > 0 && (
        <div className="space-y-3 pt-2 max-w-2xl mx-auto">
          <div className="flex items-center justify-between gap-3">
            <h3 className="font-headline font-bold text-sm text-foreground flex items-center gap-2">
              <span>Workspace Documents</span>
              <span className="px-2 py-0.5 rounded-full text-[10px] font-bold bg-[#ede9de] dark:bg-[#242628] text-muted-foreground">
                {documents.length}
              </span>
            </h3>
            <div className="flex items-center gap-3">
              <span className="hidden sm:inline text-[11px] text-muted-foreground font-sans">
                Indexed for analytical retrieval
              </span>
              {currentWsId && (
                <Button
                  type="button"
                  size="sm"
                  onClick={() => navigate(`/workspaces/${currentWsId}/chat`)}
                  className="h-8 px-3.5 bg-[#c96442] hover:bg-[#b05730] text-white font-medium rounded-xl text-xs shadow-xs flex items-center gap-1.5 cursor-pointer transition-colors"
                >
                  <MessageSquare className="h-3.5 w-3.5" />
                  <span>Go to Chat</span>
                </Button>
              )}
            </div>
          </div>

          <div className="rounded-2xl border border-border/80 bg-card overflow-hidden divide-y divide-border/50 shadow-xs">
            {documents.slice(0, 6).map((doc) => (
              <div
                key={doc.id}
                className="p-3.5 flex items-center justify-between gap-3 hover:bg-accent/40 transition-colors"
              >
                <div className="flex items-center gap-3 min-w-0">
                  <FileText className="h-4 w-4 text-[#c96442] shrink-0" />
                  <div className="min-w-0">
                    <div className="text-xs font-semibold text-foreground truncate">
                      {doc.filename}
                    </div>
                    <div className="text-[10px] text-muted-foreground flex items-center gap-2 mt-0.5">
                      <span>{doc.upload_date}</span>
                      <span>•</span>
                      <span>{doc.chunk_count > 0 ? `${doc.chunk_count} chunks` : "Indexing"}</span>
                    </div>
                  </div>
                </div>

                <div className="flex items-center gap-3 shrink-0">
                  {getFormatBadge(doc.format)}
                  {getStatusBadge(doc.status)}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Chat CTA — shown when there are documents ready to query */}
      {documents.length > 0 && currentWsId && (
        <div className="max-w-2xl mx-auto pt-1">
          <button
            type="button"
            id="go-to-chat-btn"
            onClick={() => navigate(`/workspaces/${currentWsId}/chat`)}
            className="w-full flex items-center justify-center gap-2.5 h-11 rounded-2xl bg-gradient-to-r from-[#c96442] to-[#e07a55] hover:from-[#b05730] hover:to-[#c96442] text-white font-semibold text-sm shadow-md hover:shadow-lg transition-all duration-200 cursor-pointer group"
          >
            <MessageSquare className="h-4 w-4 group-hover:scale-110 transition-transform" />
            <span>Start Chatting with Your Documents</span>
          </button>
        </div>
      )}
    </div>
  )
}
