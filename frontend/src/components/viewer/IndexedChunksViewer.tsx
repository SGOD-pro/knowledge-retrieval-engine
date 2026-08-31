import { useState, useMemo } from "react"
import { Search, Database, Layers, Copy, Check, Sparkles, Hash, BookOpen } from "lucide-react"
import { Input } from "../ui/input"
import { Button } from "../ui/button"

export interface ChunkItem {
  id: string
  document_id?: string
  source_format?: string
  text: string
  element_type?: string
  page_number?: number | null
  section_path?: string[]
  structural_weight?: number
  embedding_fast?: number[] | null
  embedding_full?: number[] | null
  location_reference?: string | null
}

interface IndexedChunksViewerProps {
  chunks: ChunkItem[]
  filename?: string
}

export function IndexedChunksViewer({ chunks }: IndexedChunksViewerProps) {
  const [searchTerm, setSearchTerm] = useState("")
  const [copiedId, setCopiedId] = useState<string | null>(null)

  const filteredChunks = useMemo(() => {
    if (!searchTerm.trim()) return chunks
    const q = searchTerm.toLowerCase()
    return chunks.filter(
      (c) =>
        c.id.toLowerCase().includes(q) ||
        c.text.toLowerCase().includes(q) ||
        c.element_type?.toLowerCase().includes(q) ||
        c.section_path?.some((p) => p.toLowerCase().includes(q))
    )
  }, [chunks, searchTerm])

  const copyChunkText = (id: string, text: string) => {
    navigator.clipboard.writeText(text)
    setCopiedId(id)
    setTimeout(() => setCopiedId(null), 2000)
  }

  const getElementTypeBadge = (type?: string) => {
    const t = (type || "paragraph").toLowerCase()
    if (t === "heading" || t === "title") {
      return (
        <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-[#fdeae4] dark:bg-[#3d231b] text-[#c96442] dark:text-[#ffb59d]">
          HEADING
        </span>
      )
    }
    if (t === "table") {
      return (
        <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-[#e2f3ee] dark:bg-[#1a3832] text-[#006768] dark:text-[#6cd7d8]">
          TABLE
        </span>
      )
    }
    if (t === "list") {
      return (
        <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-[#ede9de] dark:bg-[#282a2c] text-foreground/80">
          LIST
        </span>
      )
    }
    return (
      <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-muted text-muted-foreground">
        TEXT
      </span>
    )
  }

  return (
    <div className="flex flex-col h-full bg-card border border-border/80 rounded-2xl overflow-hidden shadow-xs">
      {/* Control Bar */}
      <div className="p-3 border-b border-border/60 bg-muted/20 flex flex-wrap items-center justify-between gap-3 text-xs">
        <div className="flex items-center gap-2 flex-1 min-w-[200px] max-w-md">
          <div className="relative w-full">
            <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
            <Input
              type="text"
              placeholder={`Search in ${chunks.length} indexed chunks...`}
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="pl-8 h-8 text-xs bg-background rounded-lg border-border/60"
            />
          </div>
        </div>

        <div className="flex items-center gap-2">
          <span className="px-2.5 py-1 rounded-md bg-[#ede9de] dark:bg-[#282a2c] text-foreground/80 font-medium text-[11px] flex items-center gap-1.5">
            <Database className="h-3.5 w-3.5 text-[#c96442]" />
            <span>{chunks.length} Chunks Indexed in OKF</span>
          </span>
        </div>
      </div>

      {/* Chunks List */}
      <div className="flex-1 p-4 md:p-6 overflow-y-auto space-y-4 bg-background/40">
        {filteredChunks.length === 0 ? (
          <div className="h-64 flex flex-col items-center justify-center text-muted-foreground text-xs">
            <Layers className="h-8 w-8 mb-2 opacity-30" />
            <p>No indexed chunks found</p>
          </div>
        ) : (
          filteredChunks.map((chunk, idx) => (
            <div
              key={chunk.id || idx}
              className="rounded-2xl border border-border/70 bg-card p-4 md:p-5 shadow-xs hover:border-primary/50 transition-colors space-y-3"
            >
              {/* Chunk Header / Meta */}
              <div className="flex flex-wrap items-center justify-between gap-2 pb-2.5 border-b border-border/40 text-xs">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-[11px] font-bold text-primary flex items-center gap-1">
                    <Hash className="h-3 w-3" />
                    <span>Chunk #{idx + 1}</span>
                  </span>
                  <span className="text-muted-foreground/40">·</span>
                  <span className="font-mono text-[10px] text-muted-foreground truncate max-w-[140px]" title={chunk.id}>
                    {chunk.id.slice(0, 12)}...
                  </span>
                  {getElementTypeBadge(chunk.element_type)}
                </div>

                <div className="flex items-center gap-2">
                  {chunk.page_number && (
                    <span className="px-2 py-0.5 rounded bg-muted text-[10px] font-medium text-foreground">
                      Page {chunk.page_number}
                    </span>
                  )}
                  {chunk.location_reference && (
                    <span className="px-2 py-0.5 rounded bg-muted text-[10px] font-medium text-foreground">
                      {chunk.location_reference}
                    </span>
                  )}
                  <span className="px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 text-[10px] font-semibold flex items-center gap-1">
                    <Sparkles className="h-3 w-3" />
                    <span>384/1024-dim Vector</span>
                  </span>

                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => copyChunkText(chunk.id, chunk.text)}
                    className="h-7 px-2 text-xs text-muted-foreground hover:text-foreground cursor-pointer"
                  >
                    {copiedId === chunk.id ? (
                      <Check className="h-3.5 w-3.5 text-emerald-500" />
                    ) : (
                      <Copy className="h-3.5 w-3.5" />
                    )}
                  </Button>
                </div>
              </div>

              {/* Section Path if available */}
              {chunk.section_path && chunk.section_path.length > 0 && (
                <div className="flex items-center gap-1 text-[11px] text-muted-foreground">
                  <BookOpen className="h-3 w-3 text-primary shrink-0" />
                  <span className="font-medium text-foreground/80">
                    {chunk.section_path.join(" › ")}
                  </span>
                </div>
              )}

              {/* Text content */}
              <div className="bg-muted/30 rounded-xl p-3.5 font-sans text-xs text-foreground/90 leading-relaxed border border-border/40 whitespace-pre-wrap">
                {chunk.text}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
