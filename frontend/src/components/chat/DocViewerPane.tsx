import React, { useState, useEffect, useRef } from "react"
import {
  X,
  ZoomIn,
  ZoomOut,
  ChevronLeft,
  ChevronRight,
  FileText,
  Network,
  Maximize2,
  Minimize2,
  GripVertical
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
    currentPage,
    setCurrentPage,
    totalPages,
    docPaneWidth,
    setDocPaneWidth
  } = useChatStore()

  const [highlightedId, setHighlightedId] = useState<number | null>(
    activeCitation?.id || 1
  )
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
            className={`pb-2.5 text-xs font-semibold tracking-wider uppercase transition-colors relative cursor-pointer ${
              rightPaneMode === "document"
                ? "text-foreground font-bold"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <span>Page {currentPage} of {totalPages}</span>
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
          {/* Maximize / Preset toggle */}
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
          <div className="px-5 py-2 border-b border-border/40 flex items-center justify-between bg-card/60">
            <div className="flex items-center gap-2 text-xs font-medium text-foreground truncate max-w-[200px] sm:max-w-[260px]">
              <FileText className="h-3.5 w-3.5 text-[#c96442] shrink-0" />
              <span className="truncate">
                {activeCitation?.document_filename || "Candidate_A_Resume_Final.pdf"}
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
          <div className="flex-1 overflow-y-auto p-4 bg-muted/40 flex justify-center">
            <div
              style={{
                transform: `scale(${zoomLevel / 100})`,
                transformOrigin: "top center",
                maxWidth: docPaneWidth >= 650 ? "580px" : "400px"
              }}
              className="w-full bg-[#ffffff] text-[#1c1c1a] rounded-xl shadow-md p-6 space-y-4 border border-[#dad9d4] relative transition-all"
            >
              {/* Candidate Header */}
              <div className="border-b border-[#dad9d4] pb-3 text-left">
                <h1 className="font-headline font-bold text-lg tracking-tight text-[#1c1c1a]">
                  ALEXANDRA CHEN
                </h1>
                <p className="text-[10px] font-bold text-[#89726b] uppercase tracking-wider">
                  Senior Product Designer & ML Specialist
                </p>
                <p className="text-[9px] text-[#56423c] mt-0.5">
                  alexandra.chen@email.com | (123) 456-7890 | San Francisco, CA
                </p>
              </div>

              {/* Professional Summary */}
              <div className="text-left space-y-1">
                <h3 className="text-[10px] font-bold text-[#1c1c1a] tracking-wider uppercase border-b border-[#dad9d4] pb-0.5">
                  Professional Summary
                </h3>
                <p className="text-[9.5px] text-[#56423c] leading-relaxed">
                  Innovative systems and product architect with 4+ years of specialized AI experience in machine learning pipelines, NLP retrieval architectures, and interactive multimodal UX workflows.
                </p>
              </div>

              {/* Experience */}
              <div className="text-left space-y-2">
                <h3 className="text-[10px] font-bold text-[#1c1c1a] tracking-wider uppercase border-b border-[#dad9d4] pb-0.5">
                  Experience
                </h3>

                {/* Citation Bounding Box 1 */}
                <div
                  id="citation-box-1"
                  onClick={() => setHighlightedId(1)}
                  className={`p-2 rounded-lg border-2 transition-all relative cursor-pointer ${
                    highlightedId === 1 || activeCitation?.id === 1
                      ? "border-[#c96442] bg-[#c96442]/10 ring-2 ring-[#c96442]/20"
                      : "border-[#c96442]/40 hover:border-[#c96442]"
                  }`}
                >
                  {/* Pin Tag 1 */}
                  <span className="absolute -top-2.5 -right-2 h-5 w-5 rounded-full bg-[#c96442] text-white text-[10px] font-bold flex items-center justify-center shadow-xs">
                    1
                  </span>

                  <div className="font-bold text-[10.5px] text-[#1c1c1a]">
                    SENIOR PRODUCT DESIGNER | TechFlow Inc. (2019–Present)
                  </div>
                  <ul className="list-disc list-inside text-[9.5px] text-[#56423c] space-y-0.5 mt-1">
                    <li>Led end-to-end ML integration and retrieval recommendation engines.</li>
                    <li>Migrated recommendation engines and deployed production workflows.</li>
                  </ul>
                </div>

                {/* Citation Bounding Box 2 */}
                <div
                  id="citation-box-2"
                  onClick={() => setHighlightedId(2)}
                  className={`p-2 rounded-lg border-2 transition-all relative cursor-pointer mt-3 ${
                    highlightedId === 2 || activeCitation?.id === 2
                      ? "border-[#c96442] bg-[#c96442]/10 ring-2 ring-[#c96442]/20"
                      : "border-[#c96442]/40 hover:border-[#c96442]"
                  }`}
                >
                  {/* Pin Tag 2 */}
                  <span className="absolute -top-2.5 -right-2 h-5 w-5 rounded-full bg-[#c96442] text-white text-[10px] font-bold flex items-center justify-center shadow-xs">
                    2
                  </span>

                  <div className="font-bold text-[10.5px] text-[#1c1c1a]">
                    ML INFRASTRUCTURE & MODEL DEPLOYMENT
                  </div>
                  <ul className="list-disc list-inside text-[9.5px] text-[#56423c] space-y-0.5 mt-1">
                    <li>Successfully deployed three distinct predictive models into production environments.</li>
                    <li>Serving over 10k requests/min with custom transformer architectures.</li>
                    <li>Reduced overall inference latency by 22% via quantized models.</li>
                  </ul>
                </div>
              </div>

              {/* Education & Skills */}
              <div className="text-left space-y-1.5 pt-1">
                <h3 className="text-[10px] font-bold text-[#1c1c1a] tracking-wider uppercase border-b border-[#dad9d4] pb-0.5">
                  Education & Skills
                </h3>
                <p className="text-[9.5px] text-[#56423c]">
                  <strong className="text-[#1c1c1a]">M.S. Human-Computer Interaction & AI</strong> — Stanford University (2018)
                </p>
                <p className="text-[9.5px] text-[#56423c]">
                  <strong className="text-[#1c1c1a]">Skills:</strong> PyTorch, LangGraph, RAG Retrieval, Transformer architectures, UI/UX Design Systems, TypeScript, React.
                </p>
              </div>
            </div>
          </div>

          {/* Bottom Pagination controls */}
          <div className="p-2.5 border-t border-border/60 bg-card flex items-center justify-between text-xs text-muted-foreground">
            <button
              type="button"
              disabled={currentPage <= 1}
              onClick={() => setCurrentPage(Math.max(1, currentPage - 1))}
              className="flex items-center gap-1 hover:text-foreground disabled:opacity-40 cursor-pointer"
            >
              <ChevronLeft className="h-4 w-4" />
              <span>Prev</span>
            </button>
            <span className="text-[11px] font-medium">Page {currentPage} of {totalPages}</span>
            <button
              type="button"
              disabled={currentPage >= totalPages}
              onClick={() => setCurrentPage(Math.min(totalPages, currentPage + 1))}
              className="flex items-center gap-1 hover:text-foreground disabled:opacity-40 cursor-pointer"
            >
              <span>Next</span>
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        </>
      )}
    </div>
  )
}
