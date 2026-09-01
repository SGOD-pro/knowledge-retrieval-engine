import { useState, useEffect, useRef, useCallback } from "react"
import { Network, ZoomIn, ZoomOut, Maximize2 } from "lucide-react"
import { useChatStore } from "../../store/useChatStore"
import { useWorkspaceStore } from "../../store/useWorkspaceStore"
import type { KnowledgeGraphNode, KnowledgeGraphEdge } from "../../types/api"

// ─── Force-directed layout (no external dep) ────────────────────────────────

interface NodeSim {
  id: string
  x: number
  y: number
  vx: number
  vy: number
  fx?: number
  fy?: number
}

function runForceLayout(
  nodeIds: string[],
  edges: KnowledgeGraphEdge[],
  width: number,
  height: number,
  iterations = 200
): Record<string, { x: number; y: number }> {
  if (nodeIds.length === 0) return {}

  const nodeSet = new Set(nodeIds)

  // seed positions in a circle so layout is deterministic
  const nodes: NodeSim[] = nodeIds.map((id, i) => {
    const angle = (i / nodeIds.length) * 2 * Math.PI
    const r = Math.min(width, height) * 0.35
    return {
      id,
      x: width / 2 + r * Math.cos(angle),
      y: height / 2 + r * Math.sin(angle),
      vx: 0,
      vy: 0,
    }
  })

  const nodeMap: Record<string, NodeSim> = {}
  nodes.forEach((n) => (nodeMap[n.id] = n))

  // only use edges where both endpoints are in the node set
  const validEdges = edges.filter((e) => nodeSet.has(e.source) && nodeSet.has(e.target))

  const repulsion = 1200
  const attraction = 0.08
  const damping = 0.85
  const centerStrength = 0.015

  for (let iter = 0; iter < iterations; iter++) {
    // repulsion (O(n²) — fine for ≤50 nodes)
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i]
        const b = nodes[j]
        const dx = a.x - b.x
        const dy = a.y - b.y
        const dist = Math.sqrt(dx * dx + dy * dy) || 0.1
        const force = repulsion / (dist * dist)
        const fx = (dx / dist) * force
        const fy = (dy / dist) * force
        a.vx += fx
        a.vy += fy
        b.vx -= fx
        b.vy -= fy
      }
    }

    // spring attraction along edges
    validEdges.forEach((e) => {
      const a = nodeMap[e.source]
      const b = nodeMap[e.target]
      if (!a || !b) return
      const dx = b.x - a.x
      const dy = b.y - a.y
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.1
      const f = attraction * dist
      a.vx += (dx / dist) * f
      a.vy += (dy / dist) * f
      b.vx -= (dx / dist) * f
      b.vy -= (dy / dist) * f
    })

    // center gravity
    nodes.forEach((n) => {
      n.vx += (width / 2 - n.x) * centerStrength
      n.vy += (height / 2 - n.y) * centerStrength
      // apply velocity + damping
      n.vx *= damping
      n.vy *= damping
      n.x = Math.max(20, Math.min(width - 20, n.x + n.vx))
      n.y = Math.max(20, Math.min(height - 20, n.y + n.vy))
    })
  }

  const result: Record<string, { x: number; y: number }> = {}
  nodes.forEach((n) => (result[n.id] = { x: n.x, y: n.y }))
  return result
}

// ─── Component ────────────────────────────────────────────────────────────────

const NODE_COLORS: Record<string, string> = {
  person: "#c96442",
  author: "#c96442",
  document: "#006768",
  file: "#006768",
  company: "#9c87f5",
  organization: "#9c87f5",
  technology: "#3b82f6",
  concept: "#b05730",
  location: "#16a34a",
  event: "#d97706",
  default: "#605f57",
}

function nodeColor(type: string) {
  return NODE_COLORS[type?.toLowerCase()] ?? NODE_COLORS.default
}

const EDGE_COLORS: Record<string, string> = {
  SEMANTICALLY_RELATED: "#9c87f5",
  CONTAINS_FACT: "#c96442",
  CHILD_OF: "#16a34a",
  LOCATED_ON: "#3b82f6",
  CO_OCCURS_WITH: "#d97706",
  default: "#888",
}

function edgeColor(label: string) {
  return EDGE_COLORS[label] ?? EDGE_COLORS.default
}

const EDGE_LABELS: Record<string, string> = {
  SEMANTICALLY_RELATED: "similar",
  CONTAINS_FACT: "contains",
  CHILD_OF: "child of",
  LOCATED_ON: "on page",
  CO_OCCURS_WITH: "co-occurs",
}

