import { useState, useMemo } from "react"
import { Eye, Code, Search, Copy, Check } from "lucide-react"
import { Input } from "../ui/input"
import { Button } from "../ui/button"

interface MarkdownViewerProps {
  content: string
  filename?: string
}

function parseMarkdownToBlocks(md: string) {
  const lines = md.split("\n")
  const blocks: Array<{ type: string; content: any }> = []

  let inCodeBlock = false
  let codeLang = ""
  let codeBuffer: string[] = []

  let tableBuffer: string[] = []

  const flushTable = () => {
    if (tableBuffer.length >= 2) {
      const headerRow = tableBuffer[0]
        .split("|")
        .map((c) => c.trim())
        .filter((c, idx, arr) => (idx !== 0 && idx !== arr.length - 1) || c !== "")
      const dataRows = tableBuffer.slice(2).map((r) =>
        r
          .split("|")
          .map((c) => c.trim())
          .filter((c, idx, arr) => (idx !== 0 && idx !== arr.length - 1) || c !== "")
      )
      blocks.push({
        type: "table",
        content: { headers: headerRow, rows: dataRows }
      })
    }
    tableBuffer = []
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]

    if (line.startsWith("```")) {
      if (inCodeBlock) {
        blocks.push({
          type: "code",
          content: { lang: codeLang, code: codeBuffer.join("\n") }
        })
        codeBuffer = []
        inCodeBlock = false
      } else {
        if (tableBuffer.length > 0) flushTable()
        inCodeBlock = true
        codeLang = line.replace("```", "").trim() || "plaintext"
      }
      continue
    }

    if (inCodeBlock) {
      codeBuffer.push(line)
      continue
    }

    // Table row detection
    if (line.trim().startsWith("|") && line.trim().endsWith("|")) {
      tableBuffer.push(line.trim())
      continue
    } else if (tableBuffer.length > 0) {
      flushTable()
    }

    // Headings
    if (line.startsWith("### ")) {
      blocks.push({ type: "h3", content: line.replace("### ", "").trim() })
    } else if (line.startsWith("## ")) {
      blocks.push({ type: "h2", content: line.replace("## ", "").trim() })
    } else if (line.startsWith("# ")) {
      blocks.push({ type: "h1", content: line.replace("# ", "").trim() })
    } else if (line.startsWith("> ")) {
      blocks.push({ type: "blockquote", content: line.replace(/^>\s?/, "").trim() })
    } else if (line.startsWith("- [ ] ") || line.startsWith("- [x] ")) {
      const checked = line.startsWith("- [x] ")
      blocks.push({
        type: "task",
        content: { checked, text: line.replace(/- \[[ x]\] /, "").trim() }
      })
    } else if (line.startsWith("- ") || line.startsWith("* ")) {
      blocks.push({ type: "bullet", content: line.replace(/^[-*]\s+/, "").trim() })
    } else if (/^\d+\.\s+/.test(line)) {
      blocks.push({ type: "numbered", content: line.replace(/^\d+\.\s+/, "").trim() })
    } else if (line.trim() === "---" || line.trim() === "***") {
      blocks.push({ type: "hr", content: null })
    } else if (line.trim() !== "") {
      blocks.push({ type: "p", content: line.trim() })
    }
  }

  if (inCodeBlock && codeBuffer.length > 0) {
    blocks.push({
      type: "code",
      content: { lang: codeLang, code: codeBuffer.join("\n") }
    })
  }
  if (tableBuffer.length > 0) flushTable()

  return blocks
}

