import { useState, useEffect } from "react"
import { Network } from "lucide-react"
import { useChatStore } from "../../store/useChatStore"
import { useWorkspaceStore } from "../../store/useWorkspaceStore"
import type { KnowledgeGraphNode } from "../../types/api"

export function KnowledgeGraphPane() {
  const { activeWorkspace } = useWorkspaceStore()
  const { graphData, fetchGraph } = useChatStore()
  const [selectedNode, setSelectedNode] = useState<KnowledgeGraphNode | null>(null)

  const currentWsId = activeWorkspace?.id || ""

  useEffect(() => {
    if (currentWsId) {
      fetchGraph(currentWsId)
    }
  }, [currentWsId, fetchGraph])

  const nodes = graphData?.nodes || []
  const edges = graphData?.edges || []

  // Dynamic layout positioning for nodes in a circle
  const centerX = 200
  const centerY = 180
  const radius = Math.min(130, Math.max(70, nodes.length * 22))

  const nodePositions: Record<string, { x: number; y: number }> = {}
  nodes.forEach((node, idx) => {
    if (nodes.length === 1) {
      nodePositions[node.id] = { x: centerX, y: centerY }
    } else {
      const angle = (idx / nodes.length) * 2 * Math.PI - Math.PI / 2
      nodePositions[node.id] = {
        x: Math.round(centerX + radius * Math.cos(angle)),
        y: Math.round(centerY + radius * Math.sin(angle))
      }
    }
  })

  const getNodeColor = (type: string) => {
    switch (type?.toLowerCase()) {
      case "person":
      case "author":
        return "#c96442"
      case "document":
      case "file":
        return "#006768"
      case "company":
      case "organization":
        return "#9c87f5"
      case "technology":
      case "concept":
        return "#b05730"
      default:
        return "#605f57"
    }
  }

  return (
    <div className="flex-1 flex flex-col bg-card overflow-hidden">
      {/* Subheader */}
      <div className="px-5 py-2.5 border-b border-border/40 flex items-center justify-between bg-card/60">
        <div className="flex items-center gap-2 text-xs font-medium text-foreground">
          <Network className="h-3.5 w-3.5 text-[#c96442]" />
          <span>OKF Knowledge Graph</span>
        </div>
        <span className="text-[10px] font-semibold text-muted-foreground">
          {nodes.length} Nodes • {edges.length} Edges
        </span>
      </div>

      {nodes.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center p-6 text-center space-y-3 bg-muted/20">
          <div className="h-10 w-10 rounded-2xl bg-muted text-muted-foreground flex items-center justify-center">
            <Network className="h-5 w-5" />
          </div>
          <div className="space-y-1 max-w-xs">
            <h4 className="font-headline font-bold text-xs text-foreground">
              No Entities Extracted Yet
            </h4>
            <p className="text-[11px] text-muted-foreground leading-relaxed">
              Upload documents to extract concepts, entities, and cross-document relationships into the knowledge graph.
            </p>
          </div>
        </div>
      ) : (
        <>
          {/* SVG Interactive Visualizer */}
          <div className="flex-1 relative bg-muted/30 flex items-center justify-center p-4">
            <svg className="w-full h-full max-h-[380px]" viewBox="0 0 400 360">
              {/* Edges */}
              {edges.map((edge, i) => {
                const src = nodePositions[edge.source] || { x: centerX, y: centerY }
                const tgt = nodePositions[edge.target] || { x: centerX, y: centerY }
                const midX = (src.x + tgt.x) / 2
                const midY = (src.y + tgt.y) / 2

                return (
                  <g key={i}>
                    <line
                      x1={src.x}
                      y1={src.y}
                      x2={tgt.x}
                      y2={tgt.y}
                      stroke="var(--border)"
                      strokeWidth="1.5"
                      strokeDasharray="4 4"
                      opacity="0.8"
                    />
                    {edge.label && (
                      <text
                        x={midX}
                        y={midY - 4}
                        fill="var(--muted-foreground)"
                        fontSize="8"
                        fontWeight="600"
                        textAnchor="middle"
                        className="select-none"
                      >
                        {edge.label}
                      </text>
                    )}
                  </g>
                )
              })}

              {/* Nodes */}
              {nodes.map((node) => {
                const pos = nodePositions[node.id] || { x: centerX, y: centerY }
                const isSelected = selectedNode?.id === node.id
                const color = getNodeColor(node.type)

                return (
                  <g
                    key={node.id}
                    transform={`translate(${pos.x}, ${pos.y})`}
                    onClick={() => setSelectedNode(node)}
                    className="cursor-pointer transition-transform hover:scale-110"
                  >
                    <circle
                      r={isSelected ? "22" : "18"}
                      fill={color}
                      stroke="#ffffff"
                      strokeWidth={isSelected ? "3" : "1.5"}
                      className="shadow-md"
                    />
                    <text
                      y="4"
                      fill="#ffffff"
                      fontSize="9"
                      fontWeight="bold"
                      textAnchor="middle"
                      className="select-none pointer-events-none"
                    >
                      {node.type ? node.type[0].toUpperCase() : "N"}
                    </text>
                    <text
                      y="30"
                      fill="var(--foreground)"
                      fontSize="9"
                      fontWeight="600"
                      textAnchor="middle"
                      className="select-none pointer-events-none"
                    >
                      {node.label && node.label.length > 18 ? node.label.slice(0, 16) + "…" : node.label || node.id}
                    </text>
                  </g>
                )
              })}
            </svg>
          </div>

          {/* Selected Node Details Card */}
          <div className="p-4 border-t border-border/60 bg-card space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
                Entity Details
              </span>
              {selectedNode && (
                <span
                  className="px-2 py-0.5 rounded text-[10px] font-bold text-white uppercase"
                  style={{ backgroundColor: getNodeColor(selectedNode.type) }}
                >
                  {selectedNode.type}
                </span>
              )}
            </div>

            {selectedNode ? (
              <div className="space-y-1 text-xs">
                <div className="font-bold text-foreground">{selectedNode.label}</div>
                {selectedNode.properties && (
                  <div className="text-muted-foreground text-[11px] space-y-0.5">
                    {Object.entries(selectedNode.properties).map(([k, v]) => (
                      <div key={k} className="flex gap-1.5">
                        <span className="font-semibold capitalize">{k}:</span>
                        <span>{String(v)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">
                Click on any node above to inspect entity attributes and extracted connections.
              </p>
            )}
          </div>
        </>
      )}
    </div>
  )
}
