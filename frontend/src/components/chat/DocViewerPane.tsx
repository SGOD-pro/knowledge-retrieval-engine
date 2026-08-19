import { useEffect, useRef } from "react"
import {
  X,
  ZoomIn,
  ZoomOut,
  FileText,
  Network,
  Maximize2,
  Minimize2,
  GripVertical,
  BookOpen,
  MapPin,
  Quote
} from "lucide-react"
import { useChatStore } from "../../store/useChatStore"
import { KnowledgeGraphPane } from "./KnowledgeGraphPane"

export function DocViewerPane() {
  const {
    activeCitation,
    rightPaneMode,
    setRightPaneMode,
    setRightPaneOpen,
    zoomLevel,
    setZoomLevel,
    docPaneWidth,
    setDocPaneWidth
  } = useChatStore()

  const isDraggingRef = useRef(false)
  const startXRef = useRef(0)
  const startWidthRef = useRef(docPaneWidth)

  // Drag resize handler
  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isDraggingRef.current) return
      const deltaX = startXRef.current - e.clientX
      const newWidth = Math.min(Math.max(startWidthRef.current + deltaX, 360), 850)
      setDocPaneWidth(newWidth)
    }

    const handleMouseUp = () => {
      if (isDraggingRef.current) {
        isDraggingRef.current = false
        document.body.style.cursor = "default"
        document.body.style.userSelect = "auto"
      }
    }

    window.addEventListener("mousemove", handleMouseMove)
    window.addEventListener("mouseup", handleMouseUp)
    return () => {
      window.removeEventListener("mousemove", handleMouseMove)
      window.removeEventListener("mouseup", handleMouseUp)
    }
  }, [setDocPaneWidth])

  const handleMouseDown = (e: React.MouseEvent) => {
    isDraggingRef.current = true
    startXRef.current = e.clientX
    startWidthRef.current = docPaneWidth
    document.body.style.cursor = "col-resize"
    document.body.style.userSelect = "none"
  }

  const handleZoomIn = () => {
    setZoomLevel((z) => Math.min(z + 15, 160))
  }

  const handleZoomOut = () => {
    setZoomLevel((z) => Math.max(z - 15, 75))
  }

  const isMaximized = docPaneWidth >= 750

  const toggleMaximize = () => {
    if (isMaximized) {
      setDocPaneWidth(480)
    } else {
      setDocPaneWidth(780)
    }
  }

  return (
    <div
      style={{ width: `${docPaneWidth}px` }}
      className="relative shrink-0 bg-card border border-border/80 rounded-3xl flex flex-col h-full overflow-hidden shadow-xs animate-in slide-in-from-right-4 duration-150 group/pane transition-[width] duration-75"
    >
      {/* Left Edge Drag Resize Handle */}
      <div
        onMouseDown={handleMouseDown}
        className="absolute left-0 top-0 bottom-0 w-3 -ml-1.5 cursor-col-resize z-30 flex items-center justify-center hover:bg-primary/20 transition-colors group"
        title="Drag to resize Document Viewer"
      >
        <div className="w-1 h-8 rounded-full bg-border group-hover:bg-primary transition-colors flex items-center justify-center">
          <GripVertical className="h-3 w-3 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity" />
        </div>
      </div>

      {/* Top Header with Tab, Expand, and Close */}
      <div className="border-b border-border/60 px-5 pt-3.5 pb-0 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <button
            type="button"
            onClick={() => setRightPaneMode("document")}
            className={`pb-2.5 text-xs font-semibold tracking-wider uppercase transition-colors relative cursor-pointer flex items-center gap-1.5 ${
              rightPaneMode === "document"
                ? "text-foreground font-bold"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <BookOpen className="h-3.5 w-3.5" />
            <span>Document Reference</span>
            {rightPaneMode === "document" && (
              <span className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#c96442]" />
            )}
          </button>

          <button
            type="button"
            onClick={() => setRightPaneMode("graph")}
            className={`pb-2.5 text-xs font-semibold tracking-wider uppercase transition-colors relative cursor-pointer flex items-center gap-1.5 ${
              rightPaneMode === "graph"
                ? "text-foreground font-bold"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <Network className="h-3.5 w-3.5" />
            <span>Graph View</span>
            {rightPaneMode === "graph" && (
              <span className="absolute bottom-0 left-0 right-0 h-0.5 bg-[#c96442]" />
            )}
          </button>
        </div>

        <div className="flex items-center gap-1 -mt-2">
          {/* Maximize toggle */}
          <button
            type="button"
            onClick={toggleMaximize}
            className="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors cursor-pointer"
            title={isMaximized ? "Restore width (480px)" : "Expand viewer (780px)"}
          >
            {isMaximized ? (
              <Minimize2 className="h-3.5 w-3.5" />
            ) : (
              <Maximize2 className="h-3.5 w-3.5" />
            )}
          </button>

          {/* Close button */}
          <button
            type="button"
            onClick={() => setRightPaneOpen(false)}
            className="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors cursor-pointer"
            title="Close Document Viewer"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      {rightPaneMode === "graph" ? (
        <KnowledgeGraphPane />
      ) : (
        <>
          {/* Subheader: Document Name & Zoom */}
          <div className="px-5 py-2.5 border-b border-border/40 flex items-center justify-between bg-card/60">
            <div className="flex items-center gap-2 text-xs font-medium text-foreground truncate max-w-[200px] sm:max-w-[260px]">
              <FileText className="h-3.5 w-3.5 text-[#c96442] shrink-0" />
              <span className="truncate">
                {activeCitation?.document_filename || "No Citation Selected"}
              </span>
            </div>

            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={handleZoomOut}
                className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-accent transition-colors cursor-pointer"
                title="Zoom Out"
              >
                <ZoomOut className="h-3.5 w-3.5" />
              </button>
              <span className="text-[10px] font-semibold text-muted-foreground px-1">
                {zoomLevel}%
              </span>
              <button
                type="button"
                onClick={handleZoomIn}
                className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-accent transition-colors cursor-pointer"
                title="Zoom In"
              >
                <ZoomIn className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>

          {/* Document Content Canvas */}
          <div className="flex-1 overflow-y-auto p-4 sm:p-6 bg-muted/30 flex justify-center">
            {activeCitation ? (
              <div
                style={{
                  transform: `scale(${zoomLevel / 100})`,
                  transformOrigin: "top center",
                  maxWidth: docPaneWidth >= 650 ? "580px" : "440px"
                }}
                className="w-full bg-card text-foreground rounded-2xl shadow-sm p-6 space-y-4 border border-border transition-all h-fit"
              >
                {/* Citation Header */}
                <div className="border-b border-border/60 pb-3.5 flex items-start justify-between gap-2">
                  <div className="space-y-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="h-5 w-5 rounded-full bg-[#c96442] text-white text-[10px] font-bold flex items-center justify-center shrink-0">
                        {activeCitation.id}
                      </span>
                      <h2 className="font-headline font-bold text-sm text-foreground truncate">
                        {activeCitation.document_filename}
                      </h2>
                    </div>
                    <div className="flex items-center gap-2 text-[11px] text-muted-foreground pl-7">
                      <span className="flex items-center gap-1">
                        <MapPin className="h-3 w-3" />
                        <span>{activeCitation.location_reference || `Page ${activeCitation.page_number || 1}`}</span>
                      </span>
                      {activeCitation.source_format && (
                        <>
                          <span>•</span>
                          <span className="uppercase font-semibold text-[10px]">
                            {activeCitation.source_format}
                          </span>
                        </>
                      )}
                    </div>
                  </div>
                </div>

                {/* Grounded Citation Extract */}
                <div className="space-y-2">
                  <div className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wider text-[#c96442]">
                    <Quote className="h-3.5 w-3.5" />
                    <span>Cited Content</span>
                  </div>

                  <div className="p-4 rounded-xl bg-[#c96442]/5 border-2 border-[#c96442]/30 text-xs sm:text-sm text-foreground leading-relaxed font-sans shadow-xs">
                    {activeCitation.text}
                  </div>
                </div>

                {/* Chunk Reference Metadata */}
                <div className="pt-2 border-t border-border/50 text-[11px] text-muted-foreground space-y-1">
                  {activeCitation.chunk_id && (
                    <div className="flex items-center justify-between">
                      <span className="font-semibold">Chunk ID:</span>
                      <span className="font-mono text-[10px]">{activeCitation.chunk_id}</span>
                    </div>
                  )}
                  {activeCitation.page_number && (
                    <div className="flex items-center justify-between">
                      <span className="font-semibold">Document Page:</span>
                      <span>Page {activeCitation.page_number}</span>
                    </div>
                  )}
                </div>
              </div>
            ) : (
              /* Clean Empty State when no citation is clicked */
              <div className="h-full flex flex-col items-center justify-center text-center p-6 max-w-xs mx-auto space-y-3">
                <div className="h-12 w-12 rounded-2xl bg-muted text-muted-foreground flex items-center justify-center">
                  <BookOpen className="h-6 w-6" />
                </div>
                <div className="space-y-1">
                  <h4 className="font-headline font-bold text-sm text-foreground">
                    No Citation Selected
                  </h4>
                  <p className="text-xs text-muted-foreground leading-relaxed">
                    Click any citation number badge (e.g. [1], [2]) in an AI response to inspect the source passage here.
                  </p>
                </div>
              </div>
            )}
          </div>

          {/* Bottom Navigation */}
          {activeCitation && (
            <div className="p-2.5 border-t border-border/60 bg-card flex items-center justify-between text-xs text-muted-foreground">
              <span className="text-[11px] font-medium text-foreground">
                Citation [{activeCitation.id}]
              </span>
              <span className="text-[11px]">
                {activeCitation.location_reference || `Page ${activeCitation.page_number || 1}`}
              </span>
            </div>
          )}
        </>
      )}
    </div>
  )
}
