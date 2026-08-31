import { useState, useEffect, useMemo } from "react"
import { useParams, useNavigate, Link } from "react-router-dom"
import {
  ArrowLeft,
  Download,
  MessageSquare,
  FileText,
  RefreshCw,
  Layers,
  FileSpreadsheet,
  Zap,
  AlertCircle
} from "lucide-react"
import { useWorkspaceStore } from "../store/useWorkspaceStore"
import { useDocumentStore } from "../store/useDocumentStore"
import { api, API_BASE } from "../lib/api"
import { Button } from "../components/ui/button"
import { PdfViewer } from "../components/viewer/PdfViewer"
import { CsvSpreadsheetViewer } from "../components/viewer/CsvSpreadsheetViewer"
import { ExcelViewer } from "../components/viewer/ExcelViewer"
import { MarkdownViewer } from "../components/viewer/MarkdownViewer"
import { DocxViewer } from "../components/viewer/DocxViewer"
import { PptxViewer } from "../components/viewer/PptxViewer"
import { CodeTextViewer } from "../components/viewer/CodeTextViewer"
import { IndexedChunksViewer } from "../components/viewer/IndexedChunksViewer"

const LARGE_FILE_THRESHOLD_BYTES = 5 * 1024 * 1024 // 5 MB

export function DocumentViewerPage() {
  const { workspaceId, documentId } = useParams<{ workspaceId: string; documentId: string }>()
  const navigate = useNavigate()
  const { activeWorkspace, workspaces, setActiveWorkspace } = useWorkspaceStore()
  const { documents } = useDocumentStore()

  const [activeTab, setActiveTab] = useState<"content" | "chunks">("content")
  const [docMeta, setDocMeta] = useState<any>(null)
  const [fileBlobUrl, setFileBlobUrl] = useState<string | null>(null)
  const [fileText, setFileText] = useState<string>("")
  const [fileArrayBuffer, setFileArrayBuffer] = useState<ArrayBuffer | null>(null)
  const [fileSizeBytes, setFileSizeBytes] = useState<number>(0)
  const [isLoading, setIsLoading] = useState<boolean>(true)
  const [error, setError] = useState<string | null>(null)

  // Sync workspace if needed
  useEffect(() => {
    if (workspaceId && activeWorkspace?.id !== workspaceId) {
      const match = workspaces.find((w) => w.id === workspaceId)
      if (match) {
        setActiveWorkspace(match)
      }
    }
  }, [workspaceId, activeWorkspace, workspaces, setActiveWorkspace])

  const currentWsId = workspaceId || activeWorkspace?.id || ""

  // Fetch document metadata and raw file
  useEffect(() => {
    if (!documentId) return
    let isCancelled = false

    async function loadDocumentData() {
      setIsLoading(true)
      setError(null)

      try {
        // 1. Fetch metadata
        let meta: any = null
        try {
          meta = await api.getDocument(documentId!)
        } catch (mErr) {
          console.warn("Could not fetch document metadata, using local store:", mErr)
          const localMatch = documents.find((d) => d.id === documentId)
          if (localMatch) {
            meta = {
              id: localMatch.id,
              filename: localMatch.filename,
              source_format: localMatch.format,
              chunks: [],
              workspace_id: currentWsId,
              status: localMatch.status,
              size: localMatch.size
            }
          }
        }

        if (isCancelled) return
        setDocMeta(meta)

        // 2. Fetch raw file
        const fileUrl = `${API_BASE}/api/v1/documents/${documentId}/file`
        const token = localStorage.getItem("kre_token")
        const headers: HeadersInit = {}
        if (token) {
          headers["Authorization"] = `Bearer ${token}`
        }

        const res = await fetch(fileUrl, { headers })
        if (!res.ok) {
          throw new Error(`Failed to fetch document file (HTTP ${res.status})`)
        }

        const blob = await res.blob()
        if (isCancelled) return

        const size = blob.size
        setFileSizeBytes(size)

        const objectUrl = URL.createObjectURL(blob)
        setFileBlobUrl(objectUrl)

        const format = (meta?.source_format || meta?.filename?.split(".").pop() || "").toLowerCase()

        // Extract ArrayBuffer or Text based on format
        if (["xlsx", "xls", "docx", "doc"].includes(format)) {
          const ab = await blob.arrayBuffer()
          if (!isCancelled) setFileArrayBuffer(ab)
        } else if (["csv", "tsv", "md", "txt", "json", "py", "ts", "js", "html", "css", "xml", "yaml", "yml"].includes(format)) {
          // If large text, read text
          const text = await blob.text()
          if (!isCancelled) setFileText(text)
        }
      } catch (err: any) {
        console.error("Error loading document:", err)
        if (!isCancelled) {
          setError(err?.message || "Failed to load document")
        }
      } finally {
        if (!isCancelled) {
          setIsLoading(false)
        }
      }
    }

    loadDocumentData()

    return () => {
      isCancelled = true
      if (fileBlobUrl) {
        URL.revokeObjectURL(fileBlobUrl)
      }
    }
  }, [documentId, currentWsId])

  const filename = docMeta?.filename || "document"
  const format = useMemo(() => {
    if (docMeta?.source_format) return docMeta.source_format.toLowerCase()
    const ext = filename.split(".").pop()
    return ext ? ext.toLowerCase() : "unknown"
  }, [docMeta, filename])

  const isLargeFile = fileSizeBytes > LARGE_FILE_THRESHOLD_BYTES

  const formatSizeStr = useMemo(() => {
    if (fileSizeBytes > 0) {
      const mb = fileSizeBytes / (1024 * 1024)
      if (mb >= 1) return `${mb.toFixed(1)} MB`
      return `${(fileSizeBytes / 1024).toFixed(0)} KB`
    }
    return docMeta?.size || "1.2 MB"
  }, [fileSizeBytes, docMeta])

  const handleDownload = () => {
    if (fileBlobUrl) {
      const a = document.createElement("a")
      a.href = fileBlobUrl
      a.download = filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
    }
  }

  const handleAskAI = () => {
    navigate(`/workspaces/${currentWsId}/chat`)
  }

  return (
    <div className="p-6 lg:p-10 max-w-7xl mx-auto space-y-6 animate-in fade-in-50 duration-200">
      {/* Top Breadcrumbs & Back Bar */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => navigate(`/library/${currentWsId}`)}
            className="h-9 px-3 rounded-xl border-border/70 text-xs font-semibold hover:bg-accent cursor-pointer flex items-center gap-1.5"
          >
            <ArrowLeft className="h-4 w-4" />
            <span>Back to Library</span>
          </Button>

          <div className="flex items-center gap-2 text-xs font-semibold text-muted-foreground uppercase tracking-wider">
            <span className="text-[#c96442]">{activeWorkspace?.name || "Workspace"}</span>
            <span>/</span>
            <Link
              to={`/library/${currentWsId}`}
              className="hover:text-foreground transition-colors"
            >
              Library
            </Link>
            <span>/</span>
            <span className="text-foreground normal-case font-bold truncate max-w-[200px]">
              {filename}
            </span>
          </div>
        </div>

        {/* Action Buttons */}
        <div className="flex items-center gap-2.5">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={handleDownload}
            disabled={!fileBlobUrl}
            className="h-9 px-3.5 rounded-xl border-border/70 text-xs font-semibold hover:bg-accent cursor-pointer flex items-center gap-1.5 shadow-xs"
          >
            <Download className="h-4 w-4 text-primary" />
            <span>Download Original</span>
          </Button>

          <Button
            type="button"
            size="sm"
            onClick={handleAskAI}
            className="h-9 px-4 rounded-xl bg-primary text-white hover:bg-primary/90 text-xs font-semibold cursor-pointer flex items-center gap-1.5 shadow-xs"
          >
            <MessageSquare className="h-4 w-4" />
            <span>Ask AI about Document</span>
          </Button>
        </div>
      </div>

      {/* Document Meta Header Card */}
      <div className="rounded-3xl border border-border/80 bg-card p-6 shadow-xs flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div className="flex items-start gap-4">
          <div className="h-12 w-12 rounded-2xl bg-[#fdeae4] dark:bg-[#3d231b] text-[#c96442] dark:text-[#ffb59d] flex items-center justify-center shrink-0 shadow-xs">
            {["csv", "tsv", "xlsx", "xls"].includes(format) ? (
              <FileSpreadsheet className="h-6 w-6" />
            ) : (
              <FileText className="h-6 w-6" />
            )}
          </div>

          <div className="space-y-1 min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2.5">
              <h1 className="font-headline text-lg sm:text-xl md:text-2xl font-bold tracking-tight text-foreground break-all sm:break-words">
                {filename}
              </h1>
              <span className="px-2.5 py-0.5 rounded-full text-[10px] font-bold bg-[#ede9de] dark:bg-[#282a2c] text-foreground/80 font-mono uppercase tracking-wider shrink-0">
                {format}
              </span>
              <span className="px-2.5 py-0.5 rounded-full text-[10px] font-medium bg-muted text-muted-foreground shrink-0">
                {formatSizeStr}
              </span>
            </div>

            <p className="text-xs text-muted-foreground font-sans truncate">
              Indexed in Workspace retrieval pipeline · {docMeta?.chunks?.length || 0} retrieval chunks generated
            </p>
          </div>
        </div>

        {/* View Mode Switcher Tab */}
        <div className="flex items-center bg-muted/40 p-1 rounded-2xl border border-border/60 shrink-0">
          <button
            type="button"
            onClick={() => setActiveTab("content")}
            className={`flex items-center gap-1.5 px-4 py-2 rounded-xl text-xs font-semibold transition-all cursor-pointer ${
              activeTab === "content"
                ? "bg-card text-primary shadow-xs border border-border/70"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <FileText className="h-3.5 w-3.5" />
            <span>Document Content</span>
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("chunks")}
            className={`flex items-center gap-1.5 px-4 py-2 rounded-xl text-xs font-semibold transition-all cursor-pointer ${
              activeTab === "chunks"
                ? "bg-card text-primary shadow-xs border border-border/70"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <Layers className="h-3.5 w-3.5" />
            <span>Indexed Chunks & OKF Vectors</span>
            <span className="ml-1 px-1.5 py-0.2 rounded-full bg-primary/10 text-primary text-[10px]">
              {docMeta?.chunks?.length || 0}
            </span>
          </button>
        </div>
      </div>

      {/* Large File Performance Preview Warning Banner (>5-6 MB) */}
      {isLargeFile && (
        <div className="p-4 rounded-2xl bg-amber-500/10 border border-amber-500/30 flex items-start gap-3 text-xs text-amber-900 dark:text-amber-200">
          <Zap className="h-5 w-5 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
          <div className="space-y-1">
            <p className="font-semibold text-amber-950 dark:text-amber-100">
              Performance Optimization Active (File Size: {formatSizeStr})
            </p>
            <p className="text-amber-800/90 dark:text-amber-300/90 text-[11px] leading-relaxed">
              This file exceeds 5MB. To ensure instantaneous response times and prevent memory latency, a streamlined high-performance preview with virtualized rendering is active. You can download the complete full file anytime using the Download button above.
            </p>
          </div>
        </div>
      )}

      {/* Loading Skeleton */}
      {isLoading ? (
        <div className="h-[550px] rounded-3xl border border-border/80 bg-card p-12 flex flex-col items-center justify-center gap-4">
          <RefreshCw className="h-8 w-8 animate-spin text-[#c96442]" />
          <div className="text-center space-y-1">
            <p className="text-sm font-semibold text-foreground">Loading Document Data</p>
            <p className="text-xs text-muted-foreground">Streaming content and parsing format structure...</p>
          </div>
        </div>
      ) : error ? (
        /* Error State */
        <div className="h-[400px] rounded-3xl border border-border/80 bg-card p-12 flex flex-col items-center justify-center gap-3 text-center">
          <AlertCircle className="h-10 w-10 text-destructive opacity-70 mb-2" />
          <h3 className="font-headline font-bold text-base text-foreground">Failed to render document</h3>
          <p className="text-xs text-muted-foreground max-w-md">{error}</p>
          {fileBlobUrl && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handleDownload}
              className="mt-3 text-xs rounded-xl"
            >
              <Download className="h-3.5 w-3.5 mr-1" />
              <span>Download File Offline</span>
            </Button>
          )}
        </div>
      ) : activeTab === "chunks" ? (
        /* Indexed Chunks Tab */
        <div className="min-h-[550px]">
          <IndexedChunksViewer
            chunks={docMeta?.chunks || []}
            filename={filename}
          />
        </div>
      ) : (
        /* Document Content Dispatcher */
        <div className="min-h-[550px]">
          {format === "pdf" && fileBlobUrl ? (
            <PdfViewer fileUrl={fileBlobUrl} filename={filename} isLargeFile={isLargeFile} />
          ) : (format === "csv" || format === "tsv") ? (
            <CsvSpreadsheetViewer csvText={fileText} filename={filename} isLargeFile={isLargeFile} />
          ) : (format === "xlsx" || format === "xls") && fileArrayBuffer ? (
            <ExcelViewer arrayBuffer={fileArrayBuffer} filename={filename} isLargeFile={isLargeFile} />
          ) : (format === "docx" || format === "doc") && fileArrayBuffer ? (
            <DocxViewer
              arrayBuffer={fileArrayBuffer}
              filename={filename}
              fallbackText={docMeta?.chunks?.map((c: any) => c.text).join("\n\n")}
            />
          ) : (format === "pptx" || format === "ppt") ? (
            <PptxViewer
              content={fileText || docMeta?.chunks?.map((c: any) => c.text).join("\n\n")}
              chunks={docMeta?.chunks}
              filename={filename}
            />
          ) : (format === "md" || format === "markdown") ? (
            <MarkdownViewer
              content={fileText || docMeta?.chunks?.map((c: any) => c.text).join("\n\n")}
              filename={filename}
            />
          ) : (
            <CodeTextViewer
              content={fileText || docMeta?.chunks?.map((c: any) => c.text).join("\n\n")}
              filename={filename}
              format={format}
            />
          )}
        </div>
      )}
    </div>
  )
}
export default DocumentViewerPage