export function KnowledgeGraphPane() {
  const { activeWorkspace } = useWorkspaceStore()
  const { graphData, fetchGraph } = useChatStore()
  const [selectedNode, setSelectedNode] = useState<KnowledgeGraphNode | null>(null)
  const [selectedEdge, setSelectedEdge] = useState<KnowledgeGraphEdge | null>(null)
  const [zoom, setZoom] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [isPanning, setIsPanning] = useState(false)
  const panStart = useRef<{ x: number; y: number; px: number; py: number } | null>(null)
  const svgRef = useRef<SVGSVGElement>(null)

  const currentWsId = activeWorkspace?.id || ""

  useEffect(() => {
    if (currentWsId) fetchGraph(currentWsId)
  }, [currentWsId, fetchGraph])

  const W = 600
  const H = 420

  const rawNodes = graphData?.nodes || []
  const rawEdges = graphData?.edges || []

  // Build the set of node IDs so we can filter edges to only rendered ones
  const nodeIdSet = new Set(rawNodes.map((n) => n.id))

  // Only include edges where BOTH endpoints are known entity nodes
  const visibleEdges = rawEdges.filter(
    (e) => nodeIdSet.has(e.source) && nodeIdSet.has(e.target)
  )

  // Compute degree for sizing
  const degree: Record<string, number> = {}
  rawNodes.forEach((n) => (degree[n.id] = 0))
  visibleEdges.forEach((e) => {
    degree[e.source] = (degree[e.source] || 0) + 1
    degree[e.target] = (degree[e.target] || 0) + 1
  })

  // Run force layout — recompute only when node list changes (memoised by length+ids)
  const nodeKey = rawNodes.map((n) => n.id).join("|")
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({})

  useEffect(() => {
    if (rawNodes.length === 0) { setPositions({}); return }
    const p = runForceLayout(rawNodes.map((n) => n.id), visibleEdges, W, H)
    setPositions(p)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeKey])

  const nodeRadius = (id: string) => {
    const freq = rawNodes.find((n) => n.id === id)?.properties?.frequency ?? 1
    const d = degree[id] ?? 0
    return Math.max(10, Math.min(22, 10 + Math.log2((Number(freq) || 1) + 1) * 2 + d))
  }

  // Pan handlers
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.target === svgRef.current || (e.target as SVGElement).tagName === "svg") {
      setIsPanning(true)
      panStart.current = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y }
    }
  }, [pan])

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!isPanning || !panStart.current) return
    setPan({
      x: panStart.current.px + (e.clientX - panStart.current.x),
      y: panStart.current.py + (e.clientY - panStart.current.y),
    })
  }, [isPanning])

  const handleMouseUp = useCallback(() => {
    setIsPanning(false)
    panStart.current = null
  }, [])

  const zoomIn = () => setZoom((z) => Math.min(3, z + 0.2))
  const zoomOut = () => setZoom((z) => Math.max(0.3, z - 0.2))
  const resetView = () => { setZoom(1); setPan({ x: 0, y: 0 }) }

  const hiddenEdgeCount = rawEdges.length - visibleEdges.length

  return (
    <div className="flex-1 flex flex-col bg-card overflow-hidden">
      {/* Header */}
      <div className="px-4 py-2.5 border-b border-border/40 flex items-center justify-between bg-card/60">
        <div className="flex items-center gap-2 text-xs font-medium text-foreground">
          <Network className="h-3.5 w-3.5 text-[#c96442]" />
          <span>OKF Knowledge Graph</span>
        </div>
        <div className="flex items-center gap-3">
          {hiddenEdgeCount > 0 && (
            <span className="text-[10px] text-amber-600 dark:text-amber-400 font-medium">
              {hiddenEdgeCount} cross-type edges hidden
            </span>
          )}
          <span className="text-[10px] font-semibold text-muted-foreground">
            {rawNodes.length} Nodes · {visibleEdges.length} Edges
          </span>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={zoomOut}
              className="p-1 rounded hover:bg-accent text-muted-foreground hover:text-foreground cursor-pointer"
              title="Zoom out"
            >
              <ZoomOut className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={resetView}
              className="p-1 rounded hover:bg-accent text-muted-foreground hover:text-foreground cursor-pointer text-[10px] font-mono"
              title="Reset view"
            >
              {Math.round(zoom * 100)}%
            </button>
            <button
              type="button"
              onClick={zoomIn}
              className="p-1 rounded hover:bg-accent text-muted-foreground hover:text-foreground cursor-pointer"
              title="Zoom in"
            >
              <ZoomIn className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={resetView}
              className="p-1 rounded hover:bg-accent text-muted-foreground hover:text-foreground cursor-pointer"
              title="Reset view"
            >
              <Maximize2 className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>

      {rawNodes.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center p-6 text-center space-y-3 bg-muted/20">
          <div className="h-10 w-10 rounded-2xl bg-muted text-muted-foreground flex items-center justify-center">
            <Network className="h-5 w-5" />
          </div>
          <div className="space-y-1 max-w-xs">
            <h4 className="font-headline font-bold text-xs text-foreground">
              No Entities Extracted Yet
            </h4>
            <p className="text-[11px] text-muted-foreground leading-relaxed">
              Upload documents to extract concepts, entities, and cross-document
              relationships into the knowledge graph.
            </p>
          </div>
        </div>
      ) : (
        <>
          {/* SVG Canvas */}
          <div
            className="flex-1 relative overflow-hidden bg-muted/20 select-none"
            style={{ cursor: isPanning ? "grabbing" : "grab" }}
          >
            <svg
              ref={svgRef}
              className="w-full h-full"
              viewBox={`0 0 ${W} ${H}`}
              onMouseDown={handleMouseDown}
              onMouseMove={handleMouseMove}
              onMouseUp={handleMouseUp}
              onMouseLeave={handleMouseUp}
            >
              <defs>
                <marker
                  id="arrow"
                  markerWidth="6"
                  markerHeight="6"
                  refX="5"
                  refY="3"
                  orient="auto"
                >
                  <path d="M0,0 L6,3 L0,6 Z" fill="#888" opacity="0.6" />
                </marker>
                {/* per-type arrows */}
                {Object.entries(EDGE_COLORS).filter(([k]) => k !== "default").map(([rel, col]) => (
                  <marker
                    key={rel}
                    id={`arrow-${rel}`}
                    markerWidth="6"
                    markerHeight="6"
                    refX="5"
                    refY="3"
                    orient="auto"
                  >
                    <path d="M0,0 L6,3 L0,6 Z" fill={col} opacity="0.7" />
                  </marker>
                ))}
              </defs>

              <g transform={`translate(${pan.x},${pan.y}) scale(${zoom})`}>
                {/* Grid dots for depth */}
                <pattern id="grid" width="30" height="30" patternUnits="userSpaceOnUse">
                  <circle cx="1" cy="1" r="0.8" fill="var(--border)" opacity="0.25" />
                </pattern>
                <rect width={W} height={H} fill="url(#grid)" />

                {/* Edges */}
                {visibleEdges.map((edge, i) => {
                  const src = positions[edge.source]
                  const tgt = positions[edge.target]
                  if (!src || !tgt) return null
                  const dx = tgt.x - src.x
                  const dy = tgt.y - src.y
                  const dist = Math.sqrt(dx * dx + dy * dy) || 1
                  // shorten to not overlap with node circle
                  const r1 = nodeRadius(edge.source)
                  const r2 = nodeRadius(edge.target)
                  const x1 = src.x + (dx / dist) * r1
                  const y1 = src.y + (dy / dist) * r1
                  const x2 = tgt.x - (dx / dist) * (r2 + 5)
                  const y2 = tgt.y - (dy / dist) * (r2 + 5)
                  const midX = (x1 + x2) / 2
                  const midY = (y1 + y2) / 2
                  const col = edgeColor(edge.label)
                  const isSelected = selectedEdge === edge
                  return (
                    <g
                      key={i}
                      onClick={(e) => {
                        e.stopPropagation()
                        setSelectedEdge(isSelected ? null : edge)
                        setSelectedNode(null)
                      }}
                      className="cursor-pointer"
                    >
                      {/* Hit area */}
                      <line
                        x1={x1} y1={y1} x2={x2} y2={y2}
                        stroke="transparent"
                        strokeWidth="10"
                      />
                      <line
                        x1={x1} y1={y1} x2={x2} y2={y2}
                        stroke={col}
                        strokeWidth={isSelected ? 2.5 : 1.2}
                        opacity={isSelected ? 1 : 0.55}
                        strokeDasharray={edge.label === "SEMANTICALLY_RELATED" ? "5 3" : undefined}
                        markerEnd={`url(#arrow-${edge.label})`}
                      />
                      {/* Edge label on hover / selection */}
                      {isSelected && (
                        <text
                          x={midX}
                          y={midY - 6}
                          fill={col}
                          fontSize="8"
                          fontWeight="700"
                          textAnchor="middle"
                          className="select-none"
                        >
                          {EDGE_LABELS[edge.label] ?? edge.label}
                        </text>
                      )}
                    </g>
                  )
                })}

                {/* Nodes */}
                {rawNodes.map((node) => {
                  const pos = positions[node.id]
                  if (!pos) return null
                  const isSelected = selectedNode?.id === node.id
                  const col = nodeColor(node.type)
                  const r = nodeRadius(node.id)
                  const shortLabel = node.label
                    ? node.label.length > 14
                      ? node.label.slice(0, 12) + "…"
                      : node.label
                    : node.id.slice(0, 12)
                  return (
                    <g
                      key={node.id}
                      transform={`translate(${pos.x},${pos.y})`}
                      onClick={(e) => {
                        e.stopPropagation()
                        setSelectedNode(isSelected ? null : node)
                        setSelectedEdge(null)
                      }}
                      className="cursor-pointer"
                      style={{ transition: "transform 0.1s" }}
                    >
                      {/* Glow ring on selection */}
                      {isSelected && (
                        <circle
                          r={r + 6}
                          fill={col}
                          opacity="0.2"
                        />
                      )}
                      <circle
                        r={r}
                        fill={col}
                        stroke={isSelected ? "#fff" : "rgba(255,255,255,0.3)"}
                        strokeWidth={isSelected ? 2.5 : 1}
                      />
                      {/* Type initial */}
                      <text
                        y="0"
                        fill="#fff"
                        fontSize={r > 14 ? "9" : "7"}
                        fontWeight="800"
                        textAnchor="middle"
                        dominantBaseline="central"
                        className="select-none pointer-events-none"
                      >
                        {node.type ? node.type[0].toUpperCase() : "E"}
                      </text>
                      {/* Label below */}
                      <text
                        y={r + 9}
                        fill="var(--foreground)"
                        fontSize="8"
                        fontWeight="600"
                        textAnchor="middle"
                        className="select-none pointer-events-none"
                        style={{ textShadow: "0 0 4px var(--background)" }}
                      >
                        {shortLabel}
                      </text>
                    </g>
                  )
                })}
              </g>
            </svg>
          </div>

          {/* Legend */}
          <div className="px-4 py-2 border-t border-border/30 flex items-center gap-3 flex-wrap bg-card/40">
            {Object.entries(EDGE_COLORS).filter(([k]) => k !== "default").map(([rel, col]) => (
              <div key={rel} className="flex items-center gap-1">
                <div
                  className="h-1.5 w-5 rounded-full"
                  style={{ backgroundColor: col, opacity: 0.8 }}
                />
                <span className="text-[9px] text-muted-foreground font-medium">
                  {EDGE_LABELS[rel] ?? rel}
                </span>
              </div>
            ))}
          </div>

          {/* Details Card */}
          <div className="p-4 border-t border-border/60 bg-card min-h-[70px]">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
                {selectedNode ? "Entity Details" : selectedEdge ? "Edge Details" : "Click to Inspect"}
              </span>
              {selectedNode && (
                <span
                  className="px-2 py-0.5 rounded text-[10px] font-bold text-white uppercase"
                  style={{ backgroundColor: nodeColor(selectedNode.type) }}
                >
                  {selectedNode.type || "Entity"}
                </span>
              )}
            </div>

            {selectedNode ? (
              <div className="space-y-0.5 text-xs">
                <div className="font-bold text-foreground">{selectedNode.label}</div>
                {selectedNode.properties && (
                  <div className="text-muted-foreground text-[11px] flex gap-4 mt-1">
                    {Object.entries(selectedNode.properties).map(([k, v]) => (
                      <div key={k} className="flex gap-1">
                        <span className="font-semibold capitalize">{k}:</span>
                        <span>{String(v)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ) : selectedEdge ? (
              <div className="text-xs text-muted-foreground flex gap-4">
                <span className="font-semibold text-foreground">{selectedEdge.source}</span>
                <span
                  className="px-1.5 py-0.5 rounded text-[10px] font-bold text-white"
                  style={{ backgroundColor: edgeColor(selectedEdge.label) }}
                >
                  {EDGE_LABELS[selectedEdge.label] ?? selectedEdge.label}
                </span>
                <span className="font-semibold text-foreground">{selectedEdge.target}</span>
                {selectedEdge.weight != null && selectedEdge.weight !== 1.0 && (
                  <span className="text-muted-foreground">score: {selectedEdge.weight.toFixed(3)}</span>
                )}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">
                Click a node or edge to inspect its attributes.
              </p>
            )}
          </div>
        </>
      )}
    </div>
  )
}
