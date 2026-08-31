import { useState, useMemo } from "react"
import { Search, Copy, Check, WrapText } from "lucide-react"
import { Input } from "../ui/input"
import { Button } from "../ui/button"

interface CodeTextViewerProps {
  content: string
  filename?: string
  format?: string
}

export function CodeTextViewer({ content, filename = "document.txt", format = "txt" }: CodeTextViewerProps) {
  const [searchTerm, setSearchTerm] = useState("")
  const [lineWrap, setLineWrap] = useState(true)
  const [copied, setCopied] = useState(false)

  const formattedContent = useMemo(() => {
    if (format.toLowerCase() === "json" || filename.endsWith(".json")) {
      try {
        const parsed = JSON.parse(content)
        return JSON.stringify(parsed, null, 2)
      } catch {
        return content
      }
    }
    return content
  }, [content, format, filename])

  const lines = useMemo(() => formattedContent.split("\n"), [formattedContent])

  const copyContent = () => {
    navigator.clipboard.writeText(formattedContent)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
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
              placeholder={`Search in ${lines.length.toLocaleString()} lines...`}
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="pl-8 h-8 text-xs bg-background rounded-lg border-border/60"
            />
          </div>
        </div>

        <div className="flex items-center gap-2">
          <span className="px-2.5 py-1 rounded-md bg-[#ede9de] dark:bg-[#282a2c] text-foreground/80 font-medium text-[11px] font-mono uppercase">
            {format} · {lines.length.toLocaleString()} Lines
          </span>

          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setLineWrap((w) => !w)}
            className={`h-8 px-2.5 rounded-lg border-border/60 text-xs cursor-pointer ${
              lineWrap ? "bg-accent/80 text-foreground" : "text-muted-foreground"
            }`}
            title="Toggle Line Wrap"
          >
            <WrapText className="h-3.5 w-3.5 mr-1" />
            <span>Wrap</span>
          </Button>

          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={copyContent}
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
                <span>Copy Code</span>
              </>
            )}
          </Button>
        </div>
      </div>

      {/* Code Editor Styled Container */}
      <div className="flex-1 overflow-auto bg-[#1e1e1e] text-[#d4d4d4] p-4 font-mono text-xs select-text">
        <div className="flex min-w-full">
          {/* Line Numbers */}
          <div className="select-none text-[#858585] text-right pr-4 border-r border-[#333333] shrink-0 font-mono text-[11px]">
            {lines.map((_, idx) => (
              <div key={idx} className="leading-6">
                {idx + 1}
              </div>
            ))}
          </div>

          {/* Code Text Content */}
          <div className="pl-4 flex-1 min-w-0 font-mono text-xs">
            {lines.map((line, idx) => {
              const matchesSearch = searchTerm && line.toLowerCase().includes(searchTerm.toLowerCase())
              return (
                <div
                  key={idx}
                  className={`leading-6 ${
                    lineWrap ? "whitespace-pre-wrap break-words" : "whitespace-pre"
                  } ${matchesSearch ? "bg-amber-500/20 text-amber-200 px-1 rounded" : ""}`}
                >
                  {line || " "}
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}