export function MarkdownViewer({ content }: MarkdownViewerProps) {
  const [viewMode, setViewMode] = useState<"rendered" | "raw">("rendered")
  const [searchTerm, setSearchTerm] = useState("")
  const [copied, setCopied] = useState(false)

  const blocks = useMemo(() => parseMarkdownToBlocks(content), [content])

  const copyRaw = () => {
    navigator.clipboard.writeText(content)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const renderInline = (text: string) => {
    if (!text) return null
    // Simple inline parser for bold **text**, code `code`, italic *text*
    const parts = text.split(/(\*\*.*?\*\*|`.*?`|\*.*?\*)/g)
    return parts.map((part, idx) => {
      if (part.startsWith("**") && part.endsWith("**")) {
        return (
          <strong key={idx} className="font-bold text-foreground">
            {part.slice(2, -2)}
          </strong>
        )
      }
      if (part.startsWith("`") && part.endsWith("`")) {
        return (
          <code
            key={idx}
            className="px-1.5 py-0.5 rounded bg-muted font-mono text-[11px] text-primary"
          >
            {part.slice(1, -1)}
          </code>
        )
      }
      if (part.startsWith("*") && part.endsWith("*")) {
        return <em key={idx}>{part.slice(1, -1)}</em>
      }
      return part
    })
  }

  return (
    <div className="flex flex-col h-full bg-card border border-border/80 rounded-2xl overflow-hidden shadow-xs">
      {/* Controls Bar */}
      <div className="p-3 border-b border-border/60 bg-muted/20 flex flex-wrap items-center justify-between gap-3 text-xs">
        <div className="flex items-center gap-2 flex-1 min-w-[200px] max-w-md">
          <div className="relative w-full">
            <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
            <Input
              type="text"
              placeholder="Search in markdown..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="pl-8 h-8 text-xs bg-background rounded-lg border-border/60"
            />
          </div>
        </div>

        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={copyRaw}
            className="h-8 px-2.5 rounded-lg border-border/60 text-xs cursor-pointer"
          >
            {copied ? (
              <>
                <Check className="h-3.5 w-3.5 mr-1 text-emerald-500" />
                <span>Copied</span>
              </>
            ) : (
              <>
                <Copy className="h-3.5 w-3.5 mr-1" />
                <span>Copy Source</span>
              </>
            )}
          </Button>

          <div className="flex items-center bg-background rounded-lg p-0.5 border border-border/60">
            <button
              type="button"
              onClick={() => setViewMode("rendered")}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium transition-colors cursor-pointer ${
                viewMode === "rendered"
                  ? "bg-primary text-primary-foreground shadow-xs"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              <Eye className="h-3 w-3" />
              <span>Preview</span>
            </button>
            <button
              type="button"
              onClick={() => setViewMode("raw")}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium transition-colors cursor-pointer ${
                viewMode === "raw"
                  ? "bg-primary text-primary-foreground shadow-xs"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              <Code className="h-3 w-3" />
              <span>Raw</span>
            </button>
          </div>
        </div>
      </div>

      {/* Content Area */}
      {viewMode === "rendered" ? (
        <div className="flex-1 p-6 md:p-10 overflow-y-auto max-w-4xl mx-auto w-full space-y-4 text-foreground font-sans leading-relaxed text-sm">
          {blocks.map((block, idx) => {
            switch (block.type) {
              case "h1":
                return (
                  <h1
                    key={idx}
                    className="font-headline text-2xl md:text-3xl font-bold tracking-tight text-primary pt-4 pb-2 border-b border-border/60"
                  >
                    {renderInline(block.content)}
                  </h1>
                )
              case "h2":
                return (
                  <h2
                    key={idx}
                    className="font-headline text-xl md:text-2xl font-bold text-foreground pt-3 pb-1 border-b border-border/40"
                  >
                    {renderInline(block.content)}
                  </h2>
                )
              case "h3":
                return (
                  <h3 key={idx} className="font-headline text-lg font-bold text-foreground/90 pt-2">
                    {renderInline(block.content)}
                  </h3>
                )
              case "blockquote":
                return (
                  <blockquote
                    key={idx}
                    className="pl-4 border-l-4 border-primary/60 bg-muted/30 py-2 pr-3 rounded-r-lg italic text-muted-foreground text-xs my-2"
                  >
                    {renderInline(block.content)}
                  </blockquote>
                )
              case "bullet":
                return (
                  <li key={idx} className="ml-5 list-disc text-sm text-foreground/90">
                    {renderInline(block.content)}
                  </li>
                )
              case "numbered":
                return (
                  <li key={idx} className="ml-5 list-decimal text-sm text-foreground/90">
                    {renderInline(block.content)}
                  </li>
                )
              case "task":
                return (
                  <div key={idx} className="flex items-center gap-2 text-sm text-foreground/90">
                    <input
                      type="checkbox"
                      checked={block.content.checked}
                      readOnly
                      className="rounded accent-primary"
                    />
                    <span className={block.content.checked ? "line-through opacity-60" : ""}>
                      {renderInline(block.content.text)}
                    </span>
                  </div>
                )
              case "table":
                return (
                  <div key={idx} className="rounded-xl border border-border/80 overflow-x-auto my-4">
                    <table className="w-full text-left text-xs border-collapse">
                      <thead>
                        <tr className="bg-muted/40 border-b border-border/70 font-semibold text-muted-foreground">
                          {block.content.headers.map((h: string, hIdx: number) => (
                            <th key={hIdx} className="p-3 border-r border-border/30 last:border-r-0">
                              {h}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border/30">
                        {block.content.rows.map((r: string[], rIdx: number) => (
                          <tr key={rIdx} className="hover:bg-accent/30">
                            {r.map((c: string, cIdx: number) => (
                              <td
                                key={cIdx}
                                className="p-3 border-r border-border/20 last:border-r-0 font-mono"
                              >
                                {c}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )
              case "code":
                return (
                  <div key={idx} className="rounded-xl bg-[#1e1e1e] text-[#d4d4d4] p-4 font-mono text-xs my-3 overflow-x-auto border border-neutral-800">
                    <div className="flex items-center justify-between pb-2 mb-2 border-b border-neutral-800 text-[10px] text-neutral-400">
                      <span>{block.content.lang}</span>
                    </div>
                    <pre>{block.content.code}</pre>
                  </div>
                )
              case "hr":
                return <hr key={idx} className="my-6 border-border/60" />
              case "p":
              default:
                return (
                  <p key={idx} className="text-foreground/90 text-sm leading-relaxed">
                    {renderInline(block.content)}
                  </p>
                )
            }
          })}
        </div>
      ) : (
        <div className="flex-1 p-6 overflow-auto font-mono text-xs bg-[#1e1e1e] text-[#d4d4d4]">
          <pre className="whitespace-pre-wrap leading-relaxed">{content}</pre>
        </div>
      )}
    </div>
  )
}
