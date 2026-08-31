import { useState, useEffect } from "react"
import mammoth from "mammoth"
import { FileText, RefreshCw, AlertCircle } from "lucide-react"

interface DocxViewerProps {
  arrayBuffer: ArrayBuffer
  filename?: string
  fallbackText?: string
}

export function DocxViewer({ arrayBuffer, fallbackText }: DocxViewerProps) {
  const [htmlContent, setHtmlContent] = useState<string>("")
  const [loading, setLoading] = useState<boolean>(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let isCancelled = false
    setLoading(true)
    setError(null)

    mammoth
      .convertToHtml({ arrayBuffer })
      .then((result) => {
        if (!isCancelled) {
          setHtmlContent(result.value || "<p>Empty document</p>")
          setLoading(false)
        }
      })
      .catch((err) => {
        console.warn("Mammoth conversion error:", err)
        if (!isCancelled) {
          if (fallbackText) {
            setHtmlContent(`<pre class="whitespace-pre-wrap font-mono text-xs">${fallbackText}</pre>`)
          } else {
            setError(err?.message || "Failed to render Word document (.docx)")
          }
          setLoading(false)
        }
      })

    return () => {
      isCancelled = true
    }
  }, [arrayBuffer, fallbackText])

  if (loading) {
    return (
      <div className="h-96 flex flex-col items-center justify-center gap-3 text-muted-foreground">
        <RefreshCw className="h-6 w-6 animate-spin text-primary" />
        <p className="text-xs font-medium">Parsing Word document...</p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="h-96 flex flex-col items-center justify-center p-6 text-center bg-card border border-border/80 rounded-2xl">
        <AlertCircle className="h-10 w-10 text-destructive mb-3 opacity-60" />
        <h3 className="font-semibold text-foreground text-sm">Unable to render Word document</h3>
        <p className="text-xs text-muted-foreground mt-1 max-w-md">{error}</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full bg-card border border-border/80 rounded-2xl overflow-hidden shadow-xs">
      {/* Header bar */}
      <div className="p-3 border-b border-border/60 bg-muted/20 flex items-center justify-between text-xs">
        <span className="text-muted-foreground flex items-center gap-2">
          <FileText className="h-4 w-4 text-primary" />
          <span>Word Document Reader</span>
        </span>
        <span className="text-[11px] text-muted-foreground font-mono">
          DOCX Semantic Layout
        </span>
      </div>

      {/* Rendered HTML Container styled with document typography */}
      <div className="flex-1 p-6 md:p-12 overflow-y-auto bg-background/50">
        <div
          className="max-w-3xl mx-auto bg-card p-8 md:p-12 rounded-2xl border border-border/70 shadow-sm space-y-4 text-foreground font-sans leading-relaxed text-sm [&_h1]:text-2xl [&_h1]:font-headline [&_h1]:font-bold [&_h1]:text-primary [&_h1]:pt-4 [&_h1]:pb-2 [&_h1]:border-b [&_h1]:border-border/60 [&_h2]:text-xl [&_h2]:font-headline [&_h2]:font-bold [&_h2]:text-foreground [&_h2]:pt-3 [&_h2]:pb-1 [&_h3]:text-lg [&_h3]:font-bold [&_h3]:text-foreground/90 [&_p]:text-foreground/90 [&_p]:leading-relaxed [&_ul]:list-disc [&_ul]:ml-6 [&_ol]:list-decimal [&_ol]:ml-6 [&_table]:w-full [&_table]:border-collapse [&_table]:border [&_table]:border-border/80 [&_th]:bg-muted/40 [&_th]:p-2.5 [&_th]:border [&_th]:border-border/60 [&_td]:p-2.5 [&_td]:border [&_td]:border-border/40 [&_blockquote]:pl-4 [&_blockquote]:border-l-4 [&_blockquote]:border-primary/60 [&_blockquote]:italic [&_blockquote]:text-muted-foreground"
          dangerouslySetInnerHTML={{ __html: htmlContent }}
        />
      </div>
    </div>
  )
}
