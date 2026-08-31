import { useState } from "react"
import { Document, Page, pdfjs } from "react-pdf"
import {
  ChevronLeft,
  ChevronRight,
  ZoomIn,
  ZoomOut,
  Maximize2,
  FileText,
  RefreshCw,
  ExternalLink
} from "lucide-react"
import { Button } from "../ui/button"

// Configure pdfjs worker to reliable CDN
pdfjs.GlobalWorkerOptions.workerSrc = `https://unpkg.com/pdfjs-dist@${pdfjs.version}/build/pdf.worker.min.mjs`

interface PdfViewerProps {
  fileUrl: string
  filename?: string
  isLargeFile?: boolean
}

export function PdfViewer({ fileUrl, filename = "document.pdf" }: PdfViewerProps) {
  const [numPages, setNumPages] = useState<number>(0)
  const [pageNumber, setPageNumber] = useState<number>(1)
  const [scale, setScale] = useState<number>(1.2)
  const [pageInput, setPageInput] = useState<string>("1")
  const [useNativeEmbed, setUseNativeEmbed] = useState<boolean>(false)

  function onDocumentLoadSuccess({ numPages }: { numPages: number }) {
    setNumPages(numPages)
    setPageNumber(1)
    setPageInput("1")
  }

  function onDocumentLoadError(error: Error) {
    console.warn("React-PDF error, falling back to native embed:", error)
    setUseNativeEmbed(true)
  }

  const handlePageChange = (newPage: number) => {
    const valid = Math.max(1, Math.min(newPage, numPages))
    setPageNumber(valid)
    setPageInput(String(valid))
  }

  const handlePageInputSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    const parsed = parseInt(pageInput, 10)
    if (!isNaN(parsed)) {
      handlePageChange(parsed)
    } else {
      setPageInput(String(pageNumber))
    }
  }

  const zoomIn = () => setScale((s) => Math.min(s + 0.2, 2.5))
  const zoomOut = () => setScale((s) => Math.max(s - 0.2, 0.6))
  const resetZoom = () => setScale(1.2)

  if (useNativeEmbed) {
    return (
      <div className="flex flex-col h-full bg-card border border-border/80 rounded-2xl overflow-hidden shadow-xs">
        <div className="p-3 border-b border-border/60 bg-muted/20 flex items-center justify-between text-xs">
          <span className="text-muted-foreground flex items-center gap-2">
            <FileText className="h-4 w-4 text-primary" />
            <span>PDF Viewer Mode</span>
          </span>
          <a
            href={fileUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-primary/10 text-primary hover:bg-primary/20 transition-colors font-medium text-xs"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            <span>Open in Native Window</span>
          </a>
        </div>
        <div className="flex-1 min-h-[600px] w-full bg-neutral-900">
          <object
            data={fileUrl}
            type="application/pdf"
            className="w-full h-full min-h-[600px]"
          >
            <iframe
              src={fileUrl}
              title={filename}
              className="w-full h-full min-h-[600px] border-0"
            />
          </object>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full bg-card border border-border/80 rounded-2xl overflow-hidden shadow-xs">
      {/* PDF Controls Header */}
      <div className="p-3 border-b border-border/60 bg-muted/20 flex flex-wrap items-center justify-between gap-3 text-xs">
        {/* Page navigation */}
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={pageNumber <= 1}
            onClick={() => handlePageChange(pageNumber - 1)}
            className="h-8 px-2 rounded-lg border-border/60 cursor-pointer"
          >
            <ChevronLeft className="h-4 w-4" />
          </Button>

          <form onSubmit={handlePageInputSubmit} className="flex items-center gap-1.5">
            <input
              type="text"
              value={pageInput}
              onChange={(e) => setPageInput(e.target.value)}
              onBlur={() => setPageInput(String(pageNumber))}
              className="w-12 h-8 text-center text-xs font-semibold bg-background border border-border/60 rounded-lg outline-none focus:ring-1 focus:ring-primary"
            />
            <span className="text-muted-foreground">/ {numPages || "--"}</span>
          </form>

          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={pageNumber >= numPages}
            onClick={() => handlePageChange(pageNumber + 1)}
            className="h-8 px-2 rounded-lg border-border/60 cursor-pointer"
          >
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>

        {/* Zoom Controls */}
        <div className="flex items-center gap-1.5">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={zoomOut}
            className="h-8 px-2.5 rounded-lg border-border/60 cursor-pointer"
            title="Zoom Out"
          >
            <ZoomOut className="h-3.5 w-3.5" />
          </Button>
          <button
            type="button"
            onClick={resetZoom}
            className="px-2 py-1 text-[11px] font-mono text-muted-foreground hover:text-foreground cursor-pointer"
          >
            {Math.round(scale * 100)}%
          </button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={zoomIn}
            className="h-8 px-2.5 rounded-lg border-border/60 cursor-pointer"
            title="Zoom In"
          >
            <ZoomIn className="h-3.5 w-3.5" />
          </Button>

          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => setUseNativeEmbed(true)}
            className="h-8 px-2 text-muted-foreground hover:text-foreground text-xs ml-2 cursor-pointer"
            title="Switch to browser native PDF frame"
          >
            <Maximize2 className="h-3.5 w-3.5 mr-1" />
            <span>Native Mode</span>
          </Button>
        </div>
      </div>

      {/* PDF Canvas Viewport */}
      <div className="flex-1 overflow-auto bg-[#525659] dark:bg-[#1a1b1c] flex justify-center p-6 min-h-[500px]">
        <Document
          file={fileUrl}
          onLoadSuccess={onDocumentLoadSuccess}
          onLoadError={onDocumentLoadError}
          loading={
            <div className="flex flex-col items-center justify-center p-12 text-white/80 gap-3">
              <RefreshCw className="h-6 w-6 animate-spin" />
              <p className="text-xs">Loading PDF document...</p>
            </div>
          }
          error={
            <div className="flex flex-col items-center justify-center p-12 text-white/80 gap-3">
              <p className="text-xs">Failed to load PDF in canvas.</p>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setUseNativeEmbed(true)}
                className="text-xs"
              >
                Open Native PDF Frame
              </Button>
            </div>
          }
        >
          <div className="shadow-2xl rounded-sm overflow-hidden bg-white">
            <Page
              pageNumber={pageNumber}
              scale={scale}
              renderTextLayer={false}
              renderAnnotationLayer={false}
            />
          </div>
        </Document>
      </div>
    </div>
  )
}
