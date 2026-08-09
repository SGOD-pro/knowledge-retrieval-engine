import { useState, useRef, useEffect } from "react"
import { Download, Waypoints } from "lucide-react"
import { NotLiveBadge } from "@/components/NotLiveBadge"
import { Button } from "@/components/ui/button"
import { ResizablePanelGroup, ResizablePanel, ResizableHandle } from "@/components/ui/resizable"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Progress } from "@/components/ui/progress"
import { ScrollArea } from "@/components/ui/scroll-area"
import ForceGraph2D from "react-force-graph-2d"

// Custom ResizeObserver hook
function useContainerDimensions(ref: React.RefObject<HTMLElement | null>) {
  const [dimensions, setDimensions] = useState({ width: 0, height: 0 })
  useEffect(() => {
    if (!ref.current) return
    const observer = new ResizeObserver(entries => {
      if (entries[0]) {
        setDimensions({
          width: entries[0].contentRect.width,
          height: entries[0].contentRect.height
        })
      }
    })
    observer.observe(ref.current)
    return () => observer.disconnect()
  }, [ref])
  return dimensions
}

// Mock OKF Data matching OKFRetriever.lookup()
const mockGraphData = {
  live: false,
  nodes: [
    { id: "Document 1", group: 1, type: "document" },
    { id: "Project Alpha", group: 2, type: "entity" },
    { id: "Revenue 2025", group: 3, type: "metric" },
    { id: "Company X", group: 2, type: "entity" }
  ],
  links: [
    { source: "Document 1", target: "Project Alpha", relation: "mentions" },
    { source: "Project Alpha", target: "Revenue 2025", relation: "targets" },
    { source: "Company X", target: "Project Alpha", relation: "owns" }
  ],
  properties: [
    { entity: "Project Alpha", property: "Status", value: "Active", confidence: 0.95 },
    { entity: "Revenue 2025", property: "Amount", value: "$10M", confidence: 0.88 },
    { entity: "Company X", property: "Sector", value: "Technology", confidence: 0.92 }
  ]
}

export function KnowledgeGraph() {
  const containerRef = useRef<HTMLDivElement>(null)
  const { width, height } = useContainerDimensions(containerRef)
  
  const handleExport = () => {
    const blob = new Blob([JSON.stringify(mockGraphData, null, 2)], { type: "application/json" })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = "kre-knowledge-graph-PREVIEW.json"
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  // Pre-calculate node colors based on CSS variables (Tailwind v4 theme)
  const getNodeColor = (node: any) => {
    switch (node.type) {
      case "document": return "var(--primary)"
      case "entity": return "var(--chart-2)"
      case "metric": return "var(--chart-1)"
      default: return "var(--muted-foreground)"
    }
  }

  return (
    <div className="h-full flex flex-col p-6 gap-4">
      <div className="flex items-center justify-between shrink-0">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight flex items-center gap-2 text-primary">
            <Waypoints className="h-6 w-6" />
            Knowledge Graph Explorer
          </h2>
          <p className="text-muted-foreground mt-1">Visualize extracted entities and OKF properties.</p>
        </div>
        <div className="flex items-center gap-4">
          <NotLiveBadge text="Static schema preview" />
          <Button variant="outline" onClick={handleExport}>
            <Download className="mr-2 h-4 w-4" />
            Export JSON
          </Button>
        </div>
      </div>

      <ResizablePanelGroup direction="horizontal" className="flex-1 rounded-xl border border-border overflow-hidden">
        
        {/* Left Pane: Force Graph */}
        <ResizablePanel defaultSize={65} minSize={40} className="bg-card relative">
          <div className="absolute inset-0" ref={containerRef}>
            {width > 0 && height > 0 && (
              <ForceGraph2D
                width={width}
                height={height}
                graphData={mockGraphData}
                nodeColor={getNodeColor}
                nodeRelSize={6}
                linkColor={() => "var(--border)"}
                backgroundColor="transparent"
                linkDirectionalArrowLength={3.5}
                linkDirectionalArrowRelPos={1}
                nodeLabel="id"
              />
            )}
          </div>
        </ResizablePanel>

        <ResizableHandle withHandle />

        {/* Right Pane: Extracted Properties */}
        <ResizablePanel defaultSize={35} minSize={20} className="bg-background flex flex-col">
          <div className="p-4 border-b border-border bg-sidebar/50 shrink-0">
            <h3 className="font-semibold tracking-tight">Extracted OKF Properties</h3>
          </div>
          <ScrollArea className="flex-1 p-4">
            <div className="flex flex-col gap-3">
              {mockGraphData.properties.map((prop, idx) => (
                <Card key={idx} className="shadow-none border-border">
                  <CardHeader className="p-3 pb-2 flex flex-row items-start justify-between space-y-0">
                    <div>
                      <CardTitle className="text-sm font-medium text-primary">
                        {prop.entity}
                      </CardTitle>
                      <p className="text-xs text-muted-foreground mt-0.5">{prop.property}</p>
                    </div>
                    <Badge variant="secondary" className="text-[10px] uppercase font-medium rounded-sm">
                      {prop.value}
                    </Badge>
                  </CardHeader>
                  <CardContent className="p-3 pt-0">
                    <div className="flex items-center justify-between text-[10px] text-muted-foreground mb-1">
                      <span>Confidence</span>
                      <span>{(prop.confidence * 100).toFixed(0)}%</span>
                    </div>
                    <Progress value={prop.confidence * 100} className="h-1 bg-accent" />
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
