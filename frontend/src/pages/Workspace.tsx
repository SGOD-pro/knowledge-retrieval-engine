import { useState } from "react"
import { useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import * as z from "zod"
import { Send, ZoomIn, ZoomOut, ChevronLeft, ChevronRight, FileText, Database } from "lucide-react"
import { NotLiveBadge } from "@/components/NotLiveBadge"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { ResizablePanelGroup, ResizablePanel, ResizableHandle } from "@/components/ui/resizable"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Breadcrumb, BreadcrumbItem, BreadcrumbLink, BreadcrumbList, BreadcrumbSeparator } from "@/components/ui/breadcrumb"
import { Form, FormControl, FormField, FormItem } from "@/components/ui/form"

const querySchema = z.object({
  query: z.string().min(1, "Query is required")
})

interface Citation {
  chunk_id: string
  document_id: string
  source_format: string
  bounding_box: any
  location_reference: string
  text_snippet: string
}

interface QueryResponse {
  answer: string
  citations: Citation[]
  confidence_score: number
  latency_breakdown: any
  fast_path: boolean
  cached: boolean
  document_ids: string[]
  retrieval_path: string[]
}

export function Workspace() {
  const [messages, setMessages] = useState<{ role: "user" | "agent", content: string, responseData?: QueryResponse }[]>([])
  const [isQuerying, setIsQuerying] = useState(false)
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null)

  const form = useForm<z.infer<typeof querySchema>>({
    resolver: zodResolver(querySchema),
    defaultValues: {
      query: ""
    }
  })

  async function onSubmit(values: z.infer<typeof querySchema>) {
    const queryText = values.query
    setMessages(prev => [...prev, { role: "user", content: queryText }])
    form.reset()
    setIsQuerying(true)

    try {
      const res = await fetch("/api/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: queryText })
      })
      if (!res.ok) throw new Error("Query failed")
      
      const data: QueryResponse = await res.json()
      setMessages(prev => [...prev, { role: "agent", content: data.answer, responseData: data }])
    } catch (e) {
      console.error(e)
      setMessages(prev => [...prev, { role: "agent", content: "Error communicating with backend." }])
    } finally {
      setIsQuerying(false)
    }
  }

  const latestResponse = messages.filter(m => m.role === "agent").pop()?.responseData

  return (
    <div className="h-full flex flex-col">
      <ResizablePanelGroup direction="horizontal" className="flex-1">
        
        {/* Left Pane: Document Viewer */}
        <ResizablePanel defaultSize={25} minSize={20} className="bg-card flex flex-col border-r border-border">
          <div className="h-14 border-b border-border flex items-center justify-between px-4 shrink-0 bg-sidebar/50">
            <Breadcrumb>
              <BreadcrumbList>
                <BreadcrumbItem>
                  <BreadcrumbLink href="#">{selectedCitation ? `Doc ${selectedCitation.document_id.slice(0, 6)}...` : "Select a citation"}</BreadcrumbLink>
                </BreadcrumbItem>
                {selectedCitation?.source_format === "pdf" && selectedCitation.bounding_box && (
                  <>
                    <BreadcrumbSeparator />
                    <BreadcrumbItem>
                      <BreadcrumbLink href="#">Page {selectedCitation.bounding_box.page_number}</BreadcrumbLink>
                    </BreadcrumbItem>
                  </>
                )}
              </BreadcrumbList>
            </Breadcrumb>
            <div className="flex items-center gap-1">
              <Button variant="ghost" size="icon" className="h-8 w-8"><ChevronLeft className="h-4 w-4" /></Button>
              <Button variant="ghost" size="icon" className="h-8 w-8"><ChevronRight className="h-4 w-4" /></Button>
              <div className="w-px h-4 bg-border mx-1" />
              <Button variant="ghost" size="icon" className="h-8 w-8"><ZoomOut className="h-4 w-4" /></Button>
              <Button variant="ghost" size="icon" className="h-8 w-8"><ZoomIn className="h-4 w-4" /></Button>
            </div>
          </div>
          <ScrollArea className="flex-1 p-6">
            {selectedCitation ? (
              <div className="bg-background border rounded-lg p-8 shadow-sm text-foreground/90 font-serif leading-relaxed">
                <NotLiveBadge text="PDF rendering not built yet. Showing text snippet." />
                <div className="mt-4">
                  {selectedCitation.text_snippet}
                </div>
              </div>
            ) : (
              <div className="flex flex-col items-center justify-center h-full text-muted-foreground pt-32">
                <FileText className="h-12 w-12 mb-4 opacity-20" />
                <p>Click a citation to view source document</p>
              </div>
            )}
          </ScrollArea>
        </ResizablePanel>
        
        <ResizableHandle withHandle />
        
        {/* Center Pane: Chat Thread */}
        <ResizablePanel defaultSize={50} minSize={30} className="flex flex-col bg-background">
          <ScrollArea className="flex-1 p-6">
            <div className="max-w-3xl mx-auto space-y-6 pb-24">
              {messages.map((msg, i) => (
                <div key={i} className={`flex gap-4 ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
                  {msg.role === "agent" && (
                    <div className="h-8 w-8 rounded-full bg-primary flex items-center justify-center shrink-0 mt-1">
                      <Database className="h-4 w-4 text-primary-foreground" />
                    </div>
                  )}
                  <div className={`flex flex-col gap-2 max-w-[85%] ${msg.role === "user" ? "items-end" : "items-start"}`}>
                    {msg.role === "agent" && msg.responseData && (
                      <div className="flex flex-wrap gap-2 mb-1">
                        {msg.responseData.retrieval_path?.map((stage, idx) => (
                          <Badge key={idx} variant="secondary" className="text-[10px] uppercase tracking-wider bg-accent text-accent-foreground border-border">
                            {stage}
                          </Badge>
                        ))}
                        {msg.responseData.cached && (
                          <Badge variant="outline" className="text-[10px] uppercase tracking-wider text-green-600 border-green-200 bg-green-50 dark:bg-green-950 dark:border-green-900">
                            Cached
                          </Badge>
                        )}
                        <Badge variant="outline" className="text-[10px] uppercase tracking-wider border-border">
                          {msg.responseData.latency_breakdown?.total_ms}ms
                        </Badge>
                      </div>
                    )}
                    <div className={`p-4 rounded-xl shadow-sm text-[15px] leading-relaxed ${
                      msg.role === "user" 
                        ? "bg-primary text-primary-foreground rounded-tr-sm" 
                        : "bg-card text-card-foreground border border-border rounded-tl-sm"
                    }`}>
                      {msg.content}
                    </div>
                  </div>
                </div>
              ))}
              {isQuerying && (
                <div className="flex gap-4 justify-start">
                  <div className="h-8 w-8 rounded-full bg-primary flex items-center justify-center shrink-0 mt-1">
                    <Database className="h-4 w-4 text-primary-foreground animate-pulse" />
                  </div>
                  <div className="p-4 rounded-xl bg-card border border-border rounded-tl-sm w-24 flex gap-1 items-center justify-center">
                    <div className="h-2 w-2 rounded-full bg-primary/50 animate-bounce" style={{ animationDelay: "0ms" }} />
                    <div className="h-2 w-2 rounded-full bg-primary/50 animate-bounce" style={{ animationDelay: "150ms" }} />
                    <div className="h-2 w-2 rounded-full bg-primary/50 animate-bounce" style={{ animationDelay: "300ms" }} />
                  </div>
                </div>
              )}
            </div>
          </ScrollArea>
          
          <div className="p-4 border-t border-border bg-card shrink-0">
            <div className="max-w-3xl mx-auto relative">
              <Form {...form}>
                <form onSubmit={form.handleSubmit(onSubmit)} className="relative flex items-end gap-2 bg-background border border-input rounded-xl shadow-sm focus-within:ring-1 focus-within:ring-ring p-2">
                  <FormField
                    control={form.control}
                    name="query"
                    render={({ field }) => (
                      <FormItem className="flex-1">
                        <FormControl>
                          <Textarea 
                            placeholder="Ask the knowledge base..."
                            className="min-h-[44px] max-h-32 resize-none border-0 shadow-none focus-visible:ring-0 p-2 bg-transparent"
                            onKeyDown={(e) => {
                              if (e.key === "Enter" && !e.shiftKey) {
                                e.preventDefault()
                                form.handleSubmit(onSubmit)()
                              }
                            }}
                            {...field}
                          />
                        </FormControl>
                      </FormItem>
                    )}
                  />
                  <Button 
                    type="submit" 
                    size="icon" 
                    disabled={isQuerying || !form.watch("query")}
                    className="h-10 w-10 shrink-0 rounded-lg bg-primary hover:bg-primary/90"
                  >
                    <Send className="h-4 w-4" />
                  </Button>
                </form>
              </Form>
            </div>
          </div>
        </ResizablePanel>

        <ResizableHandle withHandle />

        {/* Right Pane: Cited Sources */}
        <ResizablePanel defaultSize={25} minSize={20} className="bg-sidebar flex flex-col border-l border-sidebar-border">
          <div className="h-14 border-b border-sidebar-border flex items-center px-6 shrink-0 font-medium">
            Sources
          </div>
          <ScrollArea className="flex-1 p-4">
            <div className="flex flex-col gap-4">
              {!latestResponse?.citations?.length && (
                <div className="text-sm text-muted-foreground text-center pt-8">
                  No citations to display
                </div>
              )}
              {latestResponse?.citations?.map((c, i) => (
                <Card 
                  key={i} 
                  className={`cursor-pointer transition-all hover:border-primary/50 ${selectedCitation?.chunk_id === c.chunk_id ? "border-primary ring-1 ring-primary shadow-md" : ""}`}
                  onClick={() => setSelectedCitation(c)}
                >
                  <CardHeader className="p-3 pb-0">
                    <div className="flex items-center justify-between gap-2">
                      <Badge variant="outline" className="text-[10px] uppercase">{c.source_format}</Badge>
                      <span className="text-xs text-muted-foreground truncate" title={c.document_id}>
                        {c.document_id.split("-")[0]}...
                      </span>
                    </div>
                    <CardTitle className="text-sm font-medium pt-2 text-primary leading-tight">
                      {c.source_format === "pdf" ? (
                        `Page ${c.bounding_box?.page_number || "Unknown"}`
                      ) : (
                        c.location_reference || "Unknown Location"
                      )}
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="p-3 pt-2">
                    <p className="text-xs text-muted-foreground line-clamp-4 leading-relaxed">
                      {c.text_snippet}
                    </p>
                  </CardContent>
                </Card>
              ))}
            </div>
          </ScrollArea>
        </ResizablePanel>

      </ResizablePanelGroup>
    </div>
  )
}
